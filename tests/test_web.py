"""Web 接口测试：SSE 事件序列、确认中断与恢复、历史恢复、记忆接口。"""

import json
import re

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from szu_agent.agent.profile import ProfileStore
from szu_agent.web.app import create_app

from .conftest import ScriptedChatModel, ai_tool_call
from .test_e2e import confirm_with_token_from_history

RESERVE = {"campus": "粤海校区", "project": "羽毛球", "date": "2026-11-15", "slot": "20:00-21:00", "field": "羽毛球场 A4"}


def events(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def test_chat_confirm_resume_history(logged_in, home):
    llm = ScriptedChatModel(script=[
        ai_tool_call("sports_reserve_preview", RESERVE, "c1"),
        AIMessage(content="A4 场 **20:00-21:00**，确认吗？"),
        confirm_with_token_from_history,
        AIMessage(content="已提交"),
    ])
    profile = ProfileStore(home / "p.json")
    with TestClient(create_app(mock=True, llm=llm, profile=profile)) as client:
        assert client.get("/api/meta").json() == {"mock": True, "historyDays": 30, "multiuser": False}
        assert "爱小荔" in client.get("/").text

        with client.stream("POST", "/api/chat", json={"thread_id": "w1", "message": "约羽毛球"}) as r:
            evs = events(r)
        types = [e["type"] for e in evs]
        assert types[0] == "tool_start" and evs[0]["name"] == "sports_reserve_preview"
        assert {"type": "tool_end", "id": "c1", "name": "sports_reserve_preview", "ok": True} in evs
        assert "token" in types and types[-2:] == ["message", "done"]

        # 没有待确认操作时不能 resume
        assert client.post("/api/resume", json={"thread_id": "w1", "approved": True}).status_code == 409

        with client.stream("POST", "/api/chat", json={"thread_id": "w1", "message": "确认"}) as r:
            evs = events(r)
        confirm = next(e for e in evs if e["type"] == "confirm")
        assert confirm["actions"][0]["preview"]["field"] == "羽毛球场 A4"

        # 刷新页面后能恢复出待确认卡片
        hist = client.get("/api/history/w1").json()
        assert hist["pending"]["actions"][0]["tool"] == "sports_reserve_confirm"
        assert [m["role"] for m in hist["messages"]][:3] == ["user", "tool", "assistant"]

        with client.stream("POST", "/api/resume", json={"thread_id": "w1", "approved": True}) as r:
            evs = events(r)
        end = next(e for e in evs if e["type"] == "tool_end")
        assert end["name"] == "sports_reserve_confirm" and end["ok"] is True
        assert evs[-2] == {"type": "message", "text": "已提交"}
        assert client.get("/api/history/w1").json()["pending"] is None


def test_today_and_profile(logged_in, home):
    profile = ProfileStore(home / "p.json")
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=profile)) as client:
        today = client.get("/api/today").json()
        assert today["courses"]["ok"] is True and "dorm" not in today

        profile.set("dorm_building", "红豆斋")
        profile.set("dorm_room", "838")
        today = client.get("/api/today").json()
        assert today["electricity"]["data"]["remainingKwh"] == 28.9

        items = client.get("/api/profile").json()["items"]
        assert {"key": "dorm_room", "label": "宿舍房间号", "value": "838"} in items
        assert client.delete("/api/profile/dorm_room").json() == {"ok": True}
        assert client.delete("/api/profile/student_id").status_code == 404
        assert "dorm_room" not in profile.load()


def test_llm_error_is_reported_not_raised(home):
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "e", "message": "hi"}) as r:
            evs = events(r)
        assert evs[0]["type"] == "error" and evs[-1] == {"type": "done"}


def test_login_required_then_qr_login(home):
    """未登录 -> 工具返回 LOGIN_REQUIRED（SSE 带 code，前端据此弹卡片）-> 扫码 -> 凭证共享给 MCP 子进程。"""
    llm = ScriptedChatModel(script=[
        ai_tool_call("course_today", {}, "k1"), AIMessage(content="请先登录"),
        ai_tool_call("course_today", {}, "k2"), AIMessage(content="今天有马原"),
    ])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        assert client.get("/api/auth").json()["loggedIn"] is False
        with client.stream("POST", "/api/chat", json={"thread_id": "L", "message": "今天有什么课"}) as r:
            end = next(e for e in events(r) if e["type"] == "tool_end")
        assert end["ok"] is False and end["code"] == "LOGIN_REQUIRED"

        qr = client.post("/api/auth/qr").json()
        assert qr["ok"] and qr["image"].startswith("data:image/")
        states = [client.get(f"/api/auth/qr/{qr['loginId']}").json()["state"] for _ in range(5)]
        assert states == ["waiting", "waiting", "scanned", "scanned", "confirmed"]
        assert client.get(f"/api/auth/qr/{qr['loginId']}").json()["state"] == "expired"  # 用过即失效
        assert client.get("/api/auth").json()["loggedIn"] is True

        with client.stream("POST", "/api/chat", json={"thread_id": "L", "message": "我登录好了，请继续刚才的事"}) as r:
            end = next(e for e in events(r) if e["type"] == "tool_end")
        assert end["ok"] is True  # MCP 子进程读到了 Web 进程写的会话

        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert client.get("/api/auth").json()["loggedIn"] is False


def test_password_login_endpoint(home):
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        bad = client.post("/api/auth/password", json={"username": "2020123456", "password": "wrong"}).json()
        assert bad["ok"] is False and bad["error"]["code"] == "LOGIN_FAILED"
        assert client.post("/api/auth/password", json={"username": "2020123456", "password": "x"}).json() == {"ok": True}
        assert client.get("/api/auth").json()["method"] == "password"
        assert "x" not in [c["value"] for c in json.loads((home / "session-mock.json").read_text())["cookies"]]


def test_mfa_required_then_verify(logged_in, home, monkeypatch):
    monkeypatch.setenv("SZU_MOCK_MFA", "1")
    llm = ScriptedChatModel(script=[
        ai_tool_call("course_today", {}, "m1"), AIMessage(content="需要二次验证"),
        ai_tool_call("course_today", {}, "m2"), AIMessage(content="今天有马原"),
    ])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "M", "message": "今天有什么课"}) as r:
            end = next(e for e in events(r) if e["type"] == "tool_end")
        assert end["code"] == "MFA_REQUIRED" and end["target"].startswith("https://")

        start = client.post("/api/auth/mfa", json={"target": end["target"]}).json()
        assert start["needed"] and {m["name"] for m in start["methods"]} >= {"企业微信验证码", "短信验证码"}
        mid = start["mfaId"]
        assert client.post(f"/api/auth/mfa/{mid}/send", json={"method": "4"}).json()["ok"]
        bad = client.post(f"/api/auth/mfa/{mid}/submit", json={"method": "4", "secret": "111111"}).json()
        assert bad["error"]["code"] == "MFA_FAILED"
        assert client.post(f"/api/auth/mfa/{mid}/submit", json={"method": "4", "secret": "000000"}).json() == {"ok": True}
        assert client.post(f"/api/auth/mfa/{mid}/send", json={"method": "4"}).status_code == 404  # 用完即失效

        with client.stream("POST", "/api/chat", json={"thread_id": "M", "message": "我验证好了，请继续刚才的事"}) as r:
            end = next(e for e in events(r) if e["type"] == "tool_end")
        assert end["ok"] is True


def test_notice_attachments_list_and_download(logged_in, home):
    from urllib.parse import quote, unquote

    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        files = client.get("/api/notices/590201/attachments").json()["attachments"]
        assert [f["name"] for f in files] == ["国家奖学金申请审批表.docx"]
        r = client.get(f"/api/notices/attachment?url={quote(files[0]['url'])}&name={quote(files[0]['name'])}")
        assert r.status_code == 200 and "演示模式" in r.text
        assert unquote(r.headers["content-disposition"].split("UTF-8''")[1]) == "国家奖学金申请审批表.docx"
        # 只能下载公文通里的文件：其他地址（包括内网）一律拒绝，防止被当成跳板
        for bad in ["http://192.168.84.3:9090/cgcSims/", "https://www1.szu.edu.cn/manage/x", "https://evil.com/board/a.pdf"]:
            assert client.get(f"/api/notices/attachment?url={quote(bad)}").status_code == 403
        assert client.get("/api/notices/abc/attachments").status_code == 400


def test_turn_survives_client_disconnect(logged_in, home):
    """浏览器连接被中间代理掐断时，这一轮对话仍在后台跑完；之后接着问不会因为残缺历史报 400。"""
    import time as _time

    def slow_reply(messages):
        _time.sleep(1.5)  # 模拟推理很久、这段时间没有任何输出
        return AIMessage(content="这周末有百团大战")

    llm = ScriptedChatModel(script=[ai_tool_call("campus_events", {"query": "社团"}, "s1"), slow_reply,
                                    AIMessage(content="第二次也正常")])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "D", "message": "这周末有什么社团活动？"}) as r:
            for line in r.iter_lines():
                if '"tool_end"' in line:
                    break  # 工具刚结束、AI 还在想的时候，浏览器断开
        hist = client.get("/api/history/D").json()
        for _ in range(50):
            if not hist["running"]:
                break
            _time.sleep(0.1)
            hist = client.get("/api/history/D").json()
        assert hist["running"] is False
        assert hist["messages"][-1] == {"role": "assistant", "text": "这周末有百团大战"}  # 后台跑完了
        with client.stream("POST", "/api/chat", json={"thread_id": "D", "message": "这周末有什么社团活动？"}) as r:
            evs = events(r)
        assert {"type": "message", "text": "第二次也正常"} in evs and not any(e["type"] == "error" for e in evs)


def test_busy_thread_rejects_second_message(logged_in, home):
    import time as _time

    def slow(messages):
        _time.sleep(1)
        return AIMessage(content="ok")

    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[slow]), profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "B", "message": "hi"}) as r:
            assert client.post("/api/chat", json={"thread_id": "B", "message": "again"}).status_code == 409
            events(r)


async def test_heartbeat_when_idle():
    import asyncio

    from szu_agent.web.app import sse_from_queue

    q: asyncio.Queue = asyncio.Queue()
    gen = sse_from_queue(q, heartbeat=0.05)
    assert await gen.__anext__() == ": ping\n\n"  # 没有事件时发心跳
    q.put_nowait({"type": "done"})
    assert "done" in await gen.__anext__()


def test_payment_card_flow(logged_in, home):
    """预约成功 -> SSE 带订单信息（前端弹付款卡片）-> 轮询付款状态直到已支付；刷新页面后也能恢复卡片。"""
    llm = ScriptedChatModel(script=[
        ai_tool_call("sports_reserve_preview", RESERVE, "c1"), AIMessage(content="确认吗？"),
        confirm_with_token_from_history, AIMessage(content="已提交，记得付款"),
    ])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "P", "message": "约羽毛球"}) as r:
            events(r)
        with client.stream("POST", "/api/chat", json={"thread_id": "P", "message": "确认"}) as r:
            events(r)
        with client.stream("POST", "/api/resume", json={"thread_id": "P", "approved": True}) as r:
            end = next(e for e in events(r) if e["type"] == "tool_end")
        booking = end["booking"]
        assert booking["field"] == "羽毛球场 A4" and booking["slot"] == "20:00-21:00"

        states = [client.get(f"/api/sports/payment/{booking['orderNo']}").json() for _ in range(3)]
        assert states[0]["booking"]["awaitingPayment"] and states[0]["sportsFundCny"] == 20.0
        assert states[-1]["booking"]["paid"]  # 演示后端：第 3 次检查时视为已付款

        hist = client.get("/api/history/P").json()["messages"]
        assert any(m.get("booking", {}).get("orderNo") == booking["orderNo"] for m in hist)
        assert client.get("/api/sports/payment/..%2Fetc").status_code in (400, 404)



def test_single_confirmation_in_one_turn(logged_in, home):
    """预览后 AI 立即发起提交，界面只弹一次确认卡片；中间没有“确定吗”的文字回复。"""
    llm = ScriptedChatModel(script=[
        ai_tool_call("sports_reserve_preview", RESERVE, "c1"),
        confirm_with_token_from_history,  # 不经过用户回复，直接提交
        AIMessage(content="已提交"),
    ])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "S", "message": "约周日晚8点A4羽毛球"}) as r:
            evs = events(r)
        assert [e["type"] for e in evs if e["type"] in ("message", "confirm")] == ["confirm"]
        with client.stream("POST", "/api/resume", json={"thread_id": "S", "approved": True}) as r:
            evs = events(r)
        assert next(e for e in evs if e["type"] == "tool_end")["booking"]["field"] == "羽毛球场 A4"


def test_pay_card_cancel_button(logged_in, home):
    """付款卡片上的取消按钮：直接调后端（用户亲手点击），取消后付款状态同步为已取消，重复点击会被拒绝。"""
    llm = ScriptedChatModel(script=[ai_tool_call("sports_reserve_preview", RESERVE, "c1"), confirm_with_token_from_history,
                                    AIMessage(content="已提交")])
    with TestClient(create_app(mock=True, llm=llm, profile=ProfileStore(home / "p.json"))) as client:
        with client.stream("POST", "/api/chat", json={"thread_id": "X", "message": "约A4"}) as r:
            events(r)
        with client.stream("POST", "/api/resume", json={"thread_id": "X", "approved": True}) as r:
            order = next(e for e in events(r) if e["type"] == "tool_end")["booking"]["orderNo"]

        assert client.get(f"/api/sports/payment/{order}").json()["booking"]["cancellable"] is True
        done = client.post(f"/api/sports/cancel/{order}").json()
        assert done["ok"] and done["cancelled"] and done["field"] == "羽毛球场 A4"
        assert client.get(f"/api/sports/payment/{order}").json()["booking"]["cancelled"] is True
        again = client.post(f"/api/sports/cancel/{order}").json()
        assert again["error"]["code"] == "SPORTS_ALREADY_CANCELLED"
        assert client.post("/api/sports/cancel/..%2F..").status_code in (400, 404)


def test_weather_endpoint(home):
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        w = client.get("/api/weather").json()
        assert w["ok"] and w["campus"] == "粤海校区" and {"tempC", "kind", "todayMax", "rainChance"} <= set(w)
        assert client.get("/api/weather?campus=丽湖校区").json()["campus"] == "丽湖校区"


def test_recharge_card_endpoint(home):
    profile = ProfileStore(home / "p.json")
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=profile)) as client:
        r = client.get("/api/electricity/recharge").json()
        assert r["ok"] and r["remainingKwh"] is None and r["qrSvg"].startswith("<svg")  # 没有宿舍信息也给二维码
        profile.set("dorm_building", "红豆斋")
        profile.set("dorm_room", "838")
        r = client.get("/api/electricity/recharge").json()
        assert r["remainingKwh"] == 28.9 and r["dailyKwh"] == 6.4 and r["daysLeft"] == 4.5
        assert "weixiao.qq.com" in r["payUrl"] and "openid" not in r["payUrl"] and "卡包" in r["paySteps"]


def test_demo_meta_links_to_real_service(home, monkeypatch):
    """演示站要告诉访客真实服务在哪（曾有人把演示站当真实服务、输入了真实密码）。真实服务不带这个字段。"""
    monkeypatch.setenv("SZU_AGENT_REAL_URL", "https://example.test")
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        assert client.get("/api/meta").json()["realUrl"] == "https://example.test"
        assert 'id="demoBanner"' in client.get("/").text


def test_versioned_static_cached_long(home):
    # 首页给脚本加内容哈希：带哈希的地址可以长期缓存，不带的每次确认
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        html = client.get("/").text
        url = re.search(r'/static/app\.js\?v=[0-9a-f]+', html).group(0)
        assert "immutable" in client.get(url).headers["cache-control"]
        assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
