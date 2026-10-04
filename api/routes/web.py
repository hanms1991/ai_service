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
import re
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


class PinBody(BaseModel):
    pinned: bool


class ChatBody(BaseModel):
    message: str
    # None=本次请求未携带场景（回退会话记忆的场景，保持会话连续性）；
    # ""=前端显式选择「智能编排」（清空场景，不得回退成旧场景）；
    # 非空串=切换到指定场景。三态必须区分，故默认值是 None 而不是 ""。
    scene: str | None = None
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


@router.get("/models")
async def models_list(_: dict = Depends(require_web_user)) -> dict:
    """可用 LLM 预设列表与当前选中项。"""
    from core import llm

    return {
        "models": llm.list_models(),
        "current": llm.get_current_model_name(),
    }


class ModelSwitchBody(BaseModel):
    name: str


@router.post("/models/current")
async def models_switch(
    body: ModelSwitchBody, _: dict = Depends(require_web_user)
) -> dict:
    """切换当前生效的 LLM 预设（全局生效，影响所有后续请求）。"""
    from core import llm

    try:
        name = llm.set_current_model(body.name)
    except KeyError:
        raise ApiError(code="MODEL_NOT_FOUND", message=f"未知模型预设: {body.name}",
                       http_status=404)
    except ValueError:
        raise ApiError(code="MODEL_DISABLED", message=f"模型预设 {body.name} 已禁用",
                       http_status=400)
    return {"current": name}


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


@router.post("/conversations/{conv_id}/pin")
async def conv_pin(conv_id: str, body: PinBody, user: dict = Depends(require_web_user)) -> dict:
    if not web_store.set_pinned(user["id"], conv_id, body.pinned):
        raise ApiError(code="CONV_NOT_FOUND", message="会话不存在", http_status=404)
    return {"ok": True, "pinned": body.pinned}


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


@router.post("/conversations/{conv_id}/messages/{msg_id}/feedback")
async def set_message_feedback(
    conv_id: str, msg_id: int, body: dict, user: dict = Depends(require_web_user)
) -> dict:
    """设置助手消息反馈：value=1 赞 / -1 踩 / 0 取消。"""
    value = int(body.get("value", 0) or 0)
    if value not in (-1, 0, 1):
        raise ApiError(code="INVALID_VALUE", message="value 只能为 1、-1 或 0",
                       http_status=400)
    ok = web_store.set_feedback(user["id"], conv_id, msg_id, value)
    if not ok:
        raise ApiError(code="MSG_NOT_FOUND", message="消息不存在", http_status=404)
    return {"ok": True}


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

    # 场景：以本次请求为准（允许切换），并回写会话。
    # 注意区分两种"空"：body.scene 为 None（调用方未传该字段）时回退会话记忆；
    # 空串 "" 是前端显式选择「智能编排」，必须尊重，不能被 or 回退成旧场景。
    if body.scene is None:
        scene = (conv.get("scene") or "").strip()
    else:
        scene = body.scene.strip()
    if scene != (conv.get("scene") or ""):
        web_store.set_scene(user["id"], conv_id, scene)

    # ── 附件文档：前端自动携带当前所有已上传 file_id（用户不可见）。
    # 双重注入：
    #   1) file_id 列表 → planner 提取后填入技能 inputs.file_id，技能内部 read_document 读全文；
    #   2) 文档内容预览（截断）→ 走「中枢自处理」路径时 LLM 也能直接看到文档内容，
    #      避免 file_id 被自处理路径忽略导致"读不到文档"。
    agent_message = message
    raw_ids = [str(f).strip() for f in (body.file_ids or []) if str(f).strip()]
    if raw_ids:
        owned: list[str] = []
        for fid in raw_ids:
            if web_store.owns_file(user["id"], fid):
                owned.append(fid)
        if owned:
            from tools.read_document import read_document as _read_doc

            previews: list[str] = []
            for fid in owned:
                try:
                    text = await asyncio.to_thread(
                        _read_doc.invoke, {"file_id": fid}, {"callbacks": []}
                    )
                    text = str(text)
                except Exception:
                    text = ""
                if text:
                    # 单文档预览截断 6000 字符，多文档总量控制在 12000 字符内，
                    # 避免长文档把 prompt 撑爆；技能路径仍通过 file_id 读全文
                    previews.append(f"【文档 {fid} 内容预览】\n{text[:6000]}")
            preview_block = "\n\n".join(previews)[:12000]
            agent_message = (
                f"我已上传的文档 file_id：{', '.join(owned)}\n"
                f"{preview_block}\n\n"
                f"用户请求：{message}"
            )

    # 用户消息先落库（携带的附件登记 message_files，前端渲染为附件气泡）；
    # 首条消息自动生成标题；落库 content 用原始 message，不含 file_id
    sent_file_ids = owned if raw_ids else []
    web_store.add_message(
        user["id"], conv_id, "user", message, attachments=sent_file_ids
    )
    web_store.set_first_title(user["id"], conv_id, message)

    # 交付物快照：执行前后文件沙箱新增的 file_id 归属于本会话
    before_ids = {m.file_id for m in list_files()}

    async def event_stream():
        done_parts: list[str] = []    # done.output（graph 终态，含技能交付摘要）
        token_parts: list[str] = []  # 可见 token（纯对话兜底；技能 JSON 已被过滤）
        error_obj: dict[str, Any] | None = None
        stopped = False

        def _finalize_run(final_text: str, *, is_error: bool = False) -> tuple[int, list[dict[str, str]]]:
            """登记本次执行产生的交付物，并随助手消息一起落库。

            返回 (assistant_msg_id, 新增交付物)，供流结束后推送 artifacts/msg_saved 事件。
            （低并发内网工具，沙箱前后快照差分足够可靠；停止时产物也可能已渲染完。）
            """
            after_metas = {m.file_id: m for m in list_files()}
            new_files: list[dict[str, str]] = []
            for fid in set(after_metas) - before_ids:
                meta = after_metas[fid]
                web_store.register_file(
                    user["id"], fid, "artifact",
                    conversation_id=conv_id, filename=meta.original_name,
                )
                new_files.append({"file_id": fid, "filename": meta.original_name})
            msg_id = web_store.add_message(
                user["id"], conv_id, "assistant", final_text,
                is_error=is_error,
                attachments=[f["file_id"] for f in new_files],
            )
            return msg_id, new_files

        try:
            stream = agent_runner.run_invoke_stream(
                message=agent_message,
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
                    out = str(evt.get("output") or "")
                    done_parts.append(out)
                    cleaned = _strip_backend_meta(out)
                    if cleaned != out:
                        evt["output"] = cleaned
                        line = json.dumps(evt, ensure_ascii=False) + "\n"
                elif etype == "token":
                    token_parts.append(str(evt.get("content") or ""))
                elif etype == "error":
                    error_obj = evt
                yield line
        except (asyncio.CancelledError, GeneratorExit):
            # 用户点了「停止」/ 浏览器关闭连接：取消会传播到 graph 执行
            # （任务取消抛 CancelledError；部分 ASGI 清理路径注入 GeneratorExit）
            stopped = True
            # 停止路径无法再向客户端推送事件：shield 内完成「产物登记+消息落库」，
            # 刷新历史时产物卡片仍会随消息带出
            final_text = _strip_backend_meta(
                ("".join(done_parts) or "".join(token_parts)).strip()
            )
            final_text = (final_text + "\n\n（用户已停止生成，以上为已输出的部分内容）"
                          if final_text else "（用户已停止生成）")
            try:
                await asyncio.shield(asyncio.to_thread(_finalize_run, final_text))
            except Exception:
                pass
            raise
        except ApiError as e:
            error_obj = {"code": e.code, "message": e.message}
            yield json.dumps({"type": "error", "code": e.code, "message": e.message},
                             ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 —— 代理层兜底，保证前端拿到 error 事件
            error_obj = {"code": "STREAM_FAILED", "message": str(e)}
            yield json.dumps({"type": "error", "code": "STREAM_FAILED", "message": str(e)},
                             ensure_ascii=False) + "\n"

        # 正常/错误路径（连接仍存活）：先落库并登记交付物，再推送 artifacts 事件，
        # 前端把产物卡片渲染在助手回复气泡下方
        raw_text = _strip_backend_meta(
            ("".join(done_parts) or "".join(token_parts)).strip()
        )
        is_error = bool(error_obj and not raw_text)
        final_text = raw_text or (
            f"[{error_obj.get('code', 'ERROR')}] {error_obj.get('message', '执行失败')}"
            if error_obj else "（执行结束，无文本输出）"
        )
        try:
            msg_id, new_files = await asyncio.shield(
                asyncio.to_thread(_finalize_run, final_text, is_error=is_error)
            )
        except Exception:
            msg_id, new_files = 0, []
        if new_files:
            yield json.dumps(
                {"type": "artifacts", "files": new_files}, ensure_ascii=False
            ) + "\n"
        if msg_id:
            yield json.dumps(
                {"type": "msg_saved", "msg_id": msg_id}, ensure_ascii=False
            ) + "\n"

    return _ndjson_response(event_stream())


def _ndjson_response(generator):
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        generator,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 技能交付摘要里的后台信息行（文件 ID / 下载方式·API Key）——web 端产物已以
# 可点击卡片挂在消息气泡下，正文无需暴露接口细节；对外 /api/v1 契约不受影响
_WEB_BACKEND_META_RE = re.compile(r"^(文件 ID|下载方式)：.*$", re.MULTILINE)


def _strip_backend_meta(text: str) -> str:
    return _WEB_BACKEND_META_RE.sub("", text)


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
