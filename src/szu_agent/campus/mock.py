"""演示模式：用固定样例数据模拟学校系统，但走的是和真实模式同一套解析器和业务逻辑。

登录也是模拟的：扫码会在轮询几次后自动“确认”，账号密码随便填（密码填 wrong 会模拟失败）。
除电费外的功能都要求先登录，这样演示时能完整走一遍“需要登录 → 弹出登录卡片 → 继续任务”。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .cas import QrState
from .errors import CampusError
from .parsers import (
    build_courses, build_grade_report, courses_on, gs_term_code, parse_grade_page, term_key, lecture_progress, normalize_lecture, notice_stats,
    parse_library_book, parse_library_search, parse_notice_detail, parse_notice_list, parse_prebook_form,
)
from .contact import ContactStore
from .session import SessionStore

FIXTURES = Path(__file__).parent.parent / "mock_fixtures"
SPORTS_FIXTURE_TODAY = date(2026, 11, 14)  # sports.json 录制时的“今天”（开放日期 11-14、11-15）


def mock_today() -> date:
    """演示数据的“今天”：默认北京时间今天；测试用 SZU_AGENT_MOCK_TODAY 固定（MCP 子进程也继承）。"""
    if v := os.environ.get("SZU_AGENT_MOCK_TODAY"):
        return date.fromisoformat(v)
    from .booth import beijing_now
    return beijing_now().date()


def shift_dates(text: str, days: int) -> str:
    """把样例里所有 YYYY-MM-DD 平移 days 天：演示站一直是“今天、明天可约”，不会因为样例日期过期而约不到。"""
    if not days:
        return text
    return re.sub(r"\b20\d\d-\d\d-\d\d\b",
                  lambda m: (date.fromisoformat(m.group(0)) + timedelta(days=days)).isoformat(), text)


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fake_qr_svg() -> str:
    """一张看起来像二维码的图（固定随机种子，每次一样）。"""
    import random

    rnd = random.Random(7)
    n, s = 25, 8
    cells = []
    for y in range(n):
        for x in range(n):
            finder = any(x0 <= x < x0 + 7 and y0 <= y < y0 + 7 for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)))
            if finder:
                fx, fy = x % (n - 7) if x >= n - 7 else x, y % (n - 7) if y >= n - 7 else y
                on = fx in (0, 6) or fy in (0, 6) or (2 <= fx <= 4 and 2 <= fy <= 4)
            else:
                on = rnd.random() < 0.45
            if on:
                cells.append(f'<rect x="{x*s}" y="{y*s}" width="{s}" height="{s}"/>')
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-16 -16 {n*s+32} {n*s+32}"><rect x="-16" y="-16" width="100%" height="100%" fill="#fff"/><g fill="#1E1828">{"".join(cells)}</g></svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


class MockCas:
    """和 CasClient 同样的接口。演示模式不区分主登录和 WebVPN（事务中心的演示数据不要求 WebVPN）。"""

    def __init__(self, store: SessionStore, realm: str = "main") -> None:
        self.store = store
        self.realm = realm
        self.mfa_response = None
        self._polls = 0

    async def aclose(self) -> None:
        pass

    def _login(self, method: str, student_no: str = "2099000001") -> None:
        jar = httpx.Cookies()
        jar.set("MOCK_TGC", secrets.token_hex(8), domain="authserver.szu.edu.cn")
        # 演示模式没有真实学号：密码登录时把用户名当学号，扫码固定一个，用来演示/测试多人模式
        self.store.save(jar, method=method, loggedInAt=int(time.time()), mockStudentNo=student_no)

    async def qr_image(self) -> str:
        self._polls = 0
        return _fake_qr_svg()

    async def qr_poll(self) -> QrState:
        self._polls += 1
        if self._polls < 3:
            return "waiting"
        if self._polls < 5:
            return "scanned"
        self._login("qr")
        return "confirmed"

    async def password_login(self, username: str, password: str, *, remember: bool = True) -> None:
        if password == "wrong":
            raise CampusError("LOGIN_FAILED", "您提供的用户名或者密码有误")
        self._login("password", username)


class MockMfa:
    """模拟二次验证：验证码填 000000 通过，其他失败。"""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    async def aclose(self) -> None:
        pass

    async def start(self, target: str) -> dict[str, Any]:
        if self.store.load().get("mfaDone"):
            return {"needed": False}
        return {"needed": True, "reason": "你即将访问深圳大学内部网，非常用地点登录需要二次认证",
                "methods": [{"type": "4", "name": "企业微信验证码"}, {"type": "3", "name": "短信验证码"}, {"type": "2", "name": "账号密码"}]}

    async def send_code(self, method: str) -> str:
        return "验证码已发送至 138****0000（演示模式：填 000000）"

    async def submit(self, method: str, secret: str) -> None:
        if method != "2" and secret != "000000":
            raise CampusError("MFA_FAILED", "验证码错误")
        self.store.save(self.store.cookies(), mfaDone=True)


class MockCampus:
    def __init__(self, store: SessionStore, contact: ContactStore | None = None) -> None:
        self.store = store
        self.contact = contact or ContactStore()

        # 演示用的“我的预借”记录（只在 MCP 进程内存里），渲染成和真实 myfb.aspx 同样的表格，走同一个解析器
        self._prebooks: list[dict[str, str]] = [
            {"fid": "", "ctrlno": "3351727", "title": "想念地坛", "pickup": "粤海校区南区致理楼C栋1层信息中心服务大厅取书柜",
             "date": "2026-11-14", "status": "读者取消"}]
        self.sports = json.loads(shift_dates(_read("sports.json"), (mock_today() - SPORTS_FIXTURE_TODAY).days))
        self.electricity = json.loads(_read("electricity.json"))
        # 和真实接口 normalize_booking 一样的结构。
        # 预约在 MCP 进程里提交、付款卡片在 Web 进程里查询，所以订单状态存在文件里供两个进程共享
        self._state_path = store.path.with_name("mock-sports.json")
        self._default_bookings: list[dict[str, Any]] = [{
            "orderNo": b["orderNo"], "timeRange": b["timeRange"], "bookedAt": b["bookedAt"], "campus": b["campus"],
            "project": b["project"], "venue": b["venue"], "field": b["field"], "status": b["status"], "payState": b["note"],
            "paid": True, "awaitingPayment": False, "cancelled": False, "cancellable": False, "amountCny": 5.0,
            "projectCode": b["project"],
        } for b in self.sports["bookings"]]

    async def whoami(self) -> str:
        if not self.store.info()["hasSession"]:
            raise CampusError("LOGIN_REQUIRED", "未登录")
        data = self.store.load()
        # 模拟服务器在“非常用地点”：读学号前学校要求二次验证（SZU_AGENT_MOCK_LOGIN_MFA=1 时）
        if os.environ.get("SZU_AGENT_MOCK_LOGIN_MFA") == "1" and not data.get("mfaDone"):
            raise CampusError("MFA_REQUIRED", "学校要求二次验证", target="https://ehall.szu.edu.cn/new/index.html")
        return data.get("mockStudentNo") or "2099000001"

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"bookings": self._default_bookings, "checks": {}}

    def _save(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)

    def _require_login(self) -> None:
        if not self.store.info()["hasSession"]:
            raise CampusError("LOGIN_REQUIRED", "登录凭证不存在或已过期")
        # SZU_MOCK_MFA=1 时模拟“已登录但要求二次验证”
        if os.environ.get("SZU_MOCK_MFA") == "1" and not self.store.load().get("mfaDone"):
            raise CampusError("MFA_REQUIRED", "学校要求二次验证", target="https://www1.szu.edu.cn/board/")

    async def auth_status(self) -> dict[str, Any]:
        info = self.store.info()
        return {"loggedIn": info["hasSession"], **info}

    # 公文通
    async def notice_search(self, keyword: str, *, search_in: str = "title", range: str = "6m",
                            limit: int = 10, publisher: str | None = None) -> dict[str, Any]:
        self._require_login()
        items = [x for x in parse_notice_list(_read("notice-list.html"))["items"]
                 if (not keyword or keyword in x["title"]) and (not publisher or publisher in x["publisher"])]
        return {"total": len(items), "stats": notice_stats(items), "items": items[:limit]}

    async def notice_view(self, notice_id: str) -> dict[str, Any]:
        self._require_login()
        # 不同 id 用不同样例正文：奖学金类通知有单独的样例
        name = f"notice-view-{notice_id}.html" if (FIXTURES / f"notice-view-{notice_id}.html").exists() else "notice-view.html"
        return parse_notice_detail(_read(name), f"https://www1.szu.edu.cn/board/view.asp?id={notice_id}")

    async def notice_attachment(self, url: str, notice_id: str | None = None) -> tuple[bytes, str]:
        from .service import check_attachment_url

        self._require_login()
        check_attachment_url(url)
        return f"演示模式的附件占位文件\n原始地址：{url}\n".encode("utf-8"), "text/plain; charset=utf-8"

    # 讲座
    async def lecture_list(self, availability: str = "available") -> dict[str, Any]:
        self._require_login()
        rows = json.loads(_read("lecture-list.json"))["data"]
        rooms = json.loads(_read("lecture-classrooms.json"))
        items = [normalize_lecture(r, rooms.get(str(r["id"]), {}).get("data") if r["status"] == "正在报名中" else None, datetime.now()) for r in rows]
        keep = {"available": {"available"}, "open": {"available", "full", "unknown"}}.get(availability)
        return {"items": [x for x in items if not keep or x["availabilityState"] in keep]}

    async def lecture_progress(self) -> dict[str, Any]:
        self._require_login()
        return lecture_progress({"offlineTimes": "3", "onlineTimes": "4", "sumOfflineTimes": 6, "sumOnlineTimes": 8})

    # 图书馆
    async def library_search(self, keyword: str, limit: int = 5) -> dict[str, Any]:
        self._require_login()
        data = parse_library_search(_read("library-search.html"))
        data["items"] = data["items"][:limit]
        return data

    async def library_book(self, ctrlno: str) -> dict[str, Any]:
        self._require_login()
        data = parse_library_book(_read("library-book.html"), _read("library-book-holdings.html"))
        return {**data, "ctrlno": ctrlno}

    async def library_prebook(self, ctrlno: str, pickup: str | None, *, confirm: bool) -> dict[str, Any]:
        from .library import prebook

        self._require_login()
        return await prebook(self, self.contact, ctrlno, pickup, confirm=confirm)

    async def prebook_form(self, ctrlno: str) -> tuple[str, str]:
        return _read("library-addfb.html"), f"https://www.lib.szu.edu.cn/opac/admin_findbook/addfb.aspx?ctrlno={ctrlno}"

    async def library_my_prebooks(self, page: int = 1) -> dict[str, Any]:
        from .library import my_prebooks

        self._require_login()
        return await my_prebooks(self, page)

    async def library_prebook_cancel(self, fid: str, *, confirm: bool) -> dict[str, Any]:
        from .library import cancel_prebook

        self._require_login()
        return await cancel_prebook(self, fid, confirm=confirm)

    async def prebook_check(self, ctrlno: str, volume: str) -> str:
        active = any(r["ctrlno"] == ctrlno and r["status"] == "未处理" for r in self._prebooks)
        return "1" if active else "0"

    async def prebook_submit(self, page_url: str, html: str, values: dict[str, str]) -> str:
        ctrlno = page_url.rsplit("=", 1)[-1]
        pickup = next((p["name"] for p in parse_prebook_form(html)["pickups"] if p["value"] == values["ddlfl"]), "")
        fid = str(175100 + len(self._prebooks))
        title = re.split(r"[／/]", parse_prebook_form(html)["bookInfo"] or "")[0]
        self._prebooks.insert(0, {"fid": fid, "ctrlno": ctrlno, "title": title, "pickup": pickup,
                                  "date": date.today().isoformat(), "status": "未处理"})
        return "<script>alert('预借请求提交成功');</script>"  # 真实页面的成功提示（2026-11-14 实测）

    async def prebook_list_page(self, page: int) -> str:
        rows = "".join(
            f'<tr><td>{i}</td><td><span class="title"><a href="https://www.lib.szu.edu.cn/opac/bookinfo.aspx?ctrlno={r["ctrlno"]}">'
            f'{r["title"]}</a></span></td><td></td><td>{r["pickup"]}</td><td>{r["date"]}</td><td>{r["status"]}</td>'
            f'<td>{f"""<a href="javascript:deletefb({r["fid"]},0)">[取消]</a>""" if r["status"] == "未处理" else ""}</td></tr>'
            for i, r in enumerate(self._prebooks, 1))
        return (f'<span id="x_dplbl2">1</span>/<span id="x_gplbl2">1</span><table class="tb"><thead class="tbhead"><tr><td>序号</td>'
                f'<td>题名</td><td>卷期</td><td>预借取书地点</td><td>提交时间</td><td>状态</td><td></td></tr></thead>'
                f'<tbody>{rows}</tbody></table>')

    async def prebook_delete(self, fid: str) -> str:
        for r in self._prebooks:
            if r["fid"] == fid and r["status"] == "未处理":
                r["status"] = "读者取消"
                return "1"
        return "0"

    # 课表
    # 网络面试间：FakeBoothApi 模拟事务中心接口的返回，业务逻辑和真实模式共用 booth.py
    async def booth_list(self, campus: str | None = None, kind: str | None = None) -> dict[str, Any]:
        from .booth import list_booths

        self._require_login()
        return await list_booths(self._booth_api, campus, kind)

    async def booth_reserve(self, booth_id: int, day: date, start: str | None, end: str | None, grade: str | None, *,
                            confirm: bool, slots: list[int] | None = None) -> dict[str, Any]:
        from .booth import reserve

        self._require_login()
        return await reserve(self._booth_api, self.contact, booth_id, day, start, end, grade, confirm=confirm, slots=slots)

    async def booth_cancel(self, reservation_id: int, *, confirm: bool) -> dict[str, Any]:
        from .booth import cancel

        self._require_login()
        return await cancel(self._booth_api, reservation_id, confirm=confirm)

    async def booth_slots(self, booth_id: int, day: date | None = None) -> dict[str, Any]:
        from .booth import booth_slots

        self._require_login()
        return await booth_slots(self._booth_api, booth_id, day)

    async def booth_my_reservations(self, limit: int = 10) -> dict[str, Any]:
        from .booth import my_reservations

        self._require_login()
        return await my_reservations(self._booth_api, limit)

    @property
    def _booth_api(self) -> "FakeBoothApi":
        if not hasattr(self, "_fake_booth"):
            self._fake_booth = FakeBoothApi()
        return self._fake_booth

    async def course_today(self, day: date | None = None) -> dict[str, Any]:
        self._require_login()
        return courses_on(build_courses(json.loads(_read("course-api.json"))), day or date.today())

    async def course_schedule(self, term: str | None = None) -> dict[str, Any]:
        """演示：整学期课表，和 course_today 用同一份样例。"""
        self._require_login()
        data = build_courses(json.loads(_read("course-api.json")))
        name = data["term"]["name"]
        if term and term_key(term) not in term_key(name) and term_key(term) not in gs_term_code(data["term"]["id"] or ""):
            return {"term": None, "items": [], "availableTerms": [name], "hint": f"没有和“{term}”对应的学期，可选的学期见 availableTerms"}
        return {"term": name, "items": sorted(data["items"], key=lambda x: (x["weekday"] or 0, x["startSection"] or 0)),
                "unscheduled": ["科研创新实践"], "availableTerms": [name],
                "url": "https://ehall.szu.edu.cn/gsapp/sys/wdkbapp/*default/index.do"}

    async def grades(self, term: str | None = None) -> dict[str, Any]:
        """演示：研一两个学期的成绩（假数据，字段和真实接口一致）。"""
        self._require_login()
        rows, _ = parse_grade_page(json.loads(_read("grades.json")))
        return {**build_grade_report(rows, term), "url": "https://ehall.szu.edu.cn/gsapp/sys/szdxwdcjapp/*default/index.do"}

    # 体育预约
    async def sports_campuses(self) -> dict[str, Any]:
        return {"items": [{"name": c} for c in self.sports["campuses"]]}

    async def sports_projects(self, campus: str) -> dict[str, Any]:
        self._require_login()
        return {"campus": campus, "items": [{"name": v["name"], "modes": v.get("modes", ["散场"])}
                                             for v in self.sports["venues"] if v["campus"] == campus]}

    def _check_project(self, campus: str, project: str, mode: str | None = None) -> str:
        v = next((v for v in self.sports["venues"] if v["campus"] == campus and v["name"] == project), None)
        if v is None:
            raise CampusError("SPORTS_PROJECT_NOT_FOUND", f"{campus} 没有项目“{project}”",
                              available=[x["name"] for x in self.sports["venues"] if x["campus"] == campus])
        modes = v.get("modes", ["散场"])
        if mode and mode not in modes:
            raise CampusError("SPORTS_MODE_UNSUPPORTED", f"{project}不支持{mode}，可选：{'、'.join(modes)}")
        return mode or ("散场" if "散场" in modes else modes[0])

    async def sports_slots(self, campus: str, project: str, day: str | None = None, mode: str | None = None) -> dict[str, Any]:
        self._require_login()
        mode = self._check_project(campus, project, mode)
        day = day or self.sports["dates"][0]
        items = []
        for s in self.sports["slotsByDate"].get(day, []):
            label, _, state = s["label"].partition("(")
            state = state.rstrip(")")
            items.append({"label": label, "state": state, "selectable": state == "可预约", "remaining": s.get("remaining", 0)})
        return {"campus": campus, "project": project, "mode": mode, "date": day, "openDates": self.sports["dates"], "items": items}

    async def sports_fields(self, campus: str, project: str, day: str, slot: str, mode: str | None = None) -> dict[str, Any]:
        self._require_login()
        mode = self._check_project(campus, project, mode)
        return {"campus": campus, "project": project, "mode": mode, "date": day, "slot": slot,
                "items": [{**f, "available": f["remaining"] > 0} for f in self.sports["fields"]]}

    async def sports_reserve(self, campus: str, project: str, day: str, slot: str, field: str | None,
                             *, confirm: bool, mode: str | None = None) -> dict[str, Any]:
        slots = await self.sports_slots(campus, project, day, mode)
        target = next((s for s in slots["items"] if s["label"] == slot), None)
        if not target or not target["selectable"]:
            raise CampusError("SPORTS_SLOT_UNAVAILABLE", f"{day} {slot} 不可预约")
        fields = [f["name"] for f in self.sports["fields"] if f["remaining"] > 0]
        if field is None and len(fields) > 1:
            raise CampusError("SPORTS_FIELD_SELECTION_REQUIRED", "有多个场地可选", fields=fields)
        chosen = field or fields[0]
        if chosen not in fields:
            raise CampusError("NOT_FOUND", f"没有可用场地“{chosen}”", fields=fields)
        preview = {"campus": campus, "venue": project, "mode": slots["mode"], "field": chosen, "date": day,
                   "selected": {"label": slot, "state": target["state"], "remaining": target["remaining"]}, "submitted": False}
        if not confirm:
            return preview
        order_no = f"MOCK{int(time.time() * 1000)}"
        start, end = slot.split("-")
        state = self._load()
        state["bookings"].insert(0, {
            "orderNo": order_no, "timeRange": f"{day} {start}~{day} {end}", "bookedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "campus": campus, "project": project, "venue": "南区体育馆", "field": chosen, "status": "已预约",
            "payState": "未支付", "paid": False, "awaitingPayment": True, "cancelled": False, "cancellable": True,
            "amountCny": 0.0, "projectCode": project,
        })
        self._save(state)
        return {**preview, "submitted": True, "orderNo": order_no,
                "payment": {"required": True, "message": "预约已提交，请到“我的预约”完成支付"}}

    async def sports_bookings(self, limit: int = 5) -> dict[str, Any]:
        self._require_login()
        state = self._load()
        items = state["bookings"]
        return {"total": len(items), "items": items[:limit], "url": "https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy/index.do#/myBooking"}

    async def sports_balance(self) -> dict[str, Any]:
        self._require_login()
        return {"sportsFundCny": 20.0}

    async def sports_payment_info(self, order_no: str) -> dict[str, Any]:
        """演示：同一订单被查询第 3 次时视为用户已在学校页面付款。"""
        self._require_login()
        state = self._load()
        booking = next((b for b in state["bookings"] if b["orderNo"] == order_no), None)
        if booking is None:
            raise CampusError("NOT_FOUND", f"最近的预约里没有订单 {order_no}")
        n = state["checks"][order_no] = state["checks"].get(order_no, 0) + 1
        if n >= 3 and booking["awaitingPayment"]:
            booking.update(paid=True, awaitingPayment=False, payState="已支付", amountCny=5.0)
        self._save(state)
        from .service import pay_deadline

        return {"booking": booking, "sportsFundCny": 20.0, "referencePriceCny": 5.0, "payDeadline": pay_deadline(booking.get("bookedAt")),
                "payUrl": "https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy/index.do#/myBooking"}

    async def sports_cancel(self, order_no: str, *, confirm: bool) -> dict[str, Any]:
        self._require_login()
        state = self._load()
        b = next((x for x in state["bookings"] if x["orderNo"] == order_no), None)
        if b is None:
            raise CampusError("NOT_FOUND", f"最近的预约里没有订单 {order_no}")
        if b["cancelled"]:
            raise CampusError("SPORTS_ALREADY_CANCELLED", f"订单 {order_no} 已经是取消状态")
        if not b["cancellable"]:
            raise CampusError("SPORTS_NOT_CANCELLABLE", f"这条预约已经不能取消（{b['status']} / {b['payState']}）")
        preview = {k: b[k] for k in ("orderNo", "timeRange", "campus", "project", "venue", "field", "status", "payState", "paid")}
        if not confirm:
            return {**preview, "cancelled": False}
        b.update(cancelled=True, awaitingPayment=False, cancellable=False, status="取消预约", payState="已取消")
        self._save(state)
        return {**preview, "cancelled": True, "verified": True, "statusAfter": "已取消"}

    # 电费（真实系统在内网、不需要登录）。楼栋用和真实模式同一份配置
    async def electricity_buildings(self) -> dict[str, Any]:
        from .electricity_config import CAMPUS_MAP

        return {"campuses": [{"name": c, "buildings": list(cfg["buildings"])} for c, cfg in CAMPUS_MAP.items()]}

    def _check_building(self, building: str) -> None:
        from .electricity_config import CAMPUS_MAP

        if not any(building in b for cfg in CAMPUS_MAP.values() for b in cfg["buildings"]):
            raise CampusError("ELECTRICITY_ROOM_NOT_FOUND", f"没有楼栋“{building}”")

    async def electricity_query(self, building: str, room: str, campus: str | None = None,
                                from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        self._check_building(building)
        records = self.electricity["query"]["records"]
        latest = records[-1]
        return {"campus": campus or "新斋区", "building": building, "room": room,
                "remainingKwh": latest["remainingKwh"], "latestRecord": latest, "records": records}

    async def electricity_purchases(self, building: str, room: str, campus: str | None = None,
                                    from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        self._check_building(building)
        records = [
            {"no": 1, "room": room, "purchaseType": "微信购电", "kwh": 142.86, "amountCny": 100.0, "purchasedAt": "2026-10-08 12:35:20"},
            {"no": 2, "room": room, "purchaseType": "微信购电", "kwh": 71.43, "amountCny": 50.0, "purchasedAt": "2026-11-01 21:02:11"},
        ]
        return {"campus": campus or "新斋区", "building": building, "room": room, "records": records,
                "totalKwh": 214.29, "totalCny": 150.0}


class FakeBoothApi:
    """演示用的事务中心场地接口：返回结构照抄前端脚本里读到的字段。“我的预约”故意用 encoding=1 的加密格式返回，
    这样演示模式也会走一遍解码。"""

    BOOTHS = [
        {"id": 1, "name": "时光社区网络面试间1", "campus": "粤海", "community": "时光", "typeId": 1, "status": True,
         "description": "场地含桌子1张、椅子1个，可供1人进行网络面试。", "type": {"name": "网络面试间"}},
        {"id": 2, "name": "时光社区网络面试间2", "campus": "粤海", "community": "时光", "typeId": 1, "status": True,
         "description": "场地含桌子1张、椅子1个，可供1人进行网络面试。", "type": {"name": "网络面试间"}},
        {"id": 3, "name": "青冈栎社区网络面试间1", "campus": "丽湖", "community": "青冈栎", "typeId": 1, "status": False,
         "description": "场地含桌子1张、椅子1个，可供1人进行网络面试。", "type": {"name": "网络面试间"}},
        {"id": 11, "name": "时光社区共享琴房1", "campus": "粤海", "community": "时光", "typeId": 2, "status": True,
         "description": "配有立式钢琴一台。", "type": {"name": "共享琴房"}},
        {"id": 21, "name": "时光社区共享会议室", "campus": "粤海", "community": "时光", "typeId": 3, "status": True,
         "description": "可容纳 8 人的小型会议室。", "type": {"name": "共享会议室"}},
    ]
    TYPES = {2: {"id": 2, "name": "共享琴房", "samePersonMaxReservationPerDay": 4, "lastReservationDayBeforeAppointment": 3,
                 "availableTimePeriod": str(sum(1 << i for i in range(16, 44))), "timeOffsetMinutes": -10},
             3: {"id": 3, "name": "共享会议室", "samePersonMaxReservationPerDay": 4, "lastReservationDayBeforeAppointment": 3,
                 "availableTimePeriod": str(sum(1 << i for i in range(16, 44))), "timeOffsetMinutes": -10}}

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []  # 演示时新增的预约（只在内存里）
        self.cancelled: set[int] = set()
    # 08:00-22:00 开放（第 16-43 格）
    TYPE = {"id": 1, "name": "网络面试间", "availableTimePeriod": str(sum(1 << i for i in range(16, 44))),
            "samePersonMaxReservationPerDay": 4, "lastReservationDayBeforeAppointment": 3, "timeOffsetMinutes": -10}

    async def api(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None) -> Any:
        from datetime import timedelta

        from .booth import _XOR_KEY, unwrap, xor_decode

        params = params or {}
        if path == "/booth/list":
            return {"list": self.BOOTHS, "total": len(self.BOOTHS)}
        if m := re.fullmatch(r"/booth/info/(\d+)", path):
            booth = next((b for b in self.BOOTHS if b["id"] == int(m.group(1))), None)
            return booth
        if path.startswith("/boothType/info/"):
            return self.TYPES.get(int(path.rsplit("/", 1)[1]), self.TYPE)
        if path == "/boothReservation/eligibility":
            from .booth import beijing_now

            today = beijing_now().date()
            booth = next((b for b in self.BOOTHS if b["id"] == int(params.get("boothId", 0))), {})
            if booth.get("typeId") != 1:  # 非面试类型：不审材料，接口不给日期范围
                return {"eligible": True, "materialReviewRequired": False, "reasons": []}
            return {"eligible": True, "materialReviewRequired": True, "minDate": today.isoformat(),
                    "maxDate": (today + timedelta(days=2)).isoformat(), "minAdvanceMinutes": 120,
                    "currentMonthViolationCount": 0, "materialBans": [], "reasons": []}
        if m := re.fullmatch(r"/booth/(\d+)/available-time", path):
            day = date.fromisoformat(params["startDate"])
            times = [-1] * 48
            for i in range(16, 44):
                times[i] = 1
            seed = int(m.group(1)) + day.toordinal()
            for i in range(16, 44):  # 每天有几段已被约走，看起来像真的
                if (i * 7 + seed) % 5 == 0:
                    times[i] = 0
            for r in self.added:
                if r["boothId"] == int(m.group(1)) and r["date"] == day.isoformat() and r["id"] not in self.cancelled:
                    for i in r["timeList"]:
                        times[i] = 0
            return [{"date": day.isoformat(), "times": times, "timeOffsetMinutes": -10}]
        if path == "/boothReservation/add":
            body = json_body or {}
            booth = next(b for b in self.BOOTHS if b["id"] == body["boothId"])
            self.added.append({"id": 9100 + len(self.added), "boothId": booth["id"], "boothName": booth["name"],
                               "date": body["date"], "timeList": body["timeList"], "timeOffsetMinutes": -10,
                               "status": "RESERVED", "phone": body["phone"]})
            return None
        if m := re.fullmatch(r"/boothReservation/cancel/(\d+)", path):
            self.cancelled.add(int(m.group(1)))
            return None
        if path == "/boothReservation/my":
            from .booth import beijing_now

            day = beijing_now().date().isoformat()
            rows = [{"id": 9001, "boothId": 1, "boothName": "时光社区网络面试间1", "date": day,
                     "timeList": [30, 31], "timeOffsetMinutes": -10, "status": "RESERVED",
                     "phone": "13800000000", "name": "演示用户"}]
            rows += [{**r, "status": "CANCELLED" if r["id"] in self.cancelled else r["status"]} for r in reversed(self.added)]
            plain = json.dumps({"list": rows, "total": len(rows)}, ensure_ascii=False)
            return unwrap({"data": xor_decode(plain, _XOR_KEY), "encoding": 1})  # 异或两次 = 原文
        raise CampusError("NOT_FOUND", f"演示接口没有 {path}")
