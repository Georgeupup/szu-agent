"""登录凭证（会话 Cookie）的本地持久化。

保存的是统一认证颁发的 Cookie（CASTGC 等），不保存密码。勾选“记住我”时学校给 7 天有效期。
这个文件等同于登录凭证：权限 600，只存在用户自己的电脑上。

Web 进程负责登录（写），MCP Server 进程负责调用学校接口（读、并回写刷新后的 Cookie），
两个进程通过这个文件共享会话，所以每次请求前都按 mtime 检查是否需要重新加载。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx


def agent_home() -> Path:
    return Path(os.environ.get("SZU_AGENT_HOME", Path.home() / ".szu-agent"))


class SessionStore:
    def __init__(self, path: Path | None = None, *, mock: bool = False) -> None:
        self.path = path or agent_home() / ("session-mock.json" if mock else "session.json")
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def cookies(self) -> httpx.Cookies:
        jar = httpx.Cookies()
        for c in self.load().get("cookies", []):
            jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        return jar

    def save(self, cookies: httpx.Cookies, **meta: Any) -> None:
        with self._lock:
            data = self.load()
            data["cookies"] = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in cookies.jar
            ]
            data.update(meta)
            data["updatedAt"] = int(time.time())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            # 先以 600 权限创建再写入，避免凭证在短时间内对其他用户可读
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            tmp.replace(self.path)

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    def info(self) -> dict[str, Any]:
        """给界面展示用的会话信息，不含任何 Cookie 值。"""
        data = self.load()
        names = {c["name"] for c in data.get("cookies", [])}
        return {
            "hasSession": bool({"CASTGC", "MOCK_TGC"} & names),
            "method": data.get("method"),
            "loggedInAt": data.get("loggedInAt"),
        }
