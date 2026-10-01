# -*- coding: utf-8 -*-
"""Web 端（/chat 页面）专用 API 路由，前缀 /ui/api。

与对外 /api/v1 的区别：
  - 鉴权用服务端会话 Cookie（require_web_user），浏览器不持有真实 AI_API_KEY；
  - 会话/消息/文件归属按登录用户隔离，服务端落 SQLite；
  - /conversations/{id}/chat 是流式代理：内部调用 agent_runner.run_invoke_stream，
    NDJSON 原样透传给浏览器，同时在服务端累积消息落库、登记新产生的交付物。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from api.errors import ApiError, file_not_found, file_too_large, file_type_not_allowed
from api.services import agent_runner, web_store
from api.services.scene_resolver import get_scene_resolver
from api.services.web_auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    authenticate,
    get_user_by_id,
    parse_token,
)
from core.file_sandbox import (
    FileSizeExceeded,
    FileTypeNotAllowed,
    UploadedFileNotFound,
    allowed_extensions,
    delete_file,
    get_meta,
    list_files,
    resolve_stored_path,
    save_upload,
)

router = APIRouter(prefix="/ui/api", tags=["webchat"])


# ────────────────────────────────────────────────────────────────────
# 鉴权依赖
# ────────────────────────────────────────────────────────────────────

async def require_web_user(request: Request) -> dict[str, Any]:
    """从 httpOnly Cookie 解析登录用户；失败抛 401。"""
    token = request.cookies.get(COOKIE_NAME)
    user_id = parse_token(token)
    user = get_user_by_id(user_id) if user_id else None
    if not user:
        raise ApiError(code="WEB_UNAUTHORIZED", message="未登录或登录已过期", http_status=401)
    return user


# ────────────────────────────────────────────────────────────────────
# 请求体
# ────────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str


class NewConvBody(BaseModel):
    title: str | None = None
    scene: str | None = ""


class RenameBody(BaseModel):
    title: str


class ChatBody(BaseModel):
    message: str
    scene: str | None = ""
    # 本次发送携带的上传文档 file_id 列表（前端自动填入，用户不可见）
    file_ids: list[str] | None = None


# ────────────────────────────────────────────────────────────────────
# 登录 / 登出 / 当前用户 / 场景
# ────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginBody) -> JSONResponse:
    from api.services.web_auth import issue_token

    user = authenticate(body.username, body.password)
    if not user:
        raise ApiError(code="WEB_LOGIN_FAILED", message="用户名或密码错误", http_status=401)
    token = issue_token(user["id"])
    resp = JSONResponse({"user": {"id": user["id"], "username": user["username"]}})
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/",
    )
    return resp


@router.post("/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me")
async def me(user: dict = Depends(require_web_user)) -> dict:
    return {"user": user}


@router.get("/scenes")
async def scenes(_: dict = Depends(require_web_user)) -> dict:
    """可选场景码（新建会话时的下拉项；空串=智能编排）。"""
    try:
        bindings = get_scene_resolver().list_scenes()
        items = [
            {"scene": b.scene, "description": b.description or b.scene}
            for b in bindings
        ]
    except Exception:
        items = []
    return {"scenes": items}


# ────────────────────────────────────────────────────────────────────
# 会话 CRUD
# ────────────────────────────────────────────────────────────────────

@router.get("/conversations")
async def conv_list(user: dict = Depends(require_web_user)) -> dict:
    return {"conversations": web_store.list_conversations(user["id"])}


@router.post("/conversations")
async def conv_create(body: NewConvBody, user: dict = Depends(require_web_user)) -> dict:
    conv = web_store.create_conversation(
        user["id"], title=(body.title or "新会话"), scene=(body.scene or "")
    )
    return {"conversation": conv}


@router.patch("/conversations/{conv_id}")
async def conv_rename(conv_id: str, body: RenameBody, user: dict = Depends(require_web_user)) -> dict:
    if not web_store.rename_conversation(user["id"], conv_id, body.title):
        raise ApiError(code="CONV_NOT_FOUND", message="会话不存在", http_status=404)
    return {"ok": True}


@router.delete("/conversations/{conv_id}")
async def conv_delete(conv_id: str, user: dict = Depends(require_web_user)) -> dict:
    if not web_store.delete_conversation(user["id"], conv_id):
        raise ApiError(code="CONV_NOT_FOUND", message="会话不存在", http_status=404)
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages")
async def conv_messages(conv_id: str, user: dict = Depends(require_web_user)) -> dict:
    if not web_store.get_conversation(user["id"], conv_id):
        raise ApiError(code="CONV_NOT_FOUND", message="会话不存在", http_status=404)
    return {"messages": web_store.list_messages(user["id"], conv_id)}


# ────────────────────────────────────────────────────────────────────
# 流式对话代理
# ────────────────────────────────────────────────────────────────────

@router.post("/conversations/{conv_id}/chat")
async def conv_chat(conv_id: str, body: ChatBody, user: dict = Depends(require_web_user)):
    conv = web_store.get_conversation(user["id"], conv_id)
    if not conv:
        raise ApiError(code="CONV_NOT_FOUND", message="会话不存在", http_status=404)
    message = (body.message or "").strip()
    if not message:
        raise ApiError(code="MESSAGE_EMPTY", message="消息不能为空", http_status=400)

    # 场景：以本次请求为准（允许切换），并回写会话
    scene = (body.scene or conv.get("scene") or "").strip()
    if scene != (conv.get("scene") or ""):
        web_store.set_scene(user["id"], conv_id, scene)

    # 用户消息先落库；首条消息自动生成标题
    web_store.add_message(user["id"], conv_id, "user", message)
    web_store.set_first_title(user["id"], conv_id, message)

    # 交付物快照：执行前后文件沙箱新增的 file_id 归属于本会话
    before_ids = {m.file_id for m in list_files()}

    async def event_stream():
        done_parts: list[str] = []    # done.output（graph 终态，含技能交付摘要）
        token_parts: list[str] = []  # 可见 token（纯对话兜底；技能 JSON 已被过滤）
        error_obj: dict[str, Any] | None = None
        stopped = False

        def _register_artifacts() -> None:
            # 新产生的沙箱文件登记为该会话的交付物（低并发内网工具，快照差分足够可靠）
            after_metas = {m.file_id: m for m in list_files()}
            for fid in set(after_metas) - before_ids:
                meta = after_metas[fid]
                web_store.register_file(
                    user["id"], fid, "artifact",
                    conversation_id=conv_id, filename=meta.original_name,
                )

        try:
            stream = agent_runner.run_invoke_stream(
                message=message,
                scene=scene or None,
                inputs=None,
                thread_id=conv["thread_id"],
                context={"username": user["username"]},
                reference_data=None,
                response_format=None,
                timeout_seconds=None,
                trace_id=None,
            )
            async for line in stream:
                # 原样透传，同时解析用于落库
                try:
                    evt = json.loads(line)
                except (ValueError, TypeError):
                    evt = {}
                etype = evt.get("type")
                if etype == "done":
                    done_parts.append(str(evt.get("output") or ""))
                elif etype == "token":
                    token_parts.append(str(evt.get("content") or ""))
                elif etype == "error":
                    error_obj = evt
                yield line
        except (asyncio.CancelledError, GeneratorExit):
            # 用户点了「停止」/ 浏览器关闭连接：取消会传播到 graph 执行
            # （任务取消抛 CancelledError；部分 ASGI 清理路径注入 GeneratorExit）
            stopped = True
            raise
        except ApiError as e:
            error_obj = {"code": e.code, "message": e.message}
            yield json.dumps({"type": "error", "code": e.code, "message": e.message},
                             ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 —— 代理层兜底，保证前端拿到 error 事件
            error_obj = {"code": "STREAM_FAILED", "message": str(e)}
            yield json.dumps({"type": "error", "code": "STREAM_FAILED", "message": str(e)},
                             ensure_ascii=False) + "\n"
        finally:
            # 助手消息落库（正常完成/失败/被停止均留痕）。取消期间用 shield 保护
            # 这次毫秒级 SQLite 写入不随连接取消而中断。
            final_text = ("".join(done_parts) or "".join(token_parts)).strip()
            try:
                if stopped:
                    if final_text:
                        final_text += "\n\n（用户已停止生成，以上为已输出的部分内容）"
                    else:
                        final_text = "（用户已停止生成）"
                    await asyncio.shield(
                        asyncio.to_thread(
                            web_store.add_message, user["id"], conv_id, "assistant", final_text
                        )
                    )
                elif error_obj and not final_text:
                    await asyncio.shield(
                        asyncio.to_thread(
                            web_store.add_message,
                            user["id"], conv_id, "assistant",
                            f"[{error_obj.get('code', 'ERROR')}] "
                            f"{error_obj.get('message', '执行失败')}",
                            is_error=True,
                        )
                    )
                elif final_text:
                    await asyncio.shield(
                        asyncio.to_thread(
                            web_store.add_message, user["id"], conv_id, "assistant", final_text
                        )
                    )
                # 已产生的交付物同样登记（停止时可能已渲染完文件）
                await asyncio.shield(asyncio.to_thread(_register_artifacts))
            except Exception:
                pass  # 清理失败不影响响应结束/取消传播

    return _ndjson_response(event_stream())


def _ndjson_response(generator):
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        generator,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ────────────────────────────────────────────────────────────────────
# 文件上传 / 列表 / 下载 / 删除（归属隔离）
# ────────────────────────────────────────────────────────────────────

@router.post("/files/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(require_web_user)) -> dict:
    content = await file.read()
    try:
        meta = save_upload(
            filename=file.filename or "",
            content=content,
            content_type=file.content_type or "",
        )
    except FileSizeExceeded as e:
        raise file_too_large(e.size, e.limit)
    except FileTypeNotAllowed as e:
        raise file_type_not_allowed(e.ext, allowed_extensions())
    web_store.register_file(user["id"], meta.file_id, "upload", filename=meta.original_name)
    return {"file": meta.to_jsonable()}


@router.get("/files")
async def files_list(
    conversation_id: str | None = None,
    kind: str | None = None,
    user: dict = Depends(require_web_user),
) -> dict:
    return {
        "files": web_store.list_files(user["id"], conversation_id=conversation_id, kind=kind),
        "limits": {
            "max_upload_mb": 20,
            "allowed_extensions": allowed_extensions(),
        },
    }


@router.get("/files/{file_id}/download")
async def files_download(file_id: str, user: dict = Depends(require_web_user)) -> FileResponse:
    if not web_store.owns_file(user["id"], file_id):
        raise ApiError(code="FILE_FORBIDDEN", message="无权访问该文件", http_status=403)
    try:
        meta = get_meta(file_id)
        path = resolve_stored_path(file_id)
    except UploadedFileNotFound:
        raise file_not_found(file_id)
    return FileResponse(
        path=str(path),
        media_type=meta.content_type or "application/octet-stream",
        filename=meta.original_name,
    )


@router.delete("/files/{file_id}")
async def files_remove(file_id: str, user: dict = Depends(require_web_user)) -> dict:
    if not web_store.owns_file(user["id"], file_id):
        raise ApiError(code="FILE_FORBIDDEN", message="无权删除该文件", http_status=403)
    try:
        delete_file(file_id)
    except UploadedFileNotFound:
        raise file_not_found(file_id)
    web_store.remove_file_row(user["id"], file_id)
    return {"ok": True, "file_id": file_id}
