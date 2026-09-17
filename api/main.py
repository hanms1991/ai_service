"""FastAPI 应用入口。

启动：python -m api.main  或  uvicorn api.main:app --host 0.0.0.0 --port 8300

lifespan 启动时：
  1. 校验 AI_API_KEYS 已配置（否则拒绝启动）
  2. 初始化 SceneResolver（加载 scenes.yaml + 交叉校验注册表）
  3. 构建 supervisor_agent.yaml + AsyncSqliteSaver 单例图
  4. 把图注入 agent_runner

中间件：
  - trace_id：请求头 X-Trace-Id 优先；否则自动生成；写入 request.state

异常归一化：
  - ApiError → 设计文档第 14 节统一错误响应体
  - 未捕获异常 → INTERNAL_ERROR + trace_id
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.deps import get_api_keys
from api.errors import ApiError, internal_error
from api.routes import capabilities, invoke, tasks
from api.services.agent_runner import set_supervisor_graph
from api.services.callbacks import CallbackClient, load_whitelist_from_env, set_callback_client
from api.services.scene_resolver import init_scene_resolver
from api.services.task_store import TaskStore, set_task_store

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# 1. lifespan：启动构建单例图
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan：启动期加载与单例构建。"""
    # ── 1. 校验 AI_API_KEYS ──
    api_keys = get_api_keys()
    if not api_keys:
        raise RuntimeError(
            "AI_API_KEYS 环境变量未配置或为空。请在 .env 中设置逗号分隔的接入方密钥；"
            "缺失拒绝启动，避免裸奔。"
        )

    # ── 2. 初始化 SceneResolver（加载 scenes.yaml + 交叉校验注册表） ──
    resolver = init_scene_resolver()
    scene_count = len(resolver.list_scenes())

    # ── 2b. 初始化 TaskStore + CallbackClient（M2） ──
    ttl_hours = int(os.getenv("TASK_RESULT_TTL_HOURS", "24"))
    task_store = TaskStore(ttl_seconds=ttl_hours * 3600)
    await task_store.start_cleanup_loop()
    set_task_store(task_store)

    callback_timeout = float(os.getenv("CALLBACK_TIMEOUT_SECONDS", "10"))
    cb_client = CallbackClient(
        whitelist=load_whitelist_from_env(),
        timeout_seconds=callback_timeout,
    )
    await cb_client.__aenter__()
    set_callback_client(cb_client)

    # ── 3. 构建 supervisor_agent.yaml + AsyncSqliteSaver 单例图 ──
    supervisor_yaml_path = PROJECT_ROOT / "agents" / "configs" / "supervisor_agent.yaml"
    with supervisor_yaml_path.open("r", encoding="utf-8") as f:
        supervisor_cfg = yaml.safe_load(f) or {}

    # 注入 AsyncSqliteSaver：跨进程持久化、支持多轮记忆
    checkpoint_db = os.getenv("CHECKPOINT_DB", str(PROJECT_ROOT / "data" / "checkpoints.db"))
    Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from agents.supervisor_graph import build_supervisor_graph

    # AsyncSqliteSaver.from_conn_string 返回 async context manager
    # 进入时建立连接并自动 setup()，退出时关闭连接
    async with AsyncSqliteSaver.from_conn_string(checkpoint_db) as checkpointer:
        graph = build_supervisor_graph(
            supervisor_cfg,
            base_dir=supervisor_yaml_path.parent,
            checkpointer=checkpointer,
        )
        set_supervisor_graph(graph)

        print(f"[lifespan] supervisor 图已构建，checkpointer=AsyncSqliteSaver({checkpoint_db})")
        print(f"[lifespan] 场景码已加载：{scene_count} 个")
        print(f"[lifespan] TaskStore 已启动，TTL={ttl_hours}h，清理周期 60s")
        print(f"[lifespan] CallbackClient 已启动，超时={callback_timeout}s，"
              f"白名单={load_whitelist_from_env() or '(空，禁止外呼)'}")
        print(f"[lifespan] API 服务就绪：host={os.getenv('AGENT_HOST', '0.0.0.0')} "
              f"port={os.getenv('AGENT_PORT', '8300')}")

        yield

        # ── 关闭顺序：先停后台任务清理协程，再关 callback client，最后释放 SQLite ──
        print("[lifespan] 服务关闭中…")
        await task_store.stop_cleanup_loop()
        await cb_client.__aexit__(None, None, None)
        # async context 退出时自动关闭 SQLite 连接
        print("[lifespan] SQLite 连接已释放。")


# ════════════════════════════════════════════════════════════════
# 2. 中间件
# ════════════════════════════════════════════════════════════════

class TraceIdMiddleware(BaseHTTPMiddleware):
    """每个请求生成/透传 trace_id，写入 request.state.trace_id。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = (
            request.headers.get("X-Trace-Id")
            or request.headers.get("X-Request-Id")
            or f"trace-{uuid.uuid4().hex[:16]}"
        )
        request.state.trace_id = trace_id

        response = await call_next(request)
        # 路由可能在处理过程中更新了 request.state.trace_id（如请求体里的 trace_id），
        # 响应头取最新的 request.state.trace_id，保证与响应体里的 trace_id 一致
        response.headers["X-Trace-Id"] = getattr(request.state, "trace_id", trace_id)
        return response


# ════════════════════════════════════════════════════════════════
# 3. 异常归一化
# ════════════════════════════════════════════════════════════════

async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """ApiError → 设计文档第 14 节统一错误响应体。"""
    trace_id = getattr(request.state, "trace_id", "") or ""
    if exc.trace_id is None:
        exc.trace_id = trace_id
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "task_id": exc.task_id,
                "trace_id": exc.trace_id,
                "details": exc.details or None,
            }
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常 → INTERNAL_ERROR + trace_id，避免裸栈外泄。"""
    trace_id = getattr(request.state, "trace_id", "") or ""
    # 写入 llm_trace.log 便于排查
    import traceback
    try:
        log_path = PROJECT_ROOT / "logs" / "llm_trace.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                f"\n[#trace_id={trace_id} INTERNAL_ERROR]\n"
                f"{traceback.format_exc()}\n"
            )
    except Exception:
        pass

    err = internal_error(f"服务内部错误（trace_id={trace_id}）")
    err.trace_id = trace_id
    return JSONResponse(
        status_code=err.http_status,
        content={
            "error": {
                "code": err.code,
                "message": err.message,
                "task_id": None,
                "trace_id": trace_id,
                "details": None,
            }
        },
    )


# ════════════════════════════════════════════════════════════════
# 4. 健康检查
# ════════════════════════════════════════════════════════════════

def _healthz(request: Request):
    """存活探针（无鉴权）：进程能响应即存活。"""
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok"})


def _readyz(request: Request):
    """就绪探针（无鉴权）：校验 SceneResolver + supervisor 图已加载。"""
    from fastapi.responses import JSONResponse
    from api.services.scene_resolver import get_scene_resolver
    from api.services.agent_runner import get_supervisor_graph

    checks = {"scene_resolver": "ok", "supervisor_graph": "ok"}
    try:
        get_scene_resolver()
    except Exception as e:
        checks["scene_resolver"] = f"fail: {e}"
    try:
        get_supervisor_graph()
    except Exception as e:
        checks["supervisor_graph"] = f"fail: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "not_ready", "checks": checks},
    )


# ════════════════════════════════════════════════════════════════
# 5. app 组装
# ════════════════════════════════════════════════════════════════

def create_app() -> FastAPI:
    """构建 FastAPI app（便于测试）。"""
    app = FastAPI(
        title="中枢 Agent 对外 API",
        version="0.2.0",
        description="设计文档 docs/api_design.md v0.2",
        lifespan=lifespan,
    )

    # 中间件
    app.add_middleware(TraceIdMiddleware)

    # 异常处理器
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # 健康检查（无鉴权，存活/就绪探针）
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/readyz", _readyz, methods=["GET"])

    # 业务路由（带鉴权）
    app.include_router(invoke.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(capabilities.router, prefix="/api/v1")

    return app


app = create_app()


# ════════════════════════════════════════════════════════════════
# 6. 直接运行入口
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("AGENT_PORT", "8300"))
    host = os.getenv("AGENT_HOST", "0.0.0.0")
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=False,  # 单进程生产模式，关闭热重载
    )
