"""多人模式：身份识别、会话 Cookie、以及最重要的——B 看不到、用不到 A 的任何东西。"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from mcp.shared.memory import create_connected_server_and_client_session

from szu_agent import users
from szu_agent.campus import SessionStore
from szu_agent.campus.mock import MockCas
from szu_agent.mcp_server import PendingStore, create_server

from .conftest import ScriptedChatModel

A, B = "2099000001", "2099000002"


@pytest.fixture
def multi(monkeypatch):
    monkeypatch.setenv("SZU_AGENT_MULTIUSER", "1")
    monkeypatch.setenv("SZU_AGENT_SECRET", "test-secret")
    monkeypatch.setenv("SZU_AGENT_MOCK", "1")


def test_user_id_is_keyed_hash(monkeypatch):
    monkeypatch.setenv("SZU_AGENT_SECRET", "s1")
    a1 = users.user_id_for(A)
    assert a1 == users.user_id_for(" 2099000001 ") and len(a1) == 32 and A not in a1
    assert users.user_id_for(B) != a1
    monkeypatch.setenv("SZU_AGENT_SECRET", "s2")
    assert users.user_id_for(A) != a1  # 没有服务器密钥就算不出对应关系
    with pytest.raises(ValueError):
        users.user_dir("../etc")


def test_pending_token_bound_to_user():
    who = {"u": "a"}
    store = PendingStore(lambda: who["u"])
    token = store.create("reserve", {"x": 1})
    who["u"] = "b"
    assert store.pop(token, "reserve") is None  # B 拿到 A 的令牌也确认不了
    who["u"] = "a"
    assert store.pop(token, "reserve").params == {"x": 1}


async def test_mcp_routes_by_request_meta(multi):
    """工具进程按请求元数据里的 user_id 选登录凭证；多人模式下没带身份就拒绝，绝不退回本机账号。"""
    ua, ub = users.user_id_for(A), users.user_id_for(B)
    await MockCas(SessionStore(users.session_path(ua, mock=True))).password_login(A, "pw")

    async with create_connected_server_and_client_session(create_server()) as session:
        async def auth(meta):
            res = await session.call_tool("auth_status", {}, meta=meta)
            return res.structuredContent
        assert (await auth({"user_id": ua}))["data"]["loggedIn"] is True
        assert (await auth({"user_id": ub}))["data"]["loggedIn"] is False
        assert (await auth(None))["error"]["code"] == "LOGIN_REQUIRED"
        assert (await auth({"user_id": "local"}))["error"]["code"] == "LOGIN_REQUIRED"


def login(client, student_no):
    client.cookies.clear()
    r = client.post("/api/auth/password", json={"username": student_no, "password": "pw"}).json()
    assert r["ok"], r
    token = client.cookies.get("szu_sid")
    assert token
    return token


def use(client, token):
    client.cookies.clear()
    client.cookies.set("szu_sid", token)


def chat(client, thread_id, text):
    with client.stream("POST", "/api/chat", json={"thread_id": thread_id, "message": text}) as resp:
        assert resp.status_code == 200
        return [json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:")]


def test_users_are_isolated(multi, home):
    from szu_agent.web.app import create_app

    llm = ScriptedChatModel(script=[
        AIMessage("", tool_calls=[{"name": "remember_user_info", "args": {"key": "college", "value": "计算机与软件学院"}, "id": "c1"}]),
        AIMessage("记住了"),
    ])
    with TestClient(create_app(mock=True, llm=llm)) as client:
        # 没登录：所有个人数据接口都是 401
        assert client.get("/api/threads").status_code == 401
        assert client.post("/api/chat", json={"thread_id": "t1", "message": "hi"}).status_code == 401
        assert client.get("/api/auth").json()["appSession"] is False
        assert client.get("/api/meta").json()["multiuser"] is True

        ta = login(client, A)
        ua = users.user_id_for(A)
        assert client.get("/api/me").json() == {"multiuser": True, "loggedIn": True, "userId": ua}
        assert client.get("/api/auth").json()["loggedIn"] is True  # 工具进程用的是 A 的学校凭证
        assert not list((users.agent_home() / "pending").glob("*.json"))  # 临时凭证已搬走
        assert users.session_path(ua, mock=True).exists()

        chat(client, "t1", "我是计软的")
        assert client.get("/api/profile").json()["items"][0]["value"] == "计算机与软件学院"
        assert [t["id"] for t in client.get("/api/threads").json()["items"]] == ["t1"]
        assert len(client.get("/api/history/t1").json()["messages"]) >= 2

        tb = login(client, B)
        ub = users.user_id_for(B)
        assert ub != ua
        assert client.get("/api/threads").json()["items"] == []
        assert client.get("/api/history/t1").json()["messages"] == []      # 同样的对话 id，读不到 A 的
        assert client.get("/api/profile").json()["items"] == []            # A 的记忆不会出现在 B 这里
        assert client.delete("/api/threads/t1").json()["ok"]                # 删“自己的 t1”不影响 A

        # 学校凭证按人分开：删掉 A 的学校登录，B 不受影响
        users.session_path(ua, mock=True).unlink()
        assert client.get("/api/auth").json()["loggedIn"] is True
        use(client, ta)
        assert client.get("/api/auth").json()["loggedIn"] is False
        assert [t["id"] for t in client.get("/api/threads").json()["items"]] == ["t1"]

        # 用量统计只给管理员；管理员看不到对话标题
        assert client.get("/api/usage").status_code == 403

        # 删除我的数据：A 的一切都没了，B 还在
        assert client.delete("/api/me").json()["ok"]
        assert client.get("/api/threads").status_code == 401  # 会话也一起删了
        assert not users.user_dir(ua).exists()
        use(client, tb)
        assert client.get("/api/me").json()["loggedIn"] is True


def test_admin_usage_hides_titles(multi, home, monkeypatch):
    from szu_agent.web.app import create_app

    llm = ScriptedChatModel(script=[AIMessage("你好", usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105})])
    monkeypatch.setenv("SZU_AGENT_ADMINS", users.user_id_for(B))
    with TestClient(create_app(mock=True, llm=llm)) as client:
        login(client, A)
        chat(client, "secret-topic", "我想问问心理咨询")
        login(client, B)
        r = client.get("/api/usage").json()
        assert r["users"] == 2 and r["total"]["calls"] == 1
        assert "心理" not in json.dumps(r, ensure_ascii=False)


def test_logout_revokes_session(multi, home):
    from szu_agent.web.app import create_app

    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]))) as client:
        token = login(client, A)
        assert client.post("/api/auth/logout").json()["ok"]
        use(client, token)  # 旧 Cookie 再拿来用也不行
        assert client.get("/api/threads").status_code == 401


# ------------------------------ 端到端：多人模式下约琴房 ------------------------------ #

def _tomorrow() -> str:
    from datetime import timedelta

    from szu_agent.campus.booth import beijing_now

    return (beijing_now().date() + timedelta(days=1)).isoformat()


def _confirm_booth(messages):
    """模型拿到预览后立即调用 confirm（和真实提示词要求的一样）。"""
    import re

    from langchain_core.messages import ToolMessage

    from szu_agent.agent.context import message_text

    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "booth_reserve_preview":
            token = re.search(r'"confirmationToken":\s*"([^"]+)"', message_text(m)).group(1)
            return AIMessage("", tool_calls=[{"name": "booth_reserve_confirm", "args": {"confirmation_token": token}, "id": "c2"}])
    raise AssertionError("没有预览结果")


def _sse(resp):
    return [json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:")]


def test_piano_room_reservation_end_to_end(multi, home):
    """真实使用路径：A 登录 → 填联系手机 → “帮我约明天的琴房”（不指定时段）→ 卡片上点选格子 → 确认 → 提交。
    验证身份一路传对：预览和确认在同一个人名下（令牌绑定用户），B 恢复不了 A 的确认卡片，B 的“我的预约”里没有 A 的记录。"""
    from szu_agent.web.app import create_app

    tomorrow = _tomorrow()
    llm = ScriptedChatModel(script=[
        AIMessage("", tool_calls=[{"name": "booth_reserve_preview",
                                   "args": {"booth_id": 11, "date": tomorrow, "grade": "2024"}, "id": "c1"}]),
        _confirm_booth,
        AIMessage("约好了"),
        # B 查自己的预约
        AIMessage("", tool_calls=[{"name": "booth_my_reservations", "args": {}, "id": "b1"}]),
        AIMessage("你没有预约"),
    ])
    with TestClient(create_app(mock=True, llm=llm)) as client:
        ta = login(client, A)
        assert client.put("/api/contact", json={"phone": "13912345678"}).json()["phone"] == "139****5678"

        with client.stream("POST", "/api/chat", json={"thread_id": "p1", "message": f"帮我约{tomorrow}的琴房"}) as r:
            evs = _sse(r)
        confirm = next(e for e in evs if e["type"] == "confirm")
        action = confirm["actions"][0]
        assert action["tool"] == "booth_reserve_confirm"
        free = action["preview"]["freeSlots"]
        assert free and "13912345678" not in json.dumps(confirm, ensure_ascii=False)  # 模型和界面只见打码手机号

        # B 拿着同一个对话 id 来确认：在 B 名下这个对话不存在
        tb = login(client, B)
        assert client.post("/api/resume", json={"thread_id": "p1", "approved": True, "slots": [free[0]["index"]]}).status_code == 409

        use(client, ta)
        with client.stream("POST", "/api/resume", json={"thread_id": "p1", "approved": True, "slots": [free[0]["index"]]}) as r:
            evs = _sse(r)
        end = next(e for e in evs if e["type"] == "tool_end" and e["name"] == "booth_reserve_confirm")
        assert end["ok"] is True, evs
        assert evs[-2] == {"type": "message", "text": "约好了"}

        # A 的预约记录真的写进了 A 的（演示）学校账号；B 的账号里没有
        use(client, tb)
        with client.stream("POST", "/api/chat", json={"thread_id": "q1", "message": "我约了什么"}) as r:
            evs = _sse(r)
        assert next(e for e in evs if e["type"] == "tool_end")["ok"] is True
    from szu_agent.agent.context import message_text

    mine_b = [m for m in llm.calls[-1] if getattr(m, "name", None) == "booth_my_reservations"]
    text_b = message_text(mine_b[-1])  # content 是分块列表，必须先转成文本再判断（否则 in 判断永远为假，断言空转）
    assert '"ok": true' in text_b
    assert f'"{free[0]["start"]} - {free[0]["end"]}"' not in text_b  # B 的预约里没有 A 刚约的时段


# ------------------------------ 登录时识别身份遇到二次验证（2026-11-16 部署后真实出现） ------------------------------ #

def test_login_identify_needs_mfa(multi, home, monkeypatch):
    """服务器 IP 对学校是“非常用地点”：扫码/密码登录后去读学号时学校要求二次验证。
    这时还没有应用会话，验证流程必须凭 mfaId 继续，验证通过后再识别身份、签发会话。"""
    from szu_agent.web.app import create_app

    monkeypatch.setenv("SZU_AGENT_MOCK_LOGIN_MFA", "1")
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]))) as client:
        r = client.post("/api/auth/password", json={"username": A, "password": "pw"}).json()
        assert r["ok"] and r["mfa"]["mfaId"] and r["mfa"]["methods"]
        assert client.cookies.get("szu_sid") is None             # 还没识别出是谁，不签发会话
        assert client.get("/api/me").json()["loggedIn"] is False
        mfa_id = r["mfa"]["mfaId"]
        assert client.post("/api/auth/mfa/wrong-id/send", json={"method": "4"}).status_code == 404

        assert client.post(f"/api/auth/mfa/{mfa_id}/send", json={"method": "4"}).json()["ok"]
        assert client.post(f"/api/auth/mfa/{mfa_id}/submit", json={"method": "4", "secret": "123"}).json()["ok"] is False
        done = client.post(f"/api/auth/mfa/{mfa_id}/submit", json={"method": "4", "secret": "000000"}).json()
        assert done == {"ok": True, "appLogin": True}
        assert client.get("/api/me").json() == {"multiuser": True, "loggedIn": True, "userId": users.user_id_for(A)}
        assert not list((users.agent_home() / "pending").glob("*.json"))  # 临时凭证已搬进用户目录
        assert users.session_path(users.user_id_for(A), mock=True).exists()

        # 扫码登录走同一条路
        client.cookies.clear()
        start = client.post("/api/auth/qr", json={"realm": "main"}).json()
        polls = [client.get(f"/api/auth/qr/{start['loginId']}").json() for _ in range(5)]
        assert polls[-1]["state"] == "confirmed" and polls[-1]["mfa"]["mfaId"]
