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
    scene:     场景码（前端按钮 hint，空字符串表示无 hint）
    hint_agent: 场景码解析出的 agent 名（hint，非强制）
    hint_skill: 场景码解析出的 skill 名（hint，非强制）
    plan:      planner 产出的步骤列表（dict 形式）
    step_index: 当前执行到第几步
    results:   {output_key: result_content} 已完成步骤的输出
    final_output: 最终输出，None 表示尚未产出
    needs_human: 是否正在等待人工确认（供外部判断）
    """
    messages: Annotated[list[BaseMessage], add_messages]
    route: str
    scene: str
    hint_agent: str
    hint_skill: str
    plan: list[dict]
    step_index: int
    results: dict[str, str]
    final_output: str | None
    needs_human: bool
