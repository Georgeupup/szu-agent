"""MCP Server 测试：演示后端 + 内存中的 MCP 客户端。"""

from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session

from szu_agent.campus import SessionStore
from szu_agent.campus.mock import MockCampus
from szu_agent.mcp_server import PendingStore, create_server

from .conftest import mock_login


@asynccontextmanager
async def mcp_client(store: SessionStore):
    # anyio 的 cancel scope 必须在同一个 task 里进出，所以不用 async fixture，而是在测试里 async with
    async with create_connected_server_and_client_session(create_server(MockCampus(store))) as session:
        async def call(name, **args):
            return (await session.call_tool(name, args)).structuredContent

        call.session = session
        yield call


async def test_write_tools_are_annotated_destructive(logged_in):
    async with mcp_client(logged_in) as client:
        tools = {t.name: t for t in (await client.session.list_tools()).tools}
        assert tools["sports_reserve_confirm"].annotations.destructiveHint is True
        assert tools["campus_events"].annotations.readOnlyHint is True
        assert {"campus", "project", "date"} <= set(tools["sports_slots"].inputSchema["properties"])
        assert not any("password" in str(t.inputSchema) for t in tools.values())  # 没有任何工具接收密码


async def test_login_required_is_reported_with_hint(home):
    store = SessionStore(mock=True)
    async with mcp_client(store) as client:
        res = await client("campus_events", query="社团")
        assert res["ok"] is False and res["error"]["code"] == "LOGIN_REQUIRED"
        assert "登录卡片" in res["error"]["agentHint"]
        mock_login(store)  # 用户在界面上登录后，同一个 Server 进程立刻可用
        assert (await client("campus_events", query="社团"))["ok"] is True


async def test_campus_events_filters_and_dedupes(logged_in):
    async with mcp_client(logged_in) as client:
        res = await client("campus_events", query="社团")
        titles = [n["title"] for n in res["data"]["notices"]]
        assert titles and all("社团" in t for t in titles)
        assert res["data"]["lectures"][0]["id"] == "lec-1121"

        ids = [n["id"] for n in (await client("campus_events"))["data"]["notices"]]
        assert len(ids) == len(set(ids)) and "590010" not in ids  # “期中教学检查”不是活动


async def test_notice_view_returns_content(logged_in):
    async with mcp_client(logged_in) as client:
        assert "文山湖" in (await client("notice_view", notice_id="590101"))["data"]["content"]


async def test_reserve_requires_field_when_multiple(logged_in):
    async with mcp_client(logged_in) as client:
        res = await client("sports_reserve_preview", campus="粤海校区", project="羽毛球", date="2026-11-15", slot="20:00-21:00")
        assert res["error"]["code"] == "SPORTS_FIELD_SELECTION_REQUIRED"
        assert res["error"]["fields"] == ["羽毛球场 A4", "羽毛球场 A5"]


async def test_reserve_token_is_single_use(logged_in):
    async with mcp_client(logged_in) as client:
        preview = await client("sports_reserve_preview", campus="粤海校区", project="羽毛球",
                               date="2026-11-15", slot="20:00-21:00", field="羽毛球场 A4")
        data = preview["data"]
        assert data["preview"]["submitted"] is False and data["preview"]["field"] == "羽毛球场 A4"
        first = await client("sports_reserve_confirm", confirmation_token=data["confirmationToken"])
        assert first["ok"] is True and first["data"]["submitted"] is True and first["data"]["field"] == "羽毛球场 A4"
        again = await client("sports_reserve_confirm", confirmation_token=data["confirmationToken"])
        assert again["error"]["code"] == "TOKEN_INVALID"


async def test_confirm_rejects_forged_and_cross_kind_tokens(logged_in):
    async with mcp_client(logged_in) as client:
        assert (await client("sports_reserve_confirm", confirmation_token="made-up"))["error"]["code"] == "TOKEN_INVALID"
        # 先约一个，再取消它（历史上已完成的预约不能取消）
        p = await client("sports_reserve_preview", campus="粤海校区", project="羽毛球", date="2026-11-15", slot="20:00-21:00", field="羽毛球场 A4")
        order = (await client("sports_reserve_confirm", confirmation_token=p["data"]["confirmationToken"]))["data"]["orderNo"]
        token = (await client("sports_cancel_preview", order_no=order))["data"]["confirmationToken"]
        assert (await client("sports_reserve_confirm", confirmation_token=token))["error"]["code"] == "TOKEN_INVALID"
        assert (await client("sports_cancel_confirm", confirmation_token=token))["data"]["cancelled"] is True
        bookings = (await client("sports_bookings"))["data"]["items"]
        assert next(b for b in bookings if b["orderNo"] == order)["status"] == "取消预约"
        assert "wid" not in bookings[0]  # 内部编号不给 LLM
        refused = await client("sports_cancel_preview", order_no="202611091405161043")
        assert refused["error"]["code"] == "SPORTS_NOT_CANCELLABLE"


async def test_electricity_and_course(logged_in):
    async with mcp_client(logged_in) as client:
        res = await client("electricity_query", building="红豆斋", room="838")
        assert res["data"]["remainingKwh"] == 28.9 and len(res["data"]["recentRecords"]) == 3
        buy = await client("electricity_purchases", building="红豆斋", room="838")
        assert buy["data"]["totalCny"] == 150.0 and "buyer" not in str(buy)
        course = await client("course_today", date="2026-11-13")
        assert course["data"]["items"][0]["courseName"] == "马克思主义基本原理"
        assert (await client("course_today", date="13/11"))["error"]["code"] == "BAD_DATE"


def test_pending_store_expiry(monkeypatch):
    store = PendingStore()
    token = store.create("reserve", {})
    monkeypatch.setattr(PendingStore, "TTL", -1)
    store2 = PendingStore()
    assert store2.pop(store2.create("reserve", {}), "reserve") is None
    assert store.pop(token, "reserve") is not None



async def test_notice_search_filters_and_read_many(logged_in):
    async with mcp_client(logged_in) as client:
        res = await client("notice_search", keyword="奖学金")
        assert res["data"]["matched"] == 4 and res["data"]["stats"]["byKind"]
        apply_ = await client("notice_search", keyword="奖学金", kind="申请/报名", audience="本科生")
        titles = [i["title"] for i in apply_["data"]["items"]]
        assert titles and all("研究生" not in t for t in titles)
        many = await client("notice_read_many", notice_ids=["590201", "590202"])
        first = many["data"]["notices"][0]
        assert "2026年11月20日前" in first["content"] and "2020123456" not in first["content"]
        assert first["attachments"] == ["国家奖学金申请审批表.docx"]
