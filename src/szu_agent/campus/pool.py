"""每个用户一个学校客户端：独立的登录凭证文件、独立的查询缓存、独立的连接。

多人模式下绝不能共用一个客户端——CampusHttp 的查询缓存按地址缓存，
共用时 B 查“我的预约”可能直接拿到 A 刚才的结果。
MCP 工具进程和 Web 进程各有一个池（Web 进程只用于附件下载、付款卡片这类直接给浏览器的请求）。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from .contact import ContactStore
from .session import SessionStore


class CampusPool:
    def __init__(self, *, mock: bool, max_size: int = 200) -> None:
        self.mock = mock
        self.max_size = max_size
        self._items: OrderedDict[str, Any] = OrderedDict()

    def get(self, user_id: str) -> Any:
        from .. import users
        from . import make_campus

        if user_id in self._items:
            self._items.move_to_end(user_id)
            return self._items[user_id]
        campus = make_campus(mock=self.mock, store=SessionStore(users.session_path(user_id, mock=self.mock)),
                             contact=ContactStore(users.contact_path(user_id)))
        self._items[user_id] = campus
        while len(self._items) > self.max_size:  # 最久没用的关掉连接；它的凭证文件还在，下次用到再建
            _, old = self._items.popitem(last=False)
            self._close_later(old)
        return campus

    def drop(self, user_id: str) -> None:
        """用户退出登录 / 删除数据后丢掉内存里的客户端（含缓存）。"""
        if (old := self._items.pop(user_id, None)) is not None:
            self._close_later(old)

    @staticmethod
    def _close_later(campus: Any) -> None:
        http = getattr(campus, "http", None)
        if http is None:
            return
        try:
            asyncio.get_running_loop().create_task(http.aclose())
        except RuntimeError:
            pass

    async def aclose(self) -> None:
        for campus in self._items.values():
            if (http := getattr(campus, "http", None)) is not None:
                await http.aclose()
        self._items.clear()
