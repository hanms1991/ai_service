"""agent_runner —— API 与 LangGraph 图 / 技能直连的唯一调用点。

设计文档 11.3 节："同步与异步复用同一内核"。
一期（M1）只实现同步入口：

  run_invoke(req, trace_id) → InvokeResponse
    1. 场景直达：skill 非空 → execute_skill_v2（跳过 Planner）
    2. 智能编排：skill 为空 → 调用 supervisor_graph.ainvoke
    3. reference_data 注入（50KB 阈值已在 execute_skill_v2 内校验）
    4. response_format 与 scene 冲突 → OUTPUT_FORMAT_CONFLICT
    5. trace_id 贯穿 llm_trace.log
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.errors import (
    agent_not_found,
    internal_error,
    invoke_timeout,
    llm_upstream_error,
    output_format_conflict,
    reference_data_too_large,
    scene_not_found,
    skill_input_missing,
    skill_not_found,
    skill_output_invalid,
)
from api.schemas import InvokeResponse
from api.services.scene_resolver import get_scene_resolver
from agents.capability_registry import (
    REFERENCE_DATA_MAX_BYTES,
    SkillResult,
    execute_skill_v2,
    get_registry,
    validate_binding,
)


# ── 同步 invoke 默认超时（秒）：scene 未配置且请求未指定时使用 ──
# 与异步任务默认时限对齐（_DEFAULT_ASYNC_TIMEOUT=600）
_DEFAULT_SYNC_TIMEOUT = int(os.getenv("SYNC_INVOKE_TIMEOUT_SECONDS", "600"))


# ── 单例图句柄（lifespan 启动时注入） ──
_supervisor_graph: Any = None


def set_supervisor_graph(graph: Any) -> None:
    """lifespan 启动时调用，注入编译后的 LangGraph 图。"""
    global _supervisor_graph
    _supervisor_graph = graph


def get_supervisor_graph() -> Any:
    if _supervisor_graph is None:
        raise RuntimeError("supervisor_graph 尚未注入，请检查 lifespan 是否已启动")
    return _supervisor_graph


def _new_trace_id() -> str:
    return f"trace-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _new_task_id() -> str:
    return f"t-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _new_thread_id() -> str:
    return f"conv-{uuid.uuid4().hex[:12]}"


def _make_llm_logger(trace_id: str):
    """为本次请求构造 LLM 日志回调，trace_id 写入日志。"""
    from core.logging import LLMInteractionLogger

    project_root = Path(__file__).resolve().parent.parent.parent
    # 每次请求一个 trace 日志条目（沿用现有 llm_trace.log，append 模式）
    logger = LLMInteractionLogger(
        project_root / "logs" / "llm_trace.log", verbose=False
    )
    # 在 logger 的日志文件开头写入 trace_id 标记（便于检索）
    try:
        with (project_root / "logs" / "llm_trace.log").open("a", encoding="utf-8") as f:
            f.write(f"\n[#trace_id={trace_id}]\n")
    except Exception:
        pass
    return logger


def _validate_inputs_against_schema(
    inputs: dict[str, Any],
    input_schema: dict[str, dict],
    skill_name: str,
) -> None:
    """场景直达前做输入校验，必填缺失直接报错，不消耗 LLM。"""
    missing = [
        name
        for name, spec in input_schema.items()
        if spec.get("required") and not inputs.get(name)
    ]
    if missing:
        raise skill_input_missing(missing, skill_name)


async def run_invoke(
    message: str | None,
    scene: str | None,
    inputs: dict[str, Any] | None,
    thread_id: str | None,
    context: dict[str, Any] | None,
    reference_data: dict[str, Any] | None,
    response_format: str | None,
    timeout_seconds: int | None,
    trace_id: str | None,
) -> InvokeResponse:
    """同步执行入口。

    两条路径：
      A. scene 非空 → 解析场景码 → skill 非空走 execute_skill_v2 直达；
                       skill 为空走 Planner 智能编排（需 message 必填）
      B. scene 为空 → 走 Planner 智能编排，message 必填
    """
    if trace_id is None:
        trace_id = _new_trace_id()
    task_id = _new_task_id()
    if thread_id is None:
        thread_id = _new_thread_id()

    # ── reference_data 阈值前置检查（不消耗 LLM） ──
    if reference_data:
        compact = json.dumps(reference_data, ensure_ascii=False, separators=(",", ":"))
        size = len(compact.encode("utf-8"))
        if size > REFERENCE_DATA_MAX_BYTES:
            raise reference_data_too_large(size, REFERENCE_DATA_MAX_BYTES)

    logger = _make_llm_logger(trace_id)
    runnable_config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": [logger],
    }

    # ── scene 作为 hint：解析后传给 Planner，不再直达技能 ──
    # 前端按钮选中的 scene 只是意图倾向，最终是否执行对应技能由 Planner 判断
    hint_agent = ""
    hint_skill = ""
    effective_timeout = timeout_seconds or _DEFAULT_SYNC_TIMEOUT

    if scene:
        resolver = get_scene_resolver()
        try:
            binding = resolver.resolve(scene)
        except KeyError:
            raise scene_not_found(scene)

        # response_format 冲突检查（以 scenes.yaml 为准）
        if response_format and response_format != binding.response_format:
            raise output_format_conflict(scene, binding.response_format, response_format)

        hint_agent = binding.agent
        hint_skill = binding.skill
        effective_timeout = (
            timeout_seconds or binding.timeout_seconds or _DEFAULT_SYNC_TIMEOUT
        )

    # 所有请求统一走 Planner 智能编排（intent_router → planner → executor）
    # Planner 从用户 message 中提取技能参数；提取不到则对话式追问
    user_message = message or ""
    if not user_message:
        raise skill_input_missing(["message"], "智能编排")

    try:
        return await asyncio.wait_for(
            _run_planner(
                user_message, thread_id, task_id, scene,
                hint_agent, hint_skill,
                reference_data, runnable_config, trace_id,
            ),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        raise invoke_timeout(task_id, effective_timeout)


async def _execute_skill_v2_async(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    *,
    reference_data: dict[str, Any] | None,
    runnable_config: Any,
) -> SkillResult:
    """异步包装 execute_skill_v2（一期用 asyncio.to_thread 把同步 LLM 调用包成异步）。"""
    import asyncio

    return await asyncio.to_thread(
        execute_skill_v2,
        agent_name,
        skill_name,
        inputs,
        reference_data=reference_data,
        runnable_config=runnable_config,
    )


async def _run_planner(
    user_message: str,
    thread_id: str,
    task_id: str,
    scene: str | None,
    hint_agent: str,
    hint_skill: str,
    reference_data: dict[str, Any] | None,
    runnable_config: dict,
    trace_id: str,
) -> InvokeResponse:
    """走 Planner 智能编排（supervisor_graph.ainvoke）。

    一期同步等待；超时由上层 (asyncio.wait_for) 控制，未在 M1 实现自动转异步。
    reference_data 注入到 user_message 末尾（只读段）。
    scene/hint_agent/hint_skill 作为场景提示注入 SupervisorState，由 Planner 参考。
    """
    # reference_data 注入到 message 末尾
    if reference_data:
        compact = json.dumps(reference_data, ensure_ascii=False, separators=(",", ":"))
        user_message = (
            f"{user_message}\n\n【参考数据（只读资料，仅供你参考，不要原样罗列或照搬其字段名）】\n{compact}"
        )

    graph = get_supervisor_graph()

    try:
        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content=user_message)],
                "scene": scene or "",
                "hint_agent": hint_agent or "",
                "hint_skill": hint_skill or "",
            },
            config=runnable_config,
        )
    except Exception as e:
        raise llm_upstream_error(str(e)) from e

    # ── M2: 检测 ask/confirm interrupt ──
    # 当 Planner 命中 interrupt 时，result 带 __interrupt__ 字段，final_output 为空
    interrupt_payload = _detect_interrupt(result)
    if interrupt_payload:
        # 命中中断：output/plan 都置空，由调用方（_run_task_async）落 waiting_human
        return InvokeResponse(
            thread_id=thread_id,
            task_id=task_id,
            mode="orchestrated",
            scene=scene,
            output=None,
            structured=None,
            plan=result.get("plan") or None,
            interrupt=interrupt_payload,
            usage={},
            trace_id=trace_id,
        )

    final_output = result.get("final_output") or ""
    if not final_output and result.get("messages"):
        last = result["messages"][-1]
        final_output = getattr(last, "content", str(last))

    plan = result.get("plan") or None

    return InvokeResponse(
        thread_id=thread_id,
        task_id=task_id,
        mode="orchestrated",
        scene=scene,
        output=final_output,
        structured=None,
        plan=plan,
        interrupt=None,
        usage={},  # Planner 模式一期不聚合 usage（M2 在 executor 层累加）
        trace_id=trace_id,
    )


def _try_parse_json(text: str) -> dict | list | None:
    """兜底：技能未声明 schema 但响应是 JSON 字符串时尝试解析。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


# ════════════════════════════════════════════════════════════════
# M2：异步任务执行入口（POST /tasks）
# ════════════════════════════════════════════════════════════════

import asyncio  # noqa: E402  (M1 已用 asyncio.to_thread 内联，此处统一导入供 M2 使用)
import os  # noqa: E402

from api.errors import (  # noqa: E402
    task_not_found,
    task_timeout,
)
from api.schemas import (  # noqa: E402
    AgentRequest,
    TaskStatus,
    TERMINAL_STATES,
)
from api.services.callbacks import get_callback_client  # noqa: E402
from api.services.scene_resolver import (  # noqa: E402
    SceneBinding,
)
from api.services.task_store import (  # noqa: E402
    TaskRecord,
    get_task_store,
)


# 默认异步整体时限（设计文档 12.3：异步任务整体时限默认 10 分钟）
_DEFAULT_ASYNC_TIMEOUT = int(os.getenv("ASYNC_TASK_TIMEOUT_SECONDS", "600"))


def _normalize_binding_dict(b: SceneBinding | dict | None) -> dict | None:
    """SceneBinding → dict（存到 record.request_snapshot 里便于序列化）。"""
    if b is None:
        return None
    if isinstance(b, dict):
        return b
    return {
        "scene": b.scene,
        "description": b.description,
        "agent": b.agent,
        "skill": b.skill,
        "response_format": b.response_format,
        "timeout_seconds": b.timeout_seconds,
    }


def _detect_interrupt(result: dict) -> dict | None:
    """从 graph.ainvoke 结果中检测 interrupt 载荷。

    LangGraph 在 interrupt() 处暂停后，结果字典带 `__interrupt__` 字段，
    其值为 tuple[Interrupt, ...]，每个 Interrupt.value 是传给 interrupt() 的载荷。

    返回标准化的 interrupt dict：
        {"type": "ask|confirm", "step_id": ..., "tool": ..., "skill": ..., "message": ...}
    若无法识别类型则回退为 {"type":"ask","message": str(value)}。
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    try:
        # interrupts 可能是 tuple/ list of Interrupt 对象
        for intr in interrupts:
            value = getattr(intr, "value", intr)
            if isinstance(value, dict):
                # executor_node 的 interrupt 载荷字段：step_id/tool/skill/input/message
                msg = value.get("message") or value.get("input") or ""
                return {
                    "type": "confirm" if value.get("tool") and value.get("skill") is None and "确认" in str(msg) else "ask",
                    "step_id": value.get("step_id"),
                    "tool": value.get("tool"),
                    "skill": value.get("skill"),
                    "message": msg,
                }
            return {"type": "ask", "message": str(value)}
    except Exception:
        return None
    return None


async def submit_task(req: AgentRequest, trace_id: str) -> TaskRecord:
    """提交异步任务（POST /agent/tasks 调用入口）。

    流程：
      1. 同步校验（不消耗 LLM）：scene 解析、response_format 冲突、reference_data 阈值、必填输入
      2. 创建 TaskRecord（pending）入库
      3. asyncio.create_task(run_task_async(record)) 后台执行（不等待）
      4. 返回 record 供 routes 层返回 202

    校验失败抛 ApiError（route 层转 4xx）；不消耗 LLM。
    """
    store = get_task_store()

    # ── 解析场景码 + 校验冲突（不消耗 LLM） ──
    # scene 作为 hint 传入 graph，不再在此校验技能输入
    binding: SceneBinding | None = None
    if req.scene:
        resolver = get_scene_resolver()
        try:
            binding = resolver.resolve(req.scene)
        except KeyError:
            raise scene_not_found(req.scene)
        if req.response_format and req.response_format != binding.response_format:
            raise output_format_conflict(
                req.scene, binding.response_format, req.response_format
            )

    # ── reference_data 阈值前置检查（不消耗 LLM） ──
    if req.reference_data:
        compact = json.dumps(req.reference_data, ensure_ascii=False, separators=(",", ":"))
        size = len(compact.encode("utf-8"))
        if size > REFERENCE_DATA_MAX_BYTES:
            raise reference_data_too_large(size, REFERENCE_DATA_MAX_BYTES)

    # ── 生成 IDs / 默认超时 ──
    task_id = _new_task_id()
    thread_id = req.thread_id or _new_thread_id()
    timeout_seconds = (
        req.timeout_seconds
        or (binding.timeout_seconds if binding else None)
        or _DEFAULT_ASYNC_TIMEOUT
    )

    # ── 幂等：相同 idempotency_key 返回旧任务 ──
    if req.idempotency_key:
        existing = await store.find_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return existing

    record = TaskRecord(
        task_id=task_id,
        thread_id=thread_id,
        status=TaskStatus.PENDING,
        trace_id=trace_id,
        scene=req.scene,
        callback_url=req.callback_url,
        idempotency_key=req.idempotency_key,
        timeout_seconds=timeout_seconds,
        request_snapshot={
            "message": req.message,
            "scene": req.scene,
            "inputs": req.inputs,
            "context": req.context,
            "reference_data": req.reference_data,
            "response_format": req.response_format,
            "thread_id": thread_id,
            "binding": _normalize_binding_dict(binding),
        },
    )
    await store.create(record)

    # ── 后台启动执行（不阻塞返回 202） ──
    record.async_task = asyncio.create_task(_run_task_async(record))
    return record


async def _run_task_async(record: TaskRecord) -> None:
    """异步任务后台执行体（fire-and-forget，由 submit_task 调度）。

    状态流转：
      pending → planning → running → completed | waiting_human | failed | timeout

    错误处理：所有异常被捕获并落为 task failed/timeout，不向上抛
    （create_task 的异常只会进 task exception，需要主动检查；这里直接落库）。
    """
    store = get_task_store()
    cb_client = get_callback_client()

    try:
        # ── 状态置 planning（所有任务统一走 Planner 编排） ──
        snap = record.request_snapshot
        await store.update(
            record.task_id,
            status=TaskStatus.PLANNING,
            mode="orchestrated",
        )

        # ── 内核执行：复用 run_invoke 的内核，但需要把结果回写到 store ──
        # 用 asyncio.wait_for 控制整体时限
        invoke_coro = run_invoke(
            message=snap.get("message"),
            scene=snap.get("scene"),
            inputs=snap.get("inputs"),
            thread_id=snap.get("thread_id"),
            context=snap.get("context"),
            reference_data=snap.get("reference_data"),
            response_format=snap.get("response_format"),
            timeout_seconds=record.timeout_seconds,
            trace_id=record.trace_id,
        )
        result = await asyncio.wait_for(invoke_coro, timeout=record.timeout_seconds)

        # ── 检测 interrupt（Planner 模式命中 ask/confirm） ──
        # _run_planner 已在 result.interrupt 字段填充标准化载荷
        if result.interrupt:
            await store.set_interrupt(record.task_id, result.interrupt)
            await cb_client.notify_if_needed(store.get(record.task_id))
            return

        # ── 无中断 → 落 completed ──
        await store.set_result(
            record.task_id,
            output=result.output,
            structured=result.structured,
            plan=result.plan,
            usage=result.usage,
            mode=result.mode,
        )
        await cb_client.notify_if_needed(record)

    except asyncio.TimeoutError:
        try:
            await store.mark_timeout(record.task_id)
            await cb_client.notify_if_needed(record)
        except Exception as e:
            print(f"[task {record.task_id}] timeout 后落库失败：{e}")

    except ApiError as e:
        try:
            await store.fail(record.task_id, f"[{e.code}] {e.message}")
            await cb_client.notify_if_needed(record)
        except Exception as ex:
            print(f"[task {record.task_id}] fail 后落库失败：{ex}")

    except asyncio.CancelledError:
        # 任务被 cancel 接口取消；store.cancel 已置状态，这里不重复改
        raise

    except Exception as e:
        try:
            await store.fail(record.task_id, f"运行时异常：{type(e).__name__}: {e}")
            await cb_client.notify_if_needed(record)
        except Exception as ex:
            print(f"[task {record.task_id}] exception 后落库失败：{ex}")


async def resume_task(task_id: str, reply: str) -> TaskRecord:
    """恢复 ask/confirm 中断（POST /agent/tasks/{id}/resume）。

    流程：
      1. 查询任务，校验 status=waiting_human（否则 task_already_finished）
      2. graph.ainvoke(Command(resume=reply), config=...) 续跑
      3. 检测新 interrupt → set_interrupt（仍 waiting_human）
         否则 → set_result（completed）
      4. 触发回调

    Returns:
        更新后的 TaskRecord（route 层转 TaskDetailResponse）

    Raises:
        task_not_found / task_already_finished / llm_upstream_error
    """
    store = get_task_store()
    cb_client = get_callback_client()
    record = store.get(task_id)

    if record.status != TaskStatus.WAITING_HUMAN:
        # 已是终态 → 409；其他非终态（running/planning）→ 不允许 resume
        if record.status in TERMINAL_STATES:
            from api.errors import task_already_finished
            raise task_already_finished(task_id, record.status.value)
        # 非 waiting_human 也不允许 resume
        from api.errors import request_invalid
        raise request_invalid(
            f"任务 {task_id} 当前状态为 {record.status.value}，不可 resume（仅 waiting_human 可 resume）"
        )

    # ── 构造 runnable_config（thread_id 必须与原任务一致，才能命中 checkpointer） ──
    logger = _make_llm_logger(record.trace_id)
    runnable_config = {
        "configurable": {"thread_id": record.thread_id},
        "callbacks": [logger],
    }

    try:
        from langgraph.types import Command
        graph = get_supervisor_graph()
        result = await graph.ainvoke(
            Command(resume=reply),
            config=runnable_config,
        )
    except Exception as e:
        # 上游异常 → 任务落 failed
        try:
            await store.fail(task_id, f"resume 异常：{type(e).__name__}: {e}")
            await cb_client.notify_if_needed(record)
        except Exception:
            pass
        raise llm_upstream_error(f"resume 失败：{e}") from e

    # ── 检测是否仍处于 interrupt（用户回复后又触发新的 ask/confirm） ──
    interrupt_payload = _detect_interrupt(result)
    if interrupt_payload:
        rec = await store.set_interrupt(task_id, interrupt_payload)
        await cb_client.notify_if_needed(rec)
        return rec

    # ── 提取最终输出 ──
    final_output = result.get("final_output") or ""
    if not final_output and result.get("messages"):
        last = result["messages"][-1]
        final_output = getattr(last, "content", str(last))
    plan = result.get("plan") or None

    rec = await store.set_result(
        task_id,
        output=final_output,
        structured=None,
        plan=plan,
        mode="orchestrated",
    )
    await cb_client.notify_if_needed(rec)
    return rec
