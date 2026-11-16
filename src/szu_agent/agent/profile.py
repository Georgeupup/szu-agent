"""长期记忆：用户画像。

只记录能减少追问的少量偏好（宿舍、常用校区、兴趣），保存在本机 JSON 文件。
字段是白名单——学号、姓名、密码这类信息从结构上就存不进来。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ..users import profile_path, user_of

ProfileKey = Literal["dorm_campus", "dorm_building", "dorm_room", "preferred_sports_campus", "favorite_sports", "interests",
                     "student_type", "college"]

PROFILE_LABELS: dict[str, str] = {
    "dorm_campus": "宿舍所在斋区/校区",
    "dorm_building": "宿舍楼栋",
    "dorm_room": "宿舍房间号",
    "preferred_sports_campus": "常去的运动校区",
    "favorite_sports": "常约的运动项目",
    "interests": "感兴趣的活动类型",
    "student_type": "学生类别（本科生/研究生）",
    "college": "所在学院",
}


def default_profile_path() -> Path:
    home = Path(os.environ.get("SZU_AGENT_HOME", Path.home() / ".szu-agent"))
    return home / "profile.json"


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_profile_path()
        # LLM 常在一轮里并行调用多次 remember_user_info（楼栋 + 房间号），
        # ToolNode 会并发执行它们；不加锁时“读-改-写”互相覆盖，只剩最后一个
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if k in PROFILE_LABELS}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def set(self, key: str, value: str | None) -> dict[str, str]:
        with self._lock:
            data = self.load()
            if value:
                data[key] = value
            else:
                data.pop(key, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)  # 原子替换，写到一半崩溃也不会留下损坏的文件
            return data

    def render(self) -> str:
        data = self.load()
        if not data:
            return "（暂无）"
        return "\n".join(f"- {PROFILE_LABELS[k]}：{v}" for k, v in data.items())


class ProfileRegistry:
    """多人模式：每个用户一份记忆文件（users/<user_id>/profile.json）。同一个用户总是拿到同一个 ProfileStore，锁才有效。"""

    def __init__(self) -> None:
        self._stores: dict[str, ProfileStore] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> ProfileStore:
        with self._lock:
            if user_id not in self._stores:
                self._stores[user_id] = ProfileStore(profile_path(user_id))
            return self._stores[user_id]


Profiles = ProfileStore | ProfileRegistry


def resolve_profile(profiles: Profiles, user_id: str) -> ProfileStore:
    """单人模式 / 测试传进来的是一个固定的 ProfileStore，多人模式是 ProfileRegistry。"""
    return profiles if isinstance(profiles, ProfileStore) else profiles.get(user_id)


def make_profile_tools(profiles: Profiles):
    @tool
    def remember_user_info(key: ProfileKey, value: str, config: RunnableConfig) -> str:
        """记住用户明确告诉你的个人偏好，下次就不用再问。
        只在用户亲口说出时调用（如“我住红豆斋838”），不要根据推测保存。value 传空字符串表示删除。"""
        # config 由 LangChain 注入、不出现在工具参数里：记到谁名下由服务器决定，模型改不了
        store = resolve_profile(profiles, user_of(config))
        store.set(key, value or None)
        return f"已{'记住' if value else '删除'}：{PROFILE_LABELS[key]}{'=' + value if value else ''}"

    return [remember_user_info]
