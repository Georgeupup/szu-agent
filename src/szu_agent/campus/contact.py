"""图书馆预借要填的手机号。

为什么单独存、不放进用户画像（profile.json）：画像会被渲染进系统提示，也能由 LLM 通过
remember_user_info 写入——手机号一旦走这两条路，就会出现在模型上下文和对话历史里。
这里的手机号只能由用户在网页卡片里填写（POST /api/contact），只有 MCP 进程在提交表单时读取，
交给 LLM 的预览里只有打码后的形式。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .session import agent_home

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def mask_phone(phone: str | None) -> str | None:
    return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) >= 7 else None


def mask_email(email: str | None) -> str | None:
    """学校邮箱的用户名就是学号，所以只显示域名。"""
    return f"***@{email.split('@', 1)[1]}" if email and "@" in email else None


class ContactStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or agent_home() / "contact.json"
        self._lock = threading.Lock()

    def phone(self) -> str | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("phone")
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            return None
        return value if isinstance(value, str) and PHONE_RE.match(value) else None

    def set_phone(self, phone: str | None) -> None:
        with self._lock:
            if not phone:
                self.path.unlink(missing_ok=True)
                return
            if not PHONE_RE.match(phone):
                raise ValueError("手机号格式不对")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"phone": phone}, f)
            tmp.replace(self.path)
