"""网络面试间（事务中心 venue-api）：解码、时段计算、登录与网络判断、工具输出。"""

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from szu_agent.campus import CampusError, SessionStore
from szu_agent.campus.booth import booth_slots, mask_bits, my_reservations, slot_range, unwrap, xor_decode
from szu_agent.campus.http import CampusHttp
from szu_agent.campus.mock import FakeBoothApi
from szu_agent.campus.service import RealCampus

from .test_mcp_server import mcp_client

FIXTURE = Path(__file__).parent / "fixtures" / "booth-xor.json"


def _bj_today() -> date:
    from szu_agent.campus.booth import beijing_now

    return beijing_now().date()


def test_xor_decode_matches_frontend_js():
    # booth-xor.json 是用前端 cC() 的同一段异或代码（node）生成的，包含中文和 emoji（UTF-16 代理对）
    data = unwrap(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert data == {"list": [{"id": 7, "name": "时光社区网络面试间1 😀", "date": "2026-11-16"}]}
    assert xor_decode(xor_decode("你好 a😀")) == "你好 a😀"  # 异或两次还原


def test_slot_math():
    assert slot_range("2026-11-16", 18, -10) == ("08:50", "09:20")   # 原 09:00-09:30，整体提前 10 分钟
    assert slot_range("2026-11-16", 0, -10) == ("23:50", "00:20")
    assert mask_bits(str((1 << 16) | (1 << 43))) == {16, 43}
    assert mask_bits(None) == set(range(48))


async def test_booth_slots_filters_advance_and_range():
    api = FakeBoothApi()
    today = _bj_today()
    # 今天 12:00：至少提前 2 小时 → 最早 14:00 开始的格（第 28 格 + offset -10 = 13:50 不行，第 29 格 14:20 可以）
    now = datetime.combine(today, datetime.min.time()).replace(hour=12)
    res = await booth_slots(api, 1, today, now=now)
    starts = [s["start"] for s in res["freeSlots"]]
    assert starts and min(starts) >= "14:00"
    assert all(s["start"] < "22:00" for s in res["freeSlots"])  # 只在开放格（08:00-22:00 那几格）里
    assert res["maxSlotsPerDay"] == 4 and res["minAdvanceHours"] == 2

    far = await booth_slots(api, 1, date.fromordinal(today.toordinal() + 5), now=now)
    assert far["freeSlots"] == [] and "不在可预约范围" in far["note"]


async def test_my_reservations_drops_personal_fields():
    res = await my_reservations(FakeBoothApi())
    text = json.dumps(res, ensure_ascii=False)
    assert "13800000000" not in text and "演示用户" not in text
    assert res["reservations"][0]["slots"] == ["14:50 - 15:20", "15:20 - 15:50"]


def _svc(tmp_path, handler) -> RealCampus:
    return RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))


def _swzx_server(*, vpn_session: bool, reauth: bool = False):
    """事务中心接口：没有 WebVPN 会话 → 被带去 WebVPN 认证页；有会话 → 场地系统 401 → /venue-api/login 自动完成。"""
    state = {"venue": False}

    def handler(req: httpx.Request):
        host, path = req.url.host, req.url.path
        if host == "swzx.webvpn.szu.edu.cn":
            if not vpn_session:
                return httpx.Response(302, headers={"Location": "https://webvpn.szu.edu.cn:443/vpn_key/update?origin=x"})
            if path == "/venue-api/login":
                if reauth:
                    return httpx.Response(302, headers={"Location": "https://authserver-443.webvpn.szu.edu.cn/authserver/reAuthCheck/reAuthLoginView.do"})
                state["venue"] = True
                return httpx.Response(302, headers={"Location": "https://swzx.webvpn.szu.edu.cn/", "Set-Cookie": "VENUE_JESSESIONID=1; Path=/"})
            if path == "/":
                return httpx.Response(200, text="<html></html>")
            if not state["venue"]:
                return httpx.Response(401, json={"message": "未登录"})
            return httpx.Response(200, json={"data": {"list": FakeBoothApi.BOOTHS}, "message": "ok", "status": 200})
        if host == "webvpn.szu.edu.cn":
            return httpx.Response(302, headers={"Location": "https://authserver-443.webvpn.szu.edu.cn/authserver/login?service=cb"})
        if path.endswith("/reAuthLoginView.do"):
            return httpx.Response(200, text="<html>二次验证</html>")
        return httpx.Response(200, text="<title>统一身份认证平台</title>")

    return handler, state


async def test_with_webvpn_session_venue_login_is_automatic(tmp_path):
    handler, state = _swzx_server(vpn_session=True)
    res = await _svc(tmp_path, handler).booth_list("粤海校区", "面试")
    assert [b["name"] for b in res["booths"]] == ["时光社区网络面试间1", "时光社区网络面试间2"] and state["venue"]


async def test_without_webvpn_session_asks_for_webvpn_login(tmp_path):
    handler, _ = _swzx_server(vpn_session=False)
    with pytest.raises(CampusError) as exc:
        await _svc(tmp_path, handler).booth_list()
    assert exc.value.code == "WEBVPN_LOGIN_REQUIRED"  # 界面据此弹出“登录学校 WebVPN”卡片


async def test_reauth_on_webvpn_also_asks_for_webvpn_login(tmp_path):
    """WebVPN 的二次验证单独做过不了（要和登录同一个会话），所以不报 MFA_REQUIRED，而是让用户重新登录 WebVPN。"""
    handler, _ = _swzx_server(vpn_session=True, reauth=True)
    with pytest.raises(CampusError) as exc:
        await _svc(tmp_path, handler).booth_list()
    assert exc.value.code == "WEBVPN_LOGIN_REQUIRED"


async def test_error_message_is_passed_through(tmp_path):
    def handler(req: httpx.Request):
        return httpx.Response(400, json={"message": "当前日期不可预约"})

    with pytest.raises(CampusError) as exc:
        await _svc(tmp_path, handler).booth_my_reservations()
    assert exc.value.code == "BOOTH_REQUEST_FAILED" and exc.value.message == "当前日期不可预约"


async def test_booth_tools_on_mock(logged_in):
    async with mcp_client(logged_in) as client:
        listing = await client("booth_list", campus="丽湖")
        assert listing["ok"] and listing["data"]["booths"][0]["outOfService"] is True
        tomorrow = date.fromordinal(_bj_today().toordinal() + 1).isoformat()
        slots = await client("booth_slots", booth_id=1, date=tomorrow)
        d = slots["data"]
        assert d["freeRanges"] and d["freeSlotCount"] > 0 and "freeSlots" not in d
        assert all(r.split("-")[0][-1] in "05" for r in d["freeRanges"])  # 都是 xx:20 / xx:50
        mine = await client("booth_my_reservations")
        assert mine["ok"] and "13800000000" not in json.dumps(mine, ensure_ascii=False)


# ------------------------------------------------------------------ 预约 / 取消（琴房、会议室、心灵小屋）

def _tomorrow() -> str:
    from szu_agent.campus.booth import beijing_now

    return (beijing_now().date() + __import__("datetime").timedelta(days=1)).isoformat()


async def test_reserve_and_cancel_piano_room_on_mock(logged_in):
    from szu_agent.campus.contact import ContactStore

    ContactStore().set_phone("13912345678")
    async with mcp_client(logged_in) as client:
        rooms = await client("booth_list", kind="琴房")
        assert [b["name"] for b in rooms["data"]["booths"]] == ["时光社区共享琴房1"]
        slots = (await client("booth_slots", booth_id=11, date=_tomorrow()))["data"]
        assert slots["needsInterviewProof"] is False
        start, end = slots["freeRanges"][0].split("-")
        # 只约第一格（半小时）
        end = (datetime.strptime(start, "%H:%M").replace(year=2000) + __import__("datetime").timedelta(minutes=30)).strftime("%H:%M")

        pv = await client("booth_reserve_preview", booth_id=11, date=_tomorrow(), start=start, end=end, grade="2024")
        assert pv["ok"], pv
        p = pv["data"]["preview"]
        assert p["phone"] == "139****5678" and "13912345678" not in json.dumps(pv, ensure_ascii=False)
        done = await client("booth_reserve_confirm", confirmation_token=pv["data"]["confirmationToken"])
        assert done["ok"] and done["data"]["submitted"] and done["data"]["reservation"]["slots"] == [f"{start} - {end}"]
        again = await client("booth_reserve_confirm", confirmation_token=pv["data"]["confirmationToken"])
        assert again["error"]["code"] == "TOKEN_INVALID"  # 一次性令牌

        # 刚约的格子不再空闲
        slots2 = (await client("booth_slots", booth_id=11, date=_tomorrow()))["data"]
        assert not any(r.startswith(f"{start}-") for r in slots2["freeRanges"])

        rid = done["data"]["reservation"]["id"]
        cp = await client("booth_cancel_preview", reservation_id=rid)
        assert cp["ok"]
        cc = await client("booth_cancel_confirm", confirmation_token=cp["data"]["confirmationToken"])
        assert cc["ok"] and cc["data"]["cancelled"] and cc["data"]["record"]["status"] == "CANCELLED"


async def test_reserve_validations(logged_in):
    from szu_agent.campus.contact import ContactStore

    async with mcp_client(logged_in) as client:
        interview = await client("booth_reserve_preview", booth_id=1, date=_tomorrow(), start="14:20", end="14:50", grade="2024")
        assert interview["error"]["code"] == "NOT_SUPPORTED_YET" and interview["error"]["url"]

        slots = (await client("booth_slots", booth_id=21, date=_tomorrow()))["data"]
        start = slots["freeRanges"][0].split("-")[0]
        h, m = map(int, start.split(":"))
        end = f"{h:02d}:{m + 30:02d}" if m + 30 < 60 else f"{h + 1:02d}:{m - 30:02d}"

        no_grade = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), start=start, end=end)
        assert no_grade["error"]["code"] == "BOOTH_GRADE_REQUIRED"
        no_phone = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), start=start, end=end, grade="2024")
        assert no_phone["error"]["code"] == "CONTACT_REQUIRED"  # 界面会弹出填手机号的卡片

        ContactStore().set_phone("13912345678")
        bad = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), start="03:00", end="04:00", grade="2024")
        assert bad["error"]["code"] == "BOOTH_SLOT_UNAVAILABLE" and bad["error"]["freeRanges"]
        too_long = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), start="08:20", end="11:20", grade="2024")
        assert too_long["error"]["code"] == "BOOTH_SLOT_UNAVAILABLE"


async def test_reserve_request_shape(tmp_path):
    """真实请求：POST /venue-api/boothReservation/add，JSON {boothId, phone, grade, date, timeList}（szu-booth-detail 的 je()）。"""
    from szu_agent.campus.booth import reserve
    from szu_agent.campus.contact import ContactStore

    fake = FakeBoothApi()
    sent = []

    def handler(req: httpx.Request):
        path = req.url.path.removeprefix("/venue-api")
        body = json.loads(req.content) if req.content else None
        if req.method == "POST":
            sent.append((path, body, req.headers.get("content-type")))
        return httpx.Response(200, json={"data": _sync(fake, req.method, path, dict(req.url.params), body), "message": "ok"})

    contact = ContactStore(tmp_path / "c.json")
    contact.set_phone("13912345678")
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)), contact)
    day = date.fromordinal(_bj_today().toordinal() + 1)
    noon = datetime.combine(_bj_today(), datetime.min.time()).replace(hour=12)
    from szu_agent.campus.booth import booth_slots

    free = (await booth_slots(svc._swzx, 11, day, now=noon))["freeSlots"]
    a, b = next((x, y) for x, y in zip(free, free[1:]) if y["index"] == x["index"] + 1)  # 第一段连续的两格
    res = await reserve(svc._swzx, contact, 11, day, a["start"], b["end"], "2024", confirm=True, now=noon)
    assert res["submitted"]
    path, body, ctype = sent[0]
    assert path == "/boothReservation/add" and ctype.startswith("application/json")
    assert body == {"boothId": 11, "phone": "13912345678", "grade": "2024", "date": day.isoformat(),
                    "timeList": [a["index"], b["index"]]}


def _sync(fake: FakeBoothApi, method: str, path: str, params: dict, body):
    """在 MockTransport 的同步 handler 里调用 FakeBoothApi（它的 api() 不做任何 await，可以直接驱动协程）。"""
    coro = fake.api(method, path, params=params, json_body=body)
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("FakeBoothApi.api 不应该挂起")


async def test_stale_webvpn_tgc_leading_to_reauth_never_shows_standalone_mfa(tmp_path):
    """旧版本留下的 authserver-443 CASTGC 会让第一个接口请求直接落在 WebVPN 的验证页：
    必须报 WEBVPN_LOGIN_REQUIRED（登录 + 验证同一个会话），不能报 MFA_REQUIRED（独立验证卡片注定失败）。"""
    def handler(req: httpx.Request):
        if req.url.host == "swzx.webvpn.szu.edu.cn":
            return httpx.Response(302, headers={"Location": "https://authserver-443.webvpn.szu.edu.cn/authserver/login?service=cb"})
        if req.url.path == "/authserver/login":
            return httpx.Response(302, headers={"Location": "https://authserver-443.webvpn.szu.edu.cn/authserver/reAuthCheck/reAuthLoginView.do"})
        return httpx.Response(200, text="<html>二次验证</html>")

    with pytest.raises(CampusError) as exc:
        await _svc(tmp_path, handler).booth_list()
    assert exc.value.code == "WEBVPN_LOGIN_REQUIRED"


async def test_reserve_by_picking_slots_on_card(logged_in):
    """没说时间：预览列出全部空闲格子（卡片上点选），提交时用卡片选的格号；没选就提交会被拒绝。"""
    from szu_agent.campus.contact import ContactStore

    ContactStore().set_phone("13912345678")
    async with mcp_client(logged_in) as client:
        pv = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), grade="2024")
        assert pv["ok"], pv
        p = pv["data"]["preview"]
        assert p["freeSlots"] and p["selectedSlots"] == [] and "time" not in p and p["maxSlotsPerDay"] == 4

        none = await client("booth_reserve_confirm", confirmation_token=pv["data"]["confirmationToken"])
        assert none["error"]["code"] == "BOOTH_SLOT_REQUIRED"

        pv = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), grade="2024")
        free = pv["data"]["preview"]["freeSlots"]
        a, b = next((x, y) for x, y in zip(free, free[1:]) if y["index"] == x["index"] + 1)
        done = await client("booth_reserve_confirm", confirmation_token=pv["data"]["confirmationToken"],
                            slots=[b["index"], a["index"]])
        assert done["ok"] and done["data"]["time"] == f"{a['start']}-{b['end']}" and "freeSlots" not in done["data"]

        pv = await client("booth_reserve_preview", booth_id=21, date=_tomorrow(), grade="2024")
        gap = await client("booth_reserve_confirm", confirmation_token=pv["data"]["confirmationToken"],
                           slots=[a["index"]])  # 刚被自己约走的格子
        assert gap["error"]["code"] == "BOOTH_SLOT_UNAVAILABLE"


def test_graph_writes_card_slot_choice_into_confirm():
    from szu_agent.agent.graph import apply_user_choices

    preview = {"freeSlots": [{"index": i, "start": "", "end": ""} for i in (20, 21, 22, 30)], "selectedSlots": [21]}
    actions = [{"tool": "booth_reserve_confirm", "preview": preview}]
    call = [{"name": "booth_reserve_confirm", "id": "c1", "args": {"confirmation_token": "t", "slots": [1, 2, 3]}}]
    # 用户点了 21、22：写进参数；模型自己填的 slots 被丢掉
    assert apply_user_choices(call, actions, {"approved": True, "slots": [22, 21]})[0]["args"] == {"confirmation_token": "t", "slots": [21, 22]}
    # 没点：用预览里预选的
    assert apply_user_choices(call, actions, {"approved": True})[0]["args"]["slots"] == [21]
    # 不在空闲格子里的值被过滤
    assert apply_user_choices(call, actions, {"approved": True, "slots": [5, 30]})[0]["args"]["slots"] == [30]
