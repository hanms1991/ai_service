"""Executor 节点：按计划调用 worker agent / 直连执行技能 / 中枢自处理。

分支逻辑（与原 supervisor_graph.py 完全一致，仅做模块拆分）：
  - skill 非空且必填齐全 → execute_skill 直连，Agent 角色提示 + 技能模板，一次 LLM 调用完成
  - skill 非空但必填缺失 → 确定性转为 self_handle 对话式澄清（Planner 预校验的第二道兜底）
  - 未指定 skill   → 走 worker agent 自由文本调用（向后兼容）
  - is_final=True → 直接透传结果并设置 final_output
  - is_final=False → 存入 results[output_key] 继续下一步
  - confirm 模式 → interrupt 暂停等待人工审批
  - ask 模式 → （已废弃）历史计划兼容，interrupt 暂停等待用户补充信息
  - compose 模式 → 不调 worker，拼装前序结果为最终输出
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agents.supervisor import constants
from agents.supervisor.nodes.helpers import _call_worker, _resolve_input, _self_handle
from agents.supervisor.state import SupervisorState


def executor_node(
    state: SupervisorState,
    config: RunnableConfig,
) -> dict:
    """Executor 节点：纯代码按计划调用 worker agent / 直连执行技能。

    - 计划指定 skill → execute_skill 直连，Agent 角色提示 + 技能模板，一次 LLM 调用完成
    - 未指定 skill   → 走 worker agent 自由文本调用（向后兼容）
    - is_final=True → 直接透传结果并设置 final_output
    - is_final=False → 存入 results[output_key] 继续下一步
    - confirm 模式 → interrupt 暂停等待人工审批
    - ask 模式 → interrupt 暂停等待用户补充信息
    - compose 模式 → 不调 worker，拼装前序结果为最终输出
    """
    step_index = state["step_index"]
    plan = state["plan"]

    # 防御：计划已执行完毕
    if step_index >= len(plan):
        return {"final_output": state.get("final_output") or "计划已执行完毕。"}

    step = plan[step_index]
    tool_name = step.get("tool", "")
    skill_name = step.get("skill") or ""
    output_key = step["output_key"]
    is_final = step.get("is_final", False)
    mode = step.get("mode", "single")

    prior_results = state.get("results", {})

    # 技能输入：解析值中的 ${output_key} 引用
    skill_inputs = {
        key: _resolve_input(str(value), prior_results)
        for key, value in (step.get("inputs") or {}).items()
    }
    # 自由文本输入（未指定 skill 时使用）
    resolved_input = _resolve_input(step.get("input", ""), prior_results)

    # ── compose 模式：不调 worker，直接拼装前序结果 ──
    if mode == "compose":
        # 优先用 input 拼装模板（其中 ${output_key} 已解析）；
        # 模板为空时，按 inputs 中各引用片段的声明顺序拼接
        if resolved_input.strip():
            composed = resolved_input
        else:
            composed = "\n\n".join(
                str(v).strip() for v in skill_inputs.values() if str(v).strip()
            )
        return {
            "final_output": composed,
            "step_index": step_index + 1,
            "messages": [AIMessage(content=composed)],
        }

    # ── ask 模式：interrupt 等待用户补充信息 ──
    if mode == "ask":
        user_reply = interrupt({
            "step_id": step["step_id"],
            "message": resolved_input,
        })
        # 用户回复后，把回复拼入输入继续执行
        resolved_input = f"{resolved_input}\n用户补充信息：{user_reply}"

    # ── confirm 模式：interrupt 等待人工审批 ──
    if mode in constants.HIGH_RISK_MODES:
        approval = interrupt({
            "step_id": step["step_id"],
            "tool": tool_name,
            "skill": skill_name,
            "input": resolved_input or skill_inputs,
            "message": f"步骤 {step['step_id']} 需要人工确认。输入 'yes' 继续，'no' 取消。",
        })
        if approval != "yes":
            return {
                "final_output": f"步骤 {step['step_id']} 已被人工拒绝，执行终止。",
                "step_index": step_index + 1,
                "needs_human": False,
            }

    # ── 执行业务步骤 ──
    if skill_name:
        # tool+skill 一步直达：无需 worker 内部多轮选择技能
        from agents.capability_registry import (
            describe_skill_params,
            execute_skill,
            get_missing_required_inputs,
            validate_binding,
        )

        # 兜底防线：Planner 出口预校验理论上已拦截缺参计划，
        # 此处确保任何漏网情况都转为对话式澄清，而不是抛异常导致请求 500
        missing = get_missing_required_inputs(tool_name, skill_name, skill_inputs)
        if missing:
            try:
                skill_cfg = validate_binding(tool_name, skill_name)
                display = skill_cfg.get("display_name") or skill_name
                param_text = describe_skill_params(skill_cfg, missing)
                clarify_task = (
                    f"用户希望执行「{display}」，但必要信息尚不完整，还缺少：{param_text}。"
                    f"请以你的口吻向用户追问这些信息，一次只问必要内容并可给出简短示例；"
                    f"不要提及任何内部执行机制。"
                )
            except KeyError:
                clarify_task = (
                    f"当前任务缺少必要信息（{', '.join(missing)}），"
                    f"请向用户追问补充后再继续。"
                )
            print(
                f"[executor] 技能 {skill_name} 缺少必填输入 {missing}，"
                f"转为对话式澄清（Planner 预校验漏网兜底）"
            )
            output = _self_handle(clarify_task, config)
        else:
            from agents.capability_registry import SkillInputError

            try:
                output = execute_skill(
                    tool_name, skill_name, skill_inputs, runnable_config=config
                )
            except SkillInputError as e:
                # file_id 文档不存在/已过期/无法解析：对话式提示用户重新上传或改用文字描述
                print(f"[executor] 技能 {skill_name} 输入文档读取失败：{e}")
                display = skill_name
                try:
                    skill_cfg = validate_binding(tool_name, skill_name)
                    display = skill_cfg.get("display_name") or skill_name
                except KeyError:
                    pass
                clarify_task = (
                    f"执行「{display}」前读取用户上传的文档失败：{e} "
                    "请以你的口吻告知用户文档无法读取（可能已过期、损坏或为扫描件），"
                    "请用户重新上传相关项定义文档（docx/xlsx/pdf），或直接用文字描述分析对象的"
                    "范围、功能与边界后重试。不要提及任何内部执行机制。"
                )
                output = _self_handle(clarify_task, config)
            except Exception as e:  # noqa: BLE001 - 执行链最后防线，避免请求 500
                print(f"[executor] 技能 {skill_name} 执行失败：{type(e).__name__}: {e}")
                display = skill_name
                try:
                    skill_cfg = validate_binding(tool_name, skill_name)
                    display = skill_cfg.get("display_name") or skill_name
                except KeyError:
                    pass
                failure_task = (
                    f"执行「{display}」时未能完成（{type(e).__name__}）。"
                    "请以你的口吻简短告知用户本次分析未能完成，可建议用户："
                    "1）若提供了文档，确认文档内容与分析目标匹配后重试；"
                    "2）用文字补充更明确的相关项范围/功能描述后重试。"
                    f"内部错误摘要（不要原样转述技术细节）：{str(e)[:200]}"
                )
                output = _self_handle(failure_task, config)
    elif tool_name:
        # 指定了 tool 但未指定 skill：交给 worker agent 通用对话
        output = _call_worker(tool_name, resolved_input, config)
    else:
        # tool 也为空：中枢自处理闲聊/通用问答
        output = _self_handle(resolved_input, config)

    # 更新状态
    new_results = {**state.get("results", {}), output_key: output}
    updates: dict[str, Any] = {
        "results": new_results,
        "step_index": step_index + 1,
    }

    if is_final:
        # is_final=True → 直接透传结果并结束
        updates["final_output"] = output
        updates["messages"] = [AIMessage(content=output)]
        updates["needs_human"] = False
    else:
        # is_final=False → 存入 state 继续下一步
        updates["messages"] = [AIMessage(content=f"[步骤 {step['step_id']} 完成，结果存入 {output_key}]")]

    return updates
