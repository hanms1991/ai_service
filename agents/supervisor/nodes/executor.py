"""Executor 节点：按计划调用 worker agent / 直连执行技能 / 中枢自处理。

分支逻辑（与原 supervisor_graph.py 完全一致，仅做模块拆分）：
  - skill 非空 → execute_skill 直连，Agent 角色提示 + 技能模板，一次 LLM 调用完成
  - 未指定 skill   → 走 worker agent 自由文本调用（向后兼容）
  - is_final=True → 直接透传结果并设置 final_output
  - is_final=False → 存入 results[output_key] 继续下一步
  - confirm 模式 → interrupt 暂停等待人工审批
  - ask 模式 → interrupt 暂停等待用户补充信息
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
        from agents.capability_registry import execute_skill

        output = execute_skill(
            tool_name, skill_name, skill_inputs, runnable_config=config
        )
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
