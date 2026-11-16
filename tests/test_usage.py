"""模型用量统计：价格折算、记录与汇总、对话图自动记账、统计接口。"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from szu_agent.agent.pricing import cost_cny, is_peak
from szu_agent.agent.profile import ProfileStore
from szu_agent.agent.usage import UsageStore, extract_usage

from .conftest import ScriptedChatModel

CST = timezone(timedelta(hours=8))


def reply(text="好的", inp=10_000, hit=9_000, out=200):
    return AIMessage(text, usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                                           "input_token_details": {"cache_read": hit}})


def test_peak_hours_are_beijing_weekdays():
    assert is_peak(datetime(2026, 11, 16, 9, 0, tzinfo=CST))          # 周一 9:00
    assert not is_peak(datetime(2026, 11, 16, 12, 0, tzinfo=CST))     # 午休
    assert is_peak(datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc))  # = 北京 14:30
    assert not is_peak(datetime(2026, 11, 15, 10, 0, tzinfo=CST))     # 周日


def test_cost_uses_hit_miss_output_prices():
    off_peak = datetime(2026, 11, 16, 20, 0, tzinfo=CST)
    # flash 空闲价：命中 0.02、未命中 1、输出 4（元/百万）
    assert abs(cost_cny("deepseek-flash", cache_hit=1_000_000, cache_miss=1_000_000, output=1_000_000, at=off_peak) - 5.02) < 1e-9
    peak = datetime(2026, 11, 16, 10, 0, tzinfo=CST)
    assert abs(cost_cny("deepseek-v4-flash", cache_hit=0, cache_miss=1_000_000, output=0, at=peak) - 2.0) < 1e-9  # 旧名按 flash，高峰 ×2
    assert cost_cny("some-other-model", cache_hit=1, cache_miss=1, output=1, at=peak) is None


def test_extract_usage_from_langchain_message():
    assert extract_usage(reply()) == {"input": 10_000, "hit": 9_000, "miss": 1_000, "output": 200, "reasoning": 0}
    assert extract_usage(AIMessage("没有 usage")) is None


def test_store_report(tmp_path):
    store = UsageStore(tmp_path / "u.sqlite")
    now = datetime(2026, 11, 16, 20, 0, tzinfo=CST)
    store.record(reply(), purpose="chat", model="deepseek-flash", thread_id="t1", latency_ms=1200, at=now)
    store.record(reply(inp=20_000, hit=0, out=500), purpose="summary", model="deepseek-flash", thread_id="t1", at=now)
    store.record(AIMessage("无用量"), purpose="chat", model="deepseek-flash", thread_id="t2", at=now)
    store.record(reply(), purpose="chat", model="deepseek-flash", at=now - timedelta(days=10))  # 范围外

    r = store.report(7, now=now)
    t = r["total"]
    assert t["calls"] == 3 and t["no_usage"] == 1
    assert t["input"] == 30_000 and t["hit"] == 9_000 and t["hitRate"] == 0.3
    assert r["byPurpose"]["summary"]["calls"] == 1
    assert r["daily"][0]["day"] == "2026-11-16"
    assert r["threads"][0]["thread_id"] == "t1" and r["threads"][0]["calls"] == 2
    expected = (9_000 * 0.02 + 1_000 * 1 + 200 * 4 + 20_000 * 1 + 500 * 4) / 1_000_000
    assert abs(t["cost"] - round(expected, 4)) < 1e-9


async def test_graph_records_each_model_call(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver

    from szu_agent.agent.graph import build_graph

    store = UsageStore(tmp_path / "u.sqlite")
    llm = ScriptedChatModel(script=[reply("你好")])
    graph = build_graph(llm, [], profile=ProfileStore(tmp_path / "p.json"), checkpointer=InMemorySaver(), usage=store)
    await graph.ainvoke({"messages": [("user", "hi")]}, {"configurable": {"thread_id": "abc"}})
    r = store.report(1)
    assert r["total"]["calls"] == 1 and r["recent"][0]["thread_id"] == "abc" and r["recent"][0]["purpose"] == "chat"
    assert r["recent"][0]["latency_ms"] is not None


def test_usage_api_and_page(home):
    from szu_agent.web.app import create_app

    llm = ScriptedChatModel(script=[reply("你好")])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "t-usage", "message": "你好"}) as resp:
            for _ in resp.iter_lines():
                pass
        data = client.get("/api/usage?days=7").json()
        assert data["total"]["calls"] == 1 and data["total"]["hitRate"] == 0.9
        assert data["threads"][0]["title"]  # 标题来自历史会话
        assert "source" in data["pricing"]
        assert "模型用量" in client.get("/usage").text


# ------------------------------ 每日额度 ------------------------------ #

def test_spent_today_uses_beijing_day(tmp_path):
    store = UsageStore(tmp_path / "u.sqlite")
    now = datetime(2026, 11, 17, 0, 30, tzinfo=CST)             # 北京时间 17 日 0:30
    yesterday = datetime(2026, 11, 16, 23, 50, tzinfo=CST)      # 16 日 23:50（UTC 已经是 16 日 15:50）
    store.record(reply(inp=1_000_000, hit=0, out=0), purpose="chat", model="deepseek-flash", user_id="a", at=yesterday)
    store.record(reply(inp=100_000, hit=0, out=0), purpose="chat", model="deepseek-flash", user_id="a", at=now)
    store.record(reply(inp=100_000, hit=0, out=0), purpose="chat", model="deepseek-flash", user_id="b", at=now)
    assert abs(store.spent_today("a", now=now) - 0.1) < 1e-9       # 只算 17 日 0 点之后、只算自己
    assert store.report(7, now=now)["byUser"][0]["user_id"] == "a"


def test_daily_quota_blocks_new_messages(home, monkeypatch):
    from szu_agent.web.app import create_app

    monkeypatch.setenv("SZU_AGENT_DAILY_QUOTA_CNY", "0.01")
    big = AIMessage("好的", response_metadata={"model_name": "deepseek-flash"},
                    usage_metadata={"input_tokens": 20_000, "output_tokens": 0, "total_tokens": 20_000})  # 空闲价 0.02 元
    llm = ScriptedChatModel(script=[big])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        q = client.get("/api/quota").json()
        assert q["enabled"] and q["limitCny"] == 0.01 and q["exhausted"] is False
        with client.stream("POST", "/api/chat", json={"thread_id": "t", "message": "你好"}) as resp:
            for _ in resp.iter_lines():
                pass
        q = client.get("/api/quota").json()
        assert q["exhausted"] is True and q["remainingCny"] == 0
        r = client.post("/api/chat", json={"thread_id": "t", "message": "再来"})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "DAILY_QUOTA_EXCEEDED"
        # 确认不受额度限制（这里没有待确认的操作，所以是 409 而不是 429）
        assert client.post("/api/resume", json={"thread_id": "t", "approved": True}).status_code == 409


def test_quota_off_by_default_in_single_user(home, monkeypatch):
    from szu_agent.web.app import create_app

    monkeypatch.delenv("SZU_AGENT_DAILY_QUOTA_CNY", raising=False)
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        assert client.get("/api/quota").json() == {"enabled": False}
