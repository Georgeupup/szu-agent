"""多人模式的“应用登录会话”：用户用学校统一认证登录成功后，我们签发自己的会话 Cookie。

为什么不直接拿学校的 Cookie 当身份：学校的登录状态有效期短（7 天、随时可能被学校踢掉），
而且它是去访问学校系统的凭证，不该在浏览器和我们之间来回传。两种会话分开管：
  - 应用会话（这里）：证明“你是谁”，30 天有效；过期了重新扫码就行，user_id 不变
  - 学校会话（users/<id>/session.json）：去学校系统办事用；过期了在聊天里弹登录卡片

令牌的标准做法：
  - secrets.token_urlsafe(32)（256 位随机数），数据库只存 SHA-256；数据库泄露也冒充不了任何人
  - Cookie：HttpOnly（页面脚本读不到，XSS 偷不走）、SameSite=Lax（别的网站发起的 POST 不带它，防 CSRF）、
    HTTPS 部署时 Secure（SZU_AGENT_COOKIE_SECURE=1）
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from ..campus.session import agent_home

COOKIE = "szu_sid"
SESSION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,         -- HMAC(服务器密钥, 学号)，见 users.user_id_for；不存明文学号
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,      -- SHA-256(令牌)；令牌本身只在用户的 Cookie 里
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AccountStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or agent_home() / "accounts.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def login(self, user_id: str) -> str:
        """为这个用户新开一个会话，返回要放进 Cookie 的令牌。"""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._db() as db:
            db.execute("INSERT INTO users (user_id, created_at, last_seen_at) VALUES (?, ?, ?)"
                       " ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at", (user_id, now, now))
            db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (_hash(token), user_id, now, now + SESSION_DAYS * 86400))
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))  # 顺手清掉过期的
        return token

    def resolve(self, token: str | None) -> str | None:
        """Cookie 里的令牌 → user_id；无效或过期返回 None。"""
        if not token or len(token) > 100:
            return None
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
                             (_hash(token), now)).fetchone()
            if row:
                db.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ? AND last_seen_at < ?",
                           (now, row[0], now - 300))  # 最多 5 分钟写一次
        return row[0] if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock, self._db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))

    def delete_user(self, user_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))  # 会话级联删除

    def count_users(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
