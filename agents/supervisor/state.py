"""Supervisor 图的全局状态定义。"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing import TypedDict


class SupervisorState(TypedDict):
    """Supervisor 图的全局状态。

    messages:  对话历史（add_messages 自动累加，Checkpointer 跨轮持久化）
    route:     intent_router 的分类结果（"chat" | "task"）
    plan:      planner 产出的步骤列表（dict 形式）
    step_index: 当前执行到第几步
    results:   {output_key: result_content} 已完成步骤的输出
    final_output: 最终输出，None 表示尚未产出
    needs_human: 是否正在等待人工确认（供外部判断）
    """
    messages: Annotated[list[BaseMessage], add_messages]
    route: str
    plan: list[dict]
    step_index: int
    results: dict[str, str]
    final_output: str | None
    needs_human: bool
