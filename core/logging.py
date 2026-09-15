"""LLM 交互日志记录器。

通过 LangChain Callback 机制自动记录全部 LLM 调用与工具执行，
覆盖 supervisor → product_agent → load_skill 的完整嵌套链路。

用法：
    from core.logging import LLMInteractionLogger

    logger = LLMInteractionLogger("logs/llm_trace.log")
    agent.invoke(
        {"messages": [{"role": "user", "content": "..."}]},
        config={"callbacks": [logger]},
    )
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult


class LLMInteractionLogger(BaseCallbackHandler):
    """将 LLM 输入/输出与工具调用/结果写入日志文件。

    通过 parent_run_id 推断嵌套深度，缩进显示调用层级。
    文件写入完整内容，控制台输出超长时截断。
    """

    def __init__(self, log_path: str | Path, *, verbose: bool = True):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # 每次实例化时清空文件，开始新的日志
        self.log_path.write_text("", encoding="utf-8")
        self.verbose = verbose
        self._depth: dict[str, int] = {}  # run_id → 嵌套层级

    # ---------- 工具方法 ----------
    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _indent(self, run_id: Any, parent_run_id: Any) -> str:
        rid = str(run_id) if run_id else None
        pid = str(parent_run_id) if parent_run_id else None
        if rid and rid not in self._depth:
            parent_depth = self._depth.get(pid, 0) if pid else 0
            self._depth[rid] = parent_depth + 1
        depth = self._depth.get(rid, 0) if rid else 0
        return "  " * depth

    def _write(self, text: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        if self.verbose:
            print(text, flush=True)

    @staticmethod
    def _truncate(text: Any, limit: int = 3000) -> str:
        text = str(text) if not isinstance(text, str) else text
        if len(text) > limit:
            return text[:limit] + f"\n  ...(截断，共 {len(text)} 字符)"
        return text

    def _format_messages(self, messages: list[list[BaseMessage]]) -> str:
        lines = []
        for batch in messages:
            for msg in batch:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", str(msg))
                lines.append(f"    [{role}] {self._truncate(content)}")
        return "\n".join(lines)

    # ---------- LLM 回调 ----------
    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: str,
        parent_run_id: str | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        ind = self._indent(run_id, parent_run_id)
        model_name = serialized.get("name", "?") if serialized else "?"
        self._write(f"{ind}[{self._ts()}] LLM_START  model={model_name}  run_id={str(run_id)[:8]}")
        self._write(self._format_messages(messages))
        self._write(f"{ind}{'─' * 60}")

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        ind = self._indent(run_id, parent_run_id)
        for gen_batch in response.generations:
            for gen in gen_batch:
                msg = gen.message
                content = getattr(msg, "content", "")
                tool_calls = getattr(msg, "tool_calls", None) or []

                self._write(f"{ind}[{self._ts()}] LLM_END    run_id={str(run_id)[:8]}")
                if content:
                    self._write(f"{ind}  content: {self._truncate(content)}")
                else:
                    self._write(f"{ind}  content: (空)")
                for tc in tool_calls:
                    tc_name = tc.get("name", "?")
                    tc_args = tc.get("args", {})
                    self._write(f"{ind}  tool_call: {tc_name}({json.dumps(tc_args, ensure_ascii=False)})")

                # token 用量（如果模型返回）
                usage = response.llm_output or {}
                if usage:
                    self._write(f"{ind}  usage: {usage}")
        self._write(f"{ind}{'═' * 60}")

    # ---------- 工具回调 ----------
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        ind = self._indent(run_id, parent_run_id)
        tool_name = serialized.get("name", "?") if serialized else "?"
        self._write(f"{ind}[{self._ts()}] TOOL_START name={tool_name}  run_id={str(run_id)[:8]}")
        self._write(f"{ind}  input: {self._truncate(input_str)}")
        self._write(f"{ind}{'─' * 60}")

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        ind = self._indent(run_id, parent_run_id)
        self._write(f"{ind}[{self._ts()}] TOOL_END   run_id={str(run_id)[:8]}")
        self._write(f"{ind}  output: {self._truncate(output)}")
        self._write(f"{ind}{'═' * 60}")
