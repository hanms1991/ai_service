"""LangGraph 重构的 Supervisor Agent —— 规划-执行分离架构。

核心设计：
  Planner 节点：LLM 只输出 JSON 执行计划，不产出业务内容。
               计划包含步骤列表，每步指定 tool / input / output_key / is_final / mode。
               支持 single / chain / compose / ask / confirm 五种模式。

  Executor 节点：纯代码按计划调用现有 worker agent（不改 worker）。
                 is_final=True → 直接透传结果并结束。
                 is_final=False → 存入 state.results 继续下一步。

  条件边：根据 final_output 是否设置，决定继续执行还是结束。

  Checkpointer：MemorySaver 持久化 state，支持多轮对话记忆。
  interrupt：confirm 模式的高风险步骤暂停等待人工审批。

图结构：
  START → planner → executor → [route_after_executor]
                           ↑          |
                           └──────────┘ (final_output 未设置 & 还有步骤)
                                      → END (final_output 已设置 或 计划完毕)
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing import TypedDict

from core.llm import model


# ════════════════════════════════════════════════════════════════
# 1. 状态定义
# ════════════════════════════════════════════════════════════════

class PlanStep(BaseModel):
    """执行计划中的单个步骤。"""
    step_id: str = Field(description="步骤唯一标识，如 step_1")
    tool: str = Field(description="要调用的 worker agent 名称，必须取自能力注册表")
    skill: str = Field(
        default="",
        description=(
            "要执行的技能名，必须取自该 tool 在能力注册表中的技能列表；"
            "Executor 据此一步直达，无需 worker 内部再选技能；"
            "compose/ask/confirm 等不执行业务技能的步骤留空"
        ),
    )
    inputs: dict[str, str] | None = Field(
        default=None,
        description=(
            "技能输入参数，键名和取值必须符合技能 inputs 契约；"
            "值中可用 ${output_key} 引用前序步骤输出；必填项缺失时应改用 ask 模式"
        ),
    )
    input: str = Field(
        default="",
        description="未指定 skill 时的自由文本任务输入，可用 ${output_key} 引用前序步骤输出",
    )
    output_key: str = Field(description="本步骤结果的存储键名")
    is_final: bool = Field(default=False, description="是否为最终输出步骤")
    mode: str = Field(
        default="single",
        description=(
            "single: 单步执行 | "
            "chain: 链式，前步输出作后步输入 | "
            "compose: 组合多个结果为最终输出（不调worker，文本拼装）| "
            "ask: 暂停等待用户补充信息 | "
            "confirm: 高风险，暂停等待人工确认"
        ),
    )


class PlanSchema(BaseModel):
    """LLM 输出的完整执行计划（用于 with_structured_output）。"""
    steps: list[PlanStep] = Field(description="按顺序执行的步骤列表")


class SupervisorState(TypedDict):
    """Supervisor 图的全局状态。

    messages:  对话历史（add_messages 自动累加，Checkpointer 跨轮持久化）
    plan:      planner 产出的步骤列表（dict 形式）
    step_index: 当前执行到第几步
    results:   {output_key: result_content} 已完成步骤的输出
    final_output: 最终输出，None 表示尚未产出
    needs_human: 是否正在等待人工确认（供外部判断）
    """
    messages: Annotated[list[BaseMessage], add_messages]
    plan: list[dict]
    step_index: int
    results: dict[str, str]
    final_output: str | None
    needs_human: bool


# ════════════════════════════════════════════════════════════════
# 2. 能力注册表（唯一事实来源，从 agents/configs + skills 的 yaml 加载）
# ════════════════════════════════════════════════════════════════

# 需要人工审批的模式
HIGH_RISK_MODES = {"confirm"}

# supervisor YAML 中的 system_prompt（分派规则），作为 planner 额外指令
_EXTRA_INSTRUCTIONS: str = ""


def init_capability_registry(config: dict) -> None:
    """从 supervisor YAML 的成员清单初始化能力注册表（含 Agent↔技能绑定与校验）。"""
    from agents.capability_registry import init_registry_from_supervisor

    global _EXTRA_INSTRUCTIONS
    init_registry_from_supervisor(config)
    _EXTRA_INSTRUCTIONS = config.get("system_prompt", "") or ""


# ════════════════════════════════════════════════════════════════
# 3. Planner 节点
# ════════════════════════════════════════════════════════════════

def _build_planner_prompt(extra_instructions: str = "") -> str:
    """根据能力注册表动态生成 planner 的系统提示（注册表是唯一事实来源）。"""
    from agents.capability_registry import render_planner_catalog

    catalog = render_planner_catalog()
    return f"""你是汽车电子智能平台的总调度（Planner）。

职责：分析用户意图，生成 JSON 执行计划。你只负责规划，绝不产出任何业务内容。

可用能力目录（唯一事实来源，tool / skill / inputs 必须严格取自此处，禁止臆造）：
{catalog}

执行计划模式说明：
- single:   单步执行，选定 tool+skill 一步完成任务，is_final=true
- chain:    链式执行，前一步输出作为下一步技能输入（inputs 的值用 ${{output_key}} 引用）
- compose:  组合多个步骤的输出为最终结果（不调 worker，模板拼装，is_final=true）
- ask:      用户意图不清或技能必填输入缺失，暂停等待用户补充信息（is_final=false）
- confirm:  高风险操作（技能风险=high），暂停等待人工确认后才执行（is_final=false）

输出格式：输出紧凑 JSON，不要换行和缩进，不要附加任何解释文字或 markdown 标记。
{{
  "steps": [
    {{
      "step_id": "step_1",
      "tool": "能力目录中的 Agent 名",
      "skill": "该 Agent 技能列表中的技能名",
      "inputs": {{"参数名": "参数值，可引用 ${{prev_key}}"}},
      "input": "仅在未指定 skill 时使用：自由文本任务描述",
      "output_key": "result_1",
      "is_final": true,
      "mode": "single"
    }}
  ]
}}

规划规则：
1. tool 必须与 skill 配对：先按能力描述选定 skill，其所属 Agent 即 tool；不要凭 Agent 摘要猜测输出形态。
2. inputs 的键必须与技能输入契约完全一致，必填参数必须给出；用户未提供且无法推断时，先用 ask 模式追问，禁止编造。
3. 至少一个步骤的 is_final 为 true；引用前序结果一律用 ${{output_key}} 格式。
4. 技能风险=high 时必须用 confirm 模式；风险=low 直接执行。
5. 不要自己编造业务内容，所有产出必须通过 tool+skill 完成或 compose 拼装。
{extra_instructions}"""


def planner_node(state: SupervisorState) -> dict:
    """Planner 节点：LLM 只输出 JSON 执行计划，不产出业务内容。"""
    # 用 json_mode（response_format=json_object），兼容不支持 json_schema 的端点（如 DeepSeek）
    # 提示词已强约束输出格式，Pydantic 做二次校验
    structured_model = model.with_structured_output(
        PlanSchema, method="json_mode"
    )

    prompt = _build_planner_prompt(_EXTRA_INSTRUCTIONS)
    messages = [SystemMessage(content=prompt)] + state["messages"]

    plan: PlanSchema = structured_model.invoke(messages)

    return {
        "plan": [step.model_dump() for step in plan.steps],
        "step_index": 0,
        "results": {},
        "final_output": None,
        "needs_human": False,
    }


# ════════════════════════════════════════════════════════════════
# 4. Executor 节点
# ════════════════════════════════════════════════════════════════

def _resolve_input(text: str, results: dict[str, str]) -> str:
    """解析输入中的 ${output_key} 引用，替换为实际结果。"""
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        return results.get(key, f"[未找到结果: {key}]")
    return re.sub(r"\$\{(\w+)\}", _replacer, text)


def _call_worker(tool_name: str, task: str, runnable_config=None) -> str:
    """调用现有 worker agent，返回其最终文本输出。worker 不做任何修改。"""
    from agents.registry import get_worker

    worker = get_worker(tool_name)
    invoke_kwargs = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config
    result = worker.invoke(
        {"messages": [HumanMessage(content=task)]},
        **invoke_kwargs,
    )
    return result["messages"][-1].content


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
    tool_name = step["tool"]
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
        return {
            "final_output": resolved_input,
            "step_index": step_index + 1,
            "messages": [AIMessage(content=resolved_input)],
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
    if mode in HIGH_RISK_MODES:
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
        from agents.capability_registry import execute_skill

        output = execute_skill(
            tool_name, skill_name, skill_inputs, runnable_config=config
        )
    else:
        # 向后兼容：未指定技能，交给 worker agent 自行决策
        output = _call_worker(tool_name, resolved_input, config)

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


# ════════════════════════════════════════════════════════════════
# 5. 条件边：路由
# ════════════════════════════════════════════════════════════════

def route_after_executor(
    state: SupervisorState,
) -> Literal["executor", "__end__"]:
    """根据 final_output 是否设置决定继续执行还是结束。"""
    # 1. final_output 已设置 → 结束
    if state.get("final_output"):
        return END

    # 2. 还有未执行步骤 → 继续执行
    if state["step_index"] < len(state["plan"]):
        return "executor"

    # 3. 计划执行完毕但没有 final 步骤 → 结束
    return END


# ════════════════════════════════════════════════════════════════
# 6. 图组装
# ════════════════════════════════════════════════════════════════

def build_supervisor_graph(config: dict, base_dir=None) -> Any:
    """构建规划-执行分离的 Supervisor LangGraph。

    Args:
        config:   supervisor YAML 配置字典
        base_dir: YAML 文件所在目录（用于解析 system_prompt 外部文件引用）

    Returns:
        编译后的 CompiledStateGraph，支持 invoke / stream，
        内置 MemorySaver checkpointer 支持多轮记忆。
        调用时需传 config={"configurable": {"thread_id": "xxx"}}。
    """
    # 初始化能力注册表（agents/configs + skills yaml 绑定校验，planner 目录据此生成）
    init_capability_registry(config)

    # ── 构建 StateGraph ──
    graph = StateGraph(SupervisorState)

    # 添加节点
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)

    # 添加边
    graph.add_edge(START, "planner")          # 入口 → planner
    graph.add_edge("planner", "executor")      # planner → executor
    graph.add_conditional_edges(              # executor → 条件路由
        "executor",
        route_after_executor,
        # 路由目标：继续执行 or 结束
        {
            "executor": "executor",
            END: END,
        },
    )

    # ── 编译图，带 Checkpointer 支持多轮记忆 ──
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
