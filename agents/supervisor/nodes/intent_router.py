"""Intent Router 节点 + Chat 节点。

Intent Router 在 Planner 之前做轻量意图分类：
  - Layer 1: 规则快路径（正则/关键词），命中闲聊模式直接返回 chat
  - Layer 2: LLM 分类器，输出 chat 或 task

路由结果：
  - chat → chat_node（self_handle 直接回复，跳过 planner）
  - task → planner（现有规划-执行流程不变）
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from langchain_core.messages import SystemMessage

from agents.supervisor.state import SupervisorState
from core.llm import model


# ── Layer 1: 规则快路径 ──
# 仅匹配明显的闲聊模式，保守匹配（宁可不命中也不误判）
_CHAT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r'^(你好|您好|hi|hello|hey|嗨|哈喽)\s*[！!。.~]?$',
        r'^(谢谢|感谢|thanks|thank\s+you|多谢|辛苦了)\s*[！!。.~]?$',
        r'^(再见|bye|拜拜|886)\s*[！!。.~]?$',
        r'^(今天|现在).*(星期几|几号|日期|时间|几点)',
        r'^(你是谁|你叫什么|你能做什么|有什么功能|帮助)$',
    )
]


def _is_small_talk(text: str) -> bool:
    """规则快路径：匹配明显的闲聊模式。"""
    text = text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _CHAT_PATTERNS)


# ── Layer 2: LLM 分类器 ──
_PKG_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _PKG_ROOT / "prompts"
_TEMPLATE_CACHE: dict[str, str] = {}


def _load_intent_router_prompt() -> str:
    """加载意图分类器的 system prompt（带模块级缓存）。"""
    if "intent_router" not in _TEMPLATE_CACHE:
        path = _PROMPTS_DIR / "intent_router_prompt.yaml"
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        template = data.get("template")
        if not isinstance(template, str) or not template.strip():
            raise ValueError(
                f"intent_router 提示词配置 {path} 缺少 template 字段或为空"
            )
        _TEMPLATE_CACHE["intent_router"] = template
    return _TEMPLATE_CACHE["intent_router"]


def _classify_intent(messages: list) -> str:
    """LLM 轻量分类：输出 'chat' 或 'task'。

    不使用 structured_output，避免 Schema 校验风险。
    仅取最后 3 条消息做上下文，保持低延迟。
    """
    recent = messages[-3:] if len(messages) > 3 else messages
    try:
        response = model.invoke([
            SystemMessage(content=_load_intent_router_prompt()),
            *recent,
        ])
        result = response.content.strip().lower()
        # 宽松解析：只要包含 chat 就判为 chat，否则默认 task
        return "chat" if "chat" in result else "task"
    except Exception:
        # 分类器异常时默认走 task（更安全，planner 有重试兜底）
        return "task"


# ── 节点函数 ──

def intent_router_node(state: SupervisorState) -> dict:
    """意图分类节点：闲聊走快路径，其余走 LLM 分类。"""
    messages = state.get("messages", [])
    if not messages:
        return {"route": "task"}

    user_msg = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

    # Layer 1: 规则快路径
    if _is_small_talk(user_msg):
        return {"route": "chat"}

    # Layer 2: LLM 分类
    route = _classify_intent(messages)
    return {"route": route}


def chat_node(state: SupervisorState, config=None) -> dict:
    """闲聊节点：直接用 self_handle 回复，跳过 planner。"""
    from agents.supervisor.nodes.helpers import _self_handle

    messages = state.get("messages", [])
    user_msg = messages[-1].content if messages and hasattr(messages[-1], "content") else ""

    output = _self_handle(user_msg, config)

    return {
        "final_output": output,
        "messages": [_create_ai_message(output)],
        "needs_human": False,
    }


def _create_ai_message(content: str):
    """创建 AIMessage（延迟导入避免循环依赖）。"""
    from langchain_core.messages import AIMessage
    return AIMessage(content=content)


def route_by_intent(state: SupervisorState) -> str:
    """条件边路由函数：根据 route 字段决定走 chat 还是 planner。"""
    route = state.get("route", "task")
    if route == "chat":
        return "chat"
    return "planner"
