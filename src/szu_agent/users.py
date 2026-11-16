"""多用户：用户身份和每个用户的数据位置。

两种运行方式：
  - 单人（默认）：自己电脑上用，只有一个用户 "local"，数据就在 ~/.szu-agent/ 下（和以前完全一样）
  - 多人（SZU_AGENT_MULTIUSER=1，部署到服务器给同学用）：每个人先用学校统一认证登录，
    服务器用学号算出 user_id，数据放在 ~/.szu-agent/users/<user_id>/

user_id = HMAC-SHA256(服务器密钥, 学号) 的前 32 位十六进制：
  - 同一个人每次登录都是同一个 id（换设备、学校登录过期后重新扫码，记忆和历史都还在）
  - 明文学号不落盘、不进日志、不给模型；没有服务器密钥就算不出某个学号对应的 id
  - 学号只有一亿种左右，不加密钥的普通哈希可以被穷举反查，所以必须用 HMAC
  - 密钥一旦更换，所有人的 id 都会变（相当于数据全部“丢失”），部署后不要换

“当前是谁”放在 contextvar 里（current_user）：web 层按会话 Cookie 设置，对话图把它带进工具调用，
MCP 工具进程按请求元数据设置。它永远由服务器端代码决定，不来自模型输出或前端参数。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
from contextvars import ContextVar
from pathlib import Path

from .campus.session import agent_home

LOCAL = "local"
_ID = re.compile(r"^[0-9a-f]{32}$")
current_user: ContextVar[str] = ContextVar("szu_current_user", default=LOCAL)


def user_of(config: dict | None) -> str:
    """从 LangGraph 的 config 里取当前用户（web 层写进 configurable.user_id）。"""
    return ((config or {}).get("configurable") or {}).get("user_id") or LOCAL


def multiuser() -> bool:
    return os.environ.get("SZU_AGENT_MULTIUSER") == "1"


def server_secret() -> bytes:
    """优先用环境变量 SZU_AGENT_SECRET；没有就在数据目录生成一个（权限 600）并一直用它。"""
    if env := os.environ.get("SZU_AGENT_SECRET"):
        return env.encode()
    path = agent_home() / "secret.key"
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # 另一个进程同时生成了：用它的
            return path.read_bytes().strip()
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


def user_id_for(student_no: str) -> str:
    no = student_no.strip().upper()
    if not no:
        raise ValueError("学号为空")
    return hmac.new(server_secret(), no.encode(), hashlib.sha256).hexdigest()[:32]


def private_dir(path: Path) -> Path:
    """建目录并设成只有本人能进（700）。数据根目录已经是 700，这里再加一层：放学校登录凭证的目录不能靠“上一级碰巧是 700”。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:  # Windows 上 chmod 只影响只读位
        pass
    return path


def valid_user_id(user_id: str) -> bool:
    return user_id == LOCAL or bool(_ID.match(user_id))


def user_dir(user_id: str) -> Path:
    """这个用户的数据目录。local 就是数据根目录本身（兼容单人模式原来的文件位置）。"""
    if not valid_user_id(user_id):  # 防止拼出 ../ 之类的路径
        raise ValueError("非法的 user_id")
    return agent_home() if user_id == LOCAL else agent_home() / "users" / user_id


def session_path(user_id: str, *, mock: bool) -> Path:
    return user_dir(user_id) / ("session-mock.json" if mock else "session.json")


def profile_path(user_id: str) -> Path:
    return user_dir(user_id) / "profile.json"


def contact_path(user_id: str) -> Path:
    return user_dir(user_id) / "contact.json"


def thread_key(user_id: str, thread_id: str) -> str:
    """对话在 checkpointer 里的键。多人模式加上用户前缀：前端传来的对话 id 只在“自己名下”有意义，
    猜到别人的对话 id 也读不到（键不同），不需要每处都记得检查归属。单人模式保持原样，兼容已有历史。"""
    return thread_id if user_id == LOCAL else f"{user_id}:{thread_id}"


def delete_user_files(user_id: str) -> None:
    """删除这个用户目录下的全部文件（学校登录凭证、记忆、联系手机）。单人模式不提供（数据根目录还有别的东西）。"""
    if user_id == LOCAL:
        raise ValueError("单人模式不支持整体删除")
    shutil.rmtree(user_dir(user_id), ignore_errors=True)
