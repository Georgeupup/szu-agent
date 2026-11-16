"""历史会话：SQLite 持久化（重启后对话还在）、会话列表、删除、过期清理、文件权限。"""

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from szu_agent.agent.graph import build_graph
from szu_agent.agent.profile import ProfileStore
from szu_agent.agent.threads import open_history
from szu_agent.web.app import create_app

from .conftest import ScriptedChatModel

CFG = {"configurable": {"thread_id": "p1"}}


async def test_conversation_survives_restart(tmp_path):
    db = tmp_path / "history.sqlite"
    profile = ProfileStore(tmp_path / "p.json")
    async with open_history(db) as (saver, threads):
        graph = build_graph(ScriptedChatModel(script=[AIMessage("你好")]), [], profile=profile, checkpointer=saver)
        await threads.touch("p1", "我想约周日的羽毛球，最好是晚上八点以后的场地")
        await graph.ainvoke({"messages": [("user", "我想约羽毛球")]}, CFG)

    async with open_history(db) as (saver, threads):  # 模拟重启：重新打开同一个文件
        graph = build_graph(ScriptedChatModel(script=[]), [], profile=profile, checkpointer=saver)
        state = await graph.aget_state(CFG)
        assert [m.content for m in state.values["messages"]] == ["我想约羽毛球", "你好"]
        [item] = await threads.list()
        assert item["id"] == "p1" and item["title"] == "我想约周日的羽毛球，最好是晚上八点以后的场地"[:30]

        await threads.delete("p1")
        assert await threads.list() == []
        assert not (await graph.aget_state(CFG)).values  # checkpoint 也一起删掉了


async def test_prune_removes_only_expired_threads(tmp_path):
    async with open_history(tmp_path / "h.sqlite") as (_, threads):
        await threads.touch("old", "旧对话")
        await threads.touch("new", "新对话")
        await threads.conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = 'old'", (time.time() - 40 * 86400,))
        assert await threads.prune(30) == 1
        assert [t["id"] for t in await threads.list()] == ["new"]
        assert await threads.prune(0) == 0  # 0 = 永久保留


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 POSIX 权限位")
async def test_history_file_is_private(tmp_path):
    db = tmp_path / "home" / "history.sqlite"
    async with open_history(db):
        pass
    assert os.stat(db).st_mode & 0o077 == 0
    assert os.stat(db.parent).st_mode & 0o077 == 0


def test_thread_api(home):
    llm = ScriptedChatModel(script=[AIMessage("在的"), AIMessage("好")])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "w1", "message": "在吗"}) as r:
            r.read()
        with client.stream("POST", "/api/chat", json={"thread_id": "w2", "message": "查电费"}) as r:
            r.read()
        assert [t["title"] for t in client.get("/api/threads").json()["items"]] == ["查电费", "在吗"]

        hist = client.get("/api/history/w1").json()
        assert hist["messages"][0]["sentAt"] and hist["context"]["windowMessages"] == 2

        assert client.delete("/api/threads/w1").json() == {"ok": True}
        assert [t["id"] for t in client.get("/api/threads").json()["items"]] == ["w2"]
        assert client.get("/api/history/w1").json()["messages"] == []
    assert (home / "history-mock.sqlite").exists() and not (home / "history.sqlite").exists()
