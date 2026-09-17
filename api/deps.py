"""FastAPI 依赖：API Key 鉴权 + trace_id 提取。

设计文档 12.1 节：入向鉴权 API Key，配置于环境变量 AI_API_KEYS（逗号分隔）。
仅限内网调用，不做 JWT；启动时 AI_API_KEYS 必须配置，缺失拒绝启动。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Header, Request

from api.errors import unauthorized


def get_api_keys() -> set[str]:
    """从环境变量读取授权 API Key 集合（启动时调用，校验非空）。"""
    raw = os.getenv("AI_API_KEYS", "").strip()
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI 依赖：校验 X-API-Key 或 Authorization: Bearer <key>。

    返回校验通过的 API Key（可用于日志审计）。
    """
    api_keys = get_api_keys()
    if not api_keys:
        # 启动时未配置 → 拒绝所有请求（避免裸奔）
        raise unauthorized("服务端未配置 AI_API_KEYS，拒绝请求")

    # 优先 X-API-Key，其次 Authorization: Bearer
    candidate = None
    if x_api_key:
        candidate = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1]

    if not candidate:
        raise unauthorized("缺少 X-API-Key 或 Authorization: Bearer 头")

    if candidate not in api_keys:
        raise unauthorized("API Key 无效")

    # 把 api_key 挂到 request.state 供后续审计使用
    request.state.api_key = candidate[:8] + "..."  # 只保留前 8 位做审计
    return candidate


def get_trace_id(request: Request) -> str:
    """从 request.state 取出 lifespan 中间件生成的 trace_id。"""
    return getattr(request.state, "trace_id", "") or ""
