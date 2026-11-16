"""历史会话：对话状态用 LangGraph 的 SQLite checkpointer 持久化，另外一张小表记录会话列表。

为什么另建一张 threads 表，而不是从 checkpoint 里列出 thread_id：
checkpoint 是序列化后的完整图状态，列表页只需要标题和时间，没必要每次反序列化所有对话。

隐私：数据库里有对话原文和工具结果（通知内容、预约、电量），但没有密码和验证码
（它们从不进入对话）。文件权限设为 600，默认只保留最近 30 天，用户可以在界面上删除单个会话。
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ..campus.session import agent_home
from ..users import LOCAL, thread_key

TITLE_CHARS = 30


def default_history_path(mock: bool = False) -> Path:
    # 演示模式单独一个文件，样例数据不会混进真实对话
    return agent_home() / ("history-mock.sqlite" if mock else "history.sqlite")


def retention_days() -> float:
    """SZU_AGENT_HISTORY_DAYS：会话保留天数，0 表示永久保留。"""
    try:
        return float(os.environ.get("SZU_AGENT_HISTORY_DAYS", "30"))
    except ValueError:
        return 30


def _restrict(path: Path) -> None:
    """数据库和 SQLite 的 WAL/SHM 附属文件都只允许本人读写（Windows 上 chmod 只影响只读位，无害）。"""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            try:
                p.chmod(0o600)
            except OSError:
                pass


class ThreadStore:
    """会话列表（标题、时间）。对外的 thread_id 是前端的对话 id；存储用 users.thread_key 加上用户前缀。"""

    def __init__(self, saver: AsyncSqliteSaver) -> None:
        self.saver = saver
        self.conn: aiosqlite.Connection = saver.conn

    async def setup(self) -> None:
        async with self.saver.lock:
            await self.conn.execute(
                "CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            # 旧库没有 user_id 列：补上，已有会话都算单人模式的 local
            async with self.conn.execute("PRAGMA table_info(threads)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
            if "user_id" not in cols:
                await self.conn.execute(f"ALTER TABLE threads ADD COLUMN user_id TEXT NOT NULL DEFAULT '{LOCAL}'")
            await self.conn.execute("CREATE INDEX IF NOT EXISTS threads_user ON threads (user_id, updated_at)")
            await self.conn.commit()

    async def touch(self, thread_id: str, message: str, user_id: str = LOCAL) -> None:
        """用户发消息时调用：新会话用第一条消息当标题，老会话只更新时间。"""
        title = " ".join(message.split())[:TITLE_CHARS] or "新对话"
        now = time.time()
        async with self.saver.lock:
            await self.conn.execute(
                "INSERT INTO threads (thread_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(thread_id) DO UPDATE SET updated_at = excluded.updated_at",
                (thread_key(user_id, thread_id), user_id, title, now, now))
            await self.conn.commit()

    async def list(self, user_id: str = LOCAL, limit: int = 50) -> list[dict[str, Any]]:
        prefix = len(thread_key(user_id, ""))
        async with self.saver.lock:
            async with self.conn.execute(
                    "SELECT thread_id, title, updated_at FROM threads WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit)) as cur:
                rows = await cur.fetchall()
        return [{"id": r[0][prefix:], "title": r[1], "updatedAt": r[2]} for r in rows]

    async def exists(self, thread_id: str, user_id: str = LOCAL) -> bool:
        async with self.saver.lock:
            async with self.conn.execute("SELECT 1 FROM threads WHERE thread_id = ? AND user_id = ?",
                                         (thread_key(user_id, thread_id), user_id)) as cur:
                return await cur.fetchone() is not None

    async def delete(self, thread_id: str, user_id: str = LOCAL) -> None:
        await self._delete_key(thread_key(user_id, thread_id))

    async def delete_user(self, user_id: str) -> int:
        """删除这个用户的全部会话（“删除我的数据”）。"""
        async with self.saver.lock:
            async with self.conn.execute("SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)) as cur:
                keys = [r[0] for r in await cur.fetchall()]
        for key in keys:
            await self._delete_key(key)
        return len(keys)

    async def _delete_key(self, key: str) -> None:
        await self.saver.adelete_thread(key)  # 内部自己加锁
        async with self.saver.lock:
            await self.conn.execute("DELETE FROM threads WHERE thread_id = ?", (key,))
            await self.conn.commit()

    async def prune(self, days: float) -> int:
        """删除超过保留期的会话（所有用户），返回删除的数量。"""
        if days <= 0:
            return 0
        async with self.saver.lock:
            async with self.conn.execute("SELECT thread_id FROM threads WHERE updated_at < ?",
                                         (time.time() - days * 86400,)) as cur:
                old = [r[0] for r in await cur.fetchall()]
        for key in old:
            await self._delete_key(key)
        return len(old)


@asynccontextmanager
async def open_history(path: Path) -> AsyncIterator[tuple[AsyncSqliteSaver, ThreadStore]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # WAL 文件会被 SQLite 反复重建（权限取决于 umask），所以目录本身也只允许本人进入
        path.parent.chmod(0o700)
    except OSError:
        pass
    if not path.exists():
        # 先以 600 权限创建空文件，避免数据库在“默认权限”下存在哪怕一瞬间
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        threads = ThreadStore(saver)
        await threads.setup()
        _restrict(path)
        await threads.prune(retention_days())
        yield saver, threads
