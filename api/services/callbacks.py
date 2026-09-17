"""Webhook 回调客户端（设计文档 7.3 节末、12.3 节）。

设计要点：
  - 仅在任务终态（completed/failed/timeout/cancelled）或 waiting_human 时触发
  - 仅内网域名白名单（白名单从环境变量 CALLBACK_DOMAIN_WHITELIST 读取，逗号分隔）
  - 不做签名（内网信任，决策 7）
  - 超时短（默认 10s）
  - 失败不重试（一期）：避免重复触发，调用方应自己实现幂等
  - 异步执行（asyncio.create_task），不阻塞任务流程

回调载荷：任务完整快照（同 GET /agent/tasks/{task_id} 响应）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx

from api.schemas import TERMINAL_STATES, TaskStatus
from api.services.task_store import TaskRecord

logger = logging.getLogger(__name__)


# 终态 + waiting_human 触发回调
TRIGGER_STATES = TERMINAL_STATES | {TaskStatus.WAITING_HUMAN}


class CallbackClient:
    """Webhook 回调客户端（单进程内单例）。

    用法：lifespan 启动时构造 + set_callback_client 注入；
    agent_runner 在任务状态变化时调用 notify_if_terminal(record)。
    """

    def __init__(
        self,
        whitelist: list[str] | None = None,
        timeout_seconds: float = 10.0,
    ):
        # whitelist 是域名字符串列表（不含 scheme），如 ["backend.local", "192.168.0.1"]
        self._whitelist = {h.lower() for h in (whitelist or [])}
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "CallbackClient":
        # 复用连接池
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=2.0),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_allowed(self, url: str) -> bool:
        """URL 校验：必须在白名单中；禁止外部公网域名。"""
        if not url:
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        # 显式拒绝 IP 字面量形式中的链路本地/环回以外，但仍要求白名单匹配
        # 白名单为空时拒绝所有外呼（避免误配置导致裸调）
        if not self._whitelist:
            return False
        return host in self._whitelist

    async def notify_if_needed(self, record: TaskRecord) -> None:
        """任务终态/中断时触发回调（fire-and-forget）。

        规则：
          - record.callback_url 为空 → 跳过
          - status 不在触发集合 → 跳过
          - 域名不在白名单 → 跳过并记日志（避免裸调外网）
        """
        if not record.callback_url:
            return
        if record.status not in TRIGGER_STATES:
            return
        if not self.is_allowed(record.callback_url):
            logger.warning(
                "[callback] task=%s callback_url=%s 不在白名单，跳过回调",
                record.task_id, record.callback_url,
            )
            return
        # 异步发送，不阻塞任务流
        asyncio.create_task(self._send(record))

    async def _send(self, record: TaskRecord) -> None:
        """实际发送（私有，由 notify_if_needed 调度）。"""
        if self._client is None:
            # 兜底：lifespan 未初始化客户端时按需建一次性 client
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=2.0),
            )
        payload: dict[str, Any] = record.to_detail_dict()
        # 回调载荷加一个 event 字段标识触发类型，方便调用方分支
        payload["event"] = record.status.value
        try:
            resp = await self._client.post(
                record.callback_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Trace-Id": record.trace_id,
                    "X-Task-Id": record.task_id,
                    "User-Agent": "ai-service-callback/0.2",
                },
            )
            logger.info(
                "[callback] task=%s status=%s callback HTTP %s",
                record.task_id, record.status.value, resp.status_code,
            )
        except httpx.TimeoutException:
            logger.warning(
                "[callback] task=%s 回调超时（%ss）", record.task_id, self._timeout,
            )
        except Exception as e:
            logger.warning(
                "[callback] task=%s 回调失败：%s", record.task_id, e,
            )


# ════════════════════════════════════════════════════════════════
# 单例句柄（lifespan 启动时 set_callback_client 注入）
# ════════════════════════════════════════════════════════════════

_client: CallbackClient | None = None


def set_callback_client(client: CallbackClient) -> None:
    global _client
    _client = client


def get_callback_client() -> CallbackClient:
    if _client is None:
        raise RuntimeError("CallbackClient 尚未初始化，请检查 lifespan 是否已启动")
    return _client


def load_whitelist_from_env() -> list[str]:
    """从环境变量 CALLBACK_DOMAIN_WHITELIST 读取域名白名单。"""
    raw = os.getenv("CALLBACK_DOMAIN_WHITELIST", "").strip()
    if not raw:
        return []
    return [h.strip().lower() for h in raw.split(",") if h.strip()]
