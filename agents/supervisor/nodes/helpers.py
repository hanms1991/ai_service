"""Executor 节点的辅助函数：输入引用解析、worker 调用、中枢自处理。

独立成模块的原因：让 executor.py 只关注分支调度，把"如何调 worker /
如何调 LLM 兜底"的细节集中在 helpers.py，方便单独测试与替换。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import model


# 包根：agents/supervisor/（本文件在 agents/supervisor/nodes/ 下，回退两级）
_PKG_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _PKG_ROOT / "prompts"

# 模块级缓存：与 planner.py 同风格
_TEMPLATE_CACHE: dict[str, str] = {}


def _load_self_handle_prompt() -> str:
    """加载中枢自处理闲聊的 system prompt（带模块级缓存）。"""
    if "self_handle" not in _TEMPLATE_CACHE:
        path = _PROMPTS_DIR / "self_handle_prompt.yaml"
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        template = data.get("template")
        if not isinstance(template, str) or not template.strip():
            raise ValueError(
                f"self_handle 提示词配置 {path} 缺少 template 字段或为空"
            )
        _TEMPLATE_CACHE["self_handle"] = template
    return _TEMPLATE_CACHE["self_handle"]


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


def _self_handle(task: str, runnable_config=None) -> str:
    """中枢自行处理闲聊/通用问答：用核心模型直接回答，不派给 worker agent。"""
    invoke_kwargs = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config
    response = model.invoke(
        [SystemMessage(content=_load_self_handle_prompt()), HumanMessage(content=task)],
        **invoke_kwargs,
    )
    return response.content
