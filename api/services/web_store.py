# -*- coding: utf-8 -*-
"""Web 端会话/消息/文件归属的数据访问层（SQLite，见 web_auth.init_db）。

所有读写都必须带 user_id 做归属过滤，确保用户只能访问自己的数据。
"""
from __future__ import annotations

import uuid
from typing import Any

from api.services.web_auth import get_db


# ────────────────────────────────────────────────────────────────────
# 会话
# ────────────────────────────────────────────────────────────────────

def create_conversation(user_id: int, title: str = "新会话", scene: str = "") -> dict[str, Any]:
    conv_id = uuid.uuid4().hex
    thread_id = f"web-{uuid.uuid4().hex}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversations(id, user_id, title, scene, thread_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, user_id, title, scene, thread_id),
        )
    return get_conversation(user_id, conv_id)  # type: ignore[return-value]


def get_conversation(user_id: int, conv_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_conversations(user_id: int) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, scene, pinned, pinned_at, created_at, updated_at "
            "FROM conversations WHERE user_id = ? "
            "ORDER BY pinned DESC, pinned_at DESC, updated_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_pinned(user_id: int, conv_id: str, pinned: bool) -> bool:
    """置顶/取消置顶（带归属校验）。置顶时间决定置顶组内的先后。"""
    with get_db() as conn:
        if pinned:
            cur = conn.execute(
                "UPDATE conversations SET pinned = 1, "
                "pinned_at = datetime('now','localtime') "
                "WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE conversations SET pinned = 0, pinned_at = NULL "
                "WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            )
    return cur.rowcount > 0


def rename_conversation(user_id: int, conv_id: str, title: str) -> bool:
    title = title.strip()[:100]
    if not title:
        return False
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = datetime('now','localtime') "
            "WHERE id = ? AND user_id = ?",
            (title, conv_id, user_id),
        )
    return cur.rowcount > 0


def touch_conversation(conv_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conv_id,),
        )


def set_scene(user_id: int, conv_id: str, scene: str) -> None:
    """本次对话切换场景码时回写会话（带归属校验）。"""
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET scene = ? WHERE id = ? AND user_id = ?",
            (scene, conv_id, user_id),
        )


def delete_conversation(user_id: int, conv_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
    return cur.rowcount > 0


def set_first_title(user_id: int, conv_id: str, text: str) -> None:
    """首条用户消息自动生成标题（截断 30 字）。"""
    title = text.strip().replace("\n", " ")[:30] or "新会话"
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ? AND title = '新会话'",
            (title, conv_id, user_id),
        )


# ────────────────────────────────────────────────────────────────────
# 消息
# ────────────────────────────────────────────────────────────────────

def add_message(
    user_id: int,
    conv_id: str,
    role: str,
    content: str,
    *,
    is_error: bool = False,
    attachments: list[str] | None = None,
) -> int:
    with get_db() as conn:
        # 双重归属校验：会话必须属于该用户
        owned = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not owned:
            raise PermissionError("conversation not owned by user")
        cur = conn.execute(
            "INSERT INTO messages(conversation_id, role, content, is_error) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, 1 if is_error else 0),
        )
        msg_id = int(cur.lastrowid)
        # 登记消息附件（去重；INSERT OR IGNORE 容忍重复提交）
        if attachments:
            for fid in dict.fromkeys(str(f).strip() for f in attachments if str(f).strip()):
                conn.execute(
                    "INSERT OR IGNORE INTO message_files(message_id, file_id) VALUES (?, ?)",
                    (msg_id, fid),
                )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conv_id,),
        )
        return msg_id


def list_messages(user_id: int, conv_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.is_error, m.feedback, m.created_at "
            "FROM messages m JOIN conversations c ON m.conversation_id = c.id "
            "WHERE c.id = ? AND c.user_id = ? ORDER BY m.id",
            (conv_id, user_id),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            atts = conn.execute(
                "SELECT f.file_id, f.filename FROM message_files mf "
                "JOIN web_files f ON f.file_id = mf.file_id "
                "WHERE mf.message_id = ? ORDER BY mf.created_at, f.filename",
                (item["id"],),
            ).fetchall()
            item["attachments"] = [dict(a) for a in atts]
            result.append(item)
        # 兼容改版前的旧产物：已登记到会话、但未关联任何消息的 artifact，
        # 挂到最后一条助手消息（无助手消息则挂最后一条）展示，避免丢失下载入口
        if result:
            orphans = conn.execute(
                "SELECT f.file_id, f.filename FROM web_files f "
                "WHERE f.user_id = ? AND f.kind = 'artifact' AND f.conversation_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM message_files mf WHERE mf.file_id = f.file_id) "
                "ORDER BY f.created_at, f.filename",
                (user_id, conv_id),
            ).fetchall()
            if orphans:
                target_idx = next(
                    (i for i in range(len(result) - 1, -1, -1)
                     if result[i]["role"] == "assistant"),
                    len(result) - 1,
                )
                result[target_idx]["attachments"].extend(dict(o) for o in orphans)
    return result


def set_feedback(user_id: int, conv_id: str, msg_id: int, value: int) -> bool:
    """设置助手消息的反馈（1=赞，-1=踩，0=取消）。带归属校验。"""
    value = 1 if value > 0 else (-1 if value < 0 else 0)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE messages SET feedback = ? "
            "WHERE id = ? AND conversation_id = ? "
            "AND EXISTS (SELECT 1 FROM conversations WHERE id = conversation_id AND user_id = ?)",
            (value if value else None, msg_id, conv_id, user_id),
        )
    return cur.rowcount > 0


# ────────────────────────────────────────────────────────────────────
# 文件归属
# ────────────────────────────────────────────────────────────────────

def register_file(
    user_id: int,
    file_id: str,
    kind: str,
    *,
    conversation_id: str | None = None,
    filename: str = "",
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO web_files(file_id, user_id, kind, conversation_id, filename) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_id, user_id, kind, conversation_id, filename),
        )


def owns_file(user_id: int, file_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM web_files WHERE file_id = ? AND user_id = ?",
            (file_id, user_id),
        ).fetchone()
    return row is not None


def list_files(
    user_id: int, *, conversation_id: str | None = None, kind: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT file_id, kind, conversation_id, filename, created_at FROM web_files WHERE user_id = ?"
    params: list[Any] = [user_id]
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        params.append(conversation_id)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    # 待发送暂存区（kind=upload）只显示尚未随消息发送的文件；
    # 已关联消息的文件以附件气泡形式存在于消息流中，不再挂在输入框上方。
    if kind == "upload":
        sql += " AND NOT EXISTS (SELECT 1 FROM message_files mf WHERE mf.file_id = web_files.file_id)"
    sql += " ORDER BY created_at DESC, file_id DESC LIMIT 100"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def remove_file_row(user_id: int, file_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM web_files WHERE file_id = ? AND user_id = ?",
            (file_id, user_id),
        )
    return cur.rowcount > 0
