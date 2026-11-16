"""端到端：假 LLM -> LangGraph -> MCP Server 子进程 -> 演示后端。

验证三件只有真实链路才能暴露的事：
  1. 环境变量能传进 MCP 子进程（SZU_AGENT_HOME / SZU_AGENT_MOCK），子进程读到的是同一份登录会话
  2. preview 签发的令牌在 confirm 时仍然有效（长连接 session，而不是每次调用新起进程）
  3. 人工确认的中断能拿到真实的预览数据
"""

import json
import re

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from szu_agent.agent.graph import message_text
from szu_agent.agent.profile import ProfileStore
from szu_agent.agent.runtime import open_agent

from .conftest import ScriptedChatModel, ai_tool_call


def confirm_with_token_from_history(messages):
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "sports_reserve_preview":
            token = re.search(r'"confirmationToken":\s*"([^"]+)"', message_text(m)).group(1)
            return ai_tool_call("sports_reserve_confirm", {"confirmation_token": token}, "c3")
    raise AssertionError("no preview in history")


async def test_full_reservation_flow(logged_in, home):
    llm = ScriptedChatModel(script=[
        ai_tool_call("sports_fields", {"campus": "粤海校区", "project": "羽毛球", "date": "2026-11-15", "slot": "20:00-21:00"}, "c1"),
        ai_tool_call("sports_reserve_preview", {"campus": "粤海校区", "project": "羽毛球", "date": "2026-11-15",
                                                "slot": "20:00-21:00", "field": "羽毛球场 A4"}, "c2"),
        AIMessage(content="A4 场，确认吗？"),
        confirm_with_token_from_history,
        AIMessage(content="已提交，请去支付"),
    ])
    cfg = {"configurable": {"thread_id": "e2e"}}
    async with open_agent(mock=True, llm=llm, profile=ProfileStore(home / "p.json")) as graph:
        await graph.ainvoke({"messages": [("user", "约周日晚8点羽毛球")]}, cfg)
        await graph.ainvoke({"messages": [("user", "A4，确认")]}, cfg)

        state = await graph.aget_state(cfg)
        preview = state.interrupts[0].value["actions"][0]["preview"]
        assert preview["field"] == "羽毛球场 A4" and preview["submitted"] is False

        out = await graph.ainvoke(Command(resume={"approved": True}), cfg)
        result = json.loads(message_text([m for m in out["messages"] if isinstance(m, ToolMessage)][-1]))
        assert result["ok"] is True, result
        assert result["data"]["submitted"] is True
        assert out["messages"][-1].content == "已提交，请去支付"
