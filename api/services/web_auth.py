# -*- coding: utf-8 -*-
"""轻量 Web 端账号体系（用户名 + 密码，服务端会话 Cookie）。

设计原则（刻意保持轻量）：
  - 无邮箱 / 无验证码 / 无自助注册；账号由管理员通过 CLI 创建；
  - 密码 PBKDF2-SHA256（标准库 hashlib），不明文存储；
  - 会话 token 为 HMAC 签名的无状态字符串，存 httpOnly Cookie；
  - 用户/会话/消息/文件归属统一落在 SQLite（data/web.db）。

CLI：
  python -m api.services.web_auth create-user <用户名>
  python -m api.services.web_auth list-users
  python -m api.services.web_auth reset-password <用户名>
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("WEBCHAT_DB", str(PROJECT_ROOT / "data" / "web.db")))
SECRET_FILE = PROJECT_ROOT / "data" / ".webchat_secret"

COOKIE_NAME = "webchat_session"
COOKIE_MAX_AGE = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000

# ────────────────────────────────────────────────────────────────────
# DB
# ────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """返回 SQLite 连接（row_factory=Row，外键开启）。调用方负责 close。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """建表（幂等）。服务启动时调用一次。"""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT NOT NULL UNIQUE,
                pw_hash    TEXT NOT NULL,           -- pbkdf2_sha256$iters$salt$hash
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,        -- uuid hex
                user_id    INTEGER NOT NULL,
                title      TEXT NOT NULL DEFAULT '新会话',
                scene      TEXT NOT NULL DEFAULT '',
                thread_id  TEXT NOT NULL UNIQUE,    -- LangGraph checkpoint 线程
                pinned     INTEGER NOT NULL DEFAULT 0,   -- 是否置顶
                pinned_at  TEXT,                          -- 置顶时间（置顶组内排序）
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL,      -- user / assistant
                content         TEXT NOT NULL,
                is_error        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);

            CREATE TABLE IF NOT EXISTS web_files (
                file_id         TEXT PRIMARY KEY,
                user_id         INTEGER NOT NULL,
                kind            TEXT NOT NULL,       -- upload / artifact
                conversation_id TEXT,
                filename        TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            -- 消息与附件的关联：用户发送消息时携带的上传文档登记于此，
            -- 附件以独立气泡渲染在消息流中；关联后不再出现在输入框暂存区。
            CREATE TABLE IF NOT EXISTS message_files (
                message_id INTEGER NOT NULL,
                file_id    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (message_id, file_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES web_files(file_id) ON DELETE CASCADE
            );
            """
        )
        # 老库增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "pinned" not in existing:
            conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "pinned_at" not in existing:
            conn.execute("ALTER TABLE conversations ADD COLUMN pinned_at TEXT")
        # 消息反馈列（赞/踩）：NULL=无，1=赞，-1=踩
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "feedback" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN feedback INTEGER")


# ────────────────────────────────────────────────────────────────────
# 密码
# ────────────────────────────────────────────────────────────────────

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iter_s, salt, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iter_s)
    )
    return hmac.compare_digest(dk.hex(), hash_hex)


# ────────────────────────────────────────────────────────────────────
# 用户 CRUD
# ────────────────────────────────────────────────────────────────────

def create_user(username: str, password: str) -> int:
    username = username.strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username, pw_hash) VALUES (?, ?)",
                (username, hash_password(password)),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名已存在：{username}")
        return int(cur.lastrowid)


def reset_password(username: str, password: str) -> None:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE users SET pw_hash = ? WHERE username = ?",
            (hash_password(password), username.strip()),
        )
        if cur.rowcount == 0:
            raise ValueError(f"用户不存在：{username}")


def list_users() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, pw_hash FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    if row and verify_password(password, row["pw_hash"]):
        return {"id": row["id"], "username": row["username"]}
    return None


# ────────────────────────────────────────────────────────────────────
# 会话 token（HMAC 签名的无状态 token）
# ────────────────────────────────────────────────────────────────────

def _signing_secret() -> bytes:
    env_secret = os.getenv("WEB_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def issue_token(user_id: int, *, max_age: int = COOKIE_MAX_AGE) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + max_age}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(_signing_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse_token(token: str | None) -> int | None:
    """校验签名与有效期，返回 user_id；无效返回 None。"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(
        _signing_secret(), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    uid = payload.get("uid")
    return int(uid) if isinstance(uid, int) else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def _cli() -> None:
    init_db()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "create-user" and len(args) == 2:
        username = args[1]
        pw = getpass.getpass(f"为 {username} 设置密码：")
        pw2 = getpass.getpass("再次输入：")
        if pw != pw2:
            print("两次输入不一致")
            sys.exit(1)
        uid = create_user(username, pw)
        print(f"已创建用户：{username}（id={uid}）")
    elif cmd == "reset-password" and len(args) == 2:
        username = args[1]
        pw = getpass.getpass("新密码：")
        pw2 = getpass.getpass("再次输入：")
        if pw != pw2:
            print("两次输入不一致")
            sys.exit(1)
        reset_password(username, pw)
        print(f"已重置密码：{username}")
    elif cmd == "list-users":
        for u in list_users():
            print(f"  {u['id']:>3}  {u['username']}  ({u['created_at']})")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
