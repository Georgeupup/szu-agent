"""测试工具：脚本化的假 LLM、隔离的数据目录、模拟登录。"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from szu_agent.campus import SessionStore

# 演示数据按“今天”平移日期；测试固定在样例录制当天，断言里的 2026-11-14/15 才成立（MCP 子进程继承环境变量）
os.environ["SZU_AGENT_MOCK_TODAY"] = "2026-11-14"

Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """按剧本依次返回 AIMessage 的假模型。剧本项可以是函数，用来读取上一步工具结果（比如确认令牌）。"""

    script: list[Any]
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        assert_valid_tool_sequence(messages)
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedChatModel 剧本已用完")
        step = self.script.pop(0)
        msg = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=msg)])


def assert_valid_tool_sequence(messages) -> None:
    """模仿 OpenAI 兼容接口的校验：带 tool_calls 的 AI 消息后面，必须紧跟每个 tool_call_id 对应的 ToolMessage。
    真接口在这里会返回 400 “insufficient tool messages following tool_calls message”。"""
    from langchain_core.messages import ToolMessage

    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            following = set()
            for n in messages[i + 1:]:
                if not isinstance(n, ToolMessage):
                    break
                following.add(n.tool_call_id)
            missing = {tc["id"] for tc in m.tool_calls} - following
            if missing:
                raise AssertionError(f"400: tool_calls {missing} 后面缺少对应的 ToolMessage")


def ai_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def mock_login(store: SessionStore) -> None:
    jar = httpx.Cookies()
    jar.set("MOCK_TGC", "test", domain="authserver.szu.edu.cn")
    store.save(jar, method="qr", loggedInAt=0)


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path_factory):
    """所有测试默认使用临时数据目录，绝不碰用户真实的 ~/.szu-agent（凭证、日志、画像）。"""
    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录（会话凭证、用户画像都在里面），MCP 子进程通过环境变量继承。"""
    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def logged_in(home):
    store = SessionStore(mock=True)
    mock_login(store)
    return store
