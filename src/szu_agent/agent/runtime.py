"""把 LLM、MCP 工具、记忆组装成一个可运行的 agent。"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from .graph import build_graph
from .. import users
from .profile import ProfileRegistry, Profiles, ProfileStore, make_profile_tools, resolve_profile
from .threads import ThreadStore, default_history_path, open_history
from .ui_tools import UI_TOOLS
from .usage import UsageStore


def make_llm() -> BaseChatModel:
    """任何 OpenAI 兼容接口：阿里云百炼、DeepSeek、Kimi、智谱……只需改 .env。"""
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise SystemExit("缺少 LLM_API_KEY，请复制 .env.example 为 .env 并填写。")
    # 百炼上的 deepseek-v4.1-flash、千问 3.x 默认开思考：每次调用多 1-2 秒（2026-11-17 实测）；
    # 有些模型（如 qwen4-max）思考模式下多轮工具调用还要求回传 reasoning_content，langchain-openai 不回传。
    # 所以百炼上设 LLM_ENABLE_THINKING=0。
    # 不设就不传这个参数（DeepSeek 官方接口不认它）。
    thinking = os.environ.get("LLM_ENABLE_THINKING", "").strip()
    extra = {"enable_thinking": thinking not in ("0", "false", "off")} if thinking else None
    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key,
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
        timeout=60,
        max_retries=2,
        # 流式输出时让接口在最后一个分块里带上 usage（token 数、缓存命中），用量统计靠它。
        # langchain-openai 只对 OpenAI 官方地址默认打开，换成 DeepSeek 等地址要显式开
        stream_usage=True,
        extra_body=extra,
    )


def mcp_connections(mock: bool) -> dict:
    # 注意：必须显式传 env。MCP 的 stdio 客户端默认只继承 PATH/HOME 等少数变量，
    # SZU_AGENT_MOCK、SZU_AGENT_HOME 这类配置会丢失。
    env = {**os.environ, "SZU_AGENT_MOCK": "1" if mock else "0"}
    return {
        "szu": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "szu_agent.mcp_server"],
            "env": env,
        }
    }


@dataclass
class AgentRuntime:
    graph: Any
    tools: dict[str, BaseTool]
    profiles: Profiles
    threads: ThreadStore
    usage: UsageStore

    def profile(self, user_id: str = users.LOCAL) -> ProfileStore:
        return resolve_profile(self.profiles, user_id)


class UserMetaSession:
    """包住 MCP 会话：每次工具调用都在请求元数据里带上当前用户（users.current_user）。

    langchain-mcp-adapters 不提供传元数据的参数，但它调用的就是 session.call_tool(...)，
    而 MCP SDK 的 call_tool 支持 meta=。元数据不属于工具参数：模型看不到、也没法伪造。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        kwargs["meta"] = {**(kwargs.get("meta") or {}), "user_id": users.current_user.get()}
        return await self._session.call_tool(name, arguments, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@asynccontextmanager
async def open_runtime(
    *,
    mock: bool = False,
    llm: BaseChatModel | None = None,
    profile: Profiles | None = None,
    history_path: Path | None = None,
    usage: UsageStore | None = None,
) -> AsyncIterator[AgentRuntime]:
    """启动 MCP Server 子进程，加载工具，打开历史会话数据库，编译图。

    这里用 client.session() 保持一个长连接，而不是 client.get_tools()：
    后者每次调用工具都会新起一个 Server 进程，preview 签发的确认令牌在 confirm 时就找不到了。
    """
    client = MultiServerMCPClient(mcp_connections(mock))
    # 单人模式一份记忆；多人模式每人一份
    profile = profile or (ProfileRegistry() if users.multiuser() else ProfileStore())
    usage = usage or UsageStore()  # 演示模式也记：演示数据是假的，模型调用和花费是真的
    async with open_history(history_path or default_history_path(mock)) as (saver, threads), client.session("szu") as session:
        mcp_tools = await load_mcp_tools(UserMetaSession(session))
        tools = [*mcp_tools, *make_profile_tools(profile), *UI_TOOLS]
        graph = build_graph(llm or make_llm(), tools, profile=profile, checkpointer=saver, usage=usage)
        yield AgentRuntime(graph=graph, tools={t.name: t for t in tools}, profiles=profile, threads=threads, usage=usage)


@asynccontextmanager
async def open_agent(**kwargs) -> AsyncIterator:
    """只需要图的场景（终端、测试）用这个。"""
    async with open_runtime(**kwargs) as runtime:
        yield runtime.graph
