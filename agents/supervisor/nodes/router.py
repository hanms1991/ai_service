"""条件边：根据 final_output 是否设置决定继续执行还是结束。"""
from __future__ import annotations

from typing import Literal

from langgraph.graph import END

from agents.supervisor.state import SupervisorState


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
