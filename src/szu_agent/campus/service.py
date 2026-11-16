"""真实校园服务：直接用 HTTP 访问学校系统。

每个功能的可信度不同，代码里用 VERIFIED / UNVERIFIED 标注：
  - 接口地址和参数来自可靠参考（szu-cli 源码、szu_gym_helper）且解析器有样例测试的，标 VERIFIED-BY-REFERENCE
  - 参数靠推测、还没在真实账号上跑过的，标 UNVERIFIED，失败时抛 PAGE_CHANGED 并提示需要抓包
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .electricity_config import CAMPUS_MAP, SIMS_HOST
from .contact import ContactStore
from .errors import CampusError
from .http import CampusHttp, decode, is_login_page, select_options
from .parsers import (
    BOARD_URL, OPAC_URL, build_courses, notice_stats, courses_on, lecture_progress, normalize_lecture,
    MODE_CODES, MODE_NAMES, normalize_booking, normalize_field, normalize_slot, parse_sports_catalog, parse_library_book, parse_library_search, parse_sims_page, parse_sims_room_id, reference_price, parse_notice_detail, parse_notice_list,
)

EHALL = "https://ehall.szu.edu.cn"
LECTURE = "https://lecture.szu.edu.cn"
SPORTS_APP = f"{EHALL}/qljfwapp/sys/lwSzuCgyy"
SPORTS_PAGE = f"{SPORTS_APP}/index.do#/sportVenue"
MY_BOOKING_PAGE = f"{SPORTS_APP}/index.do#/myBooking"
MY_BOOKING_LIST = f"{SPORTS_APP}/modules/myBooking/myBookingInfo.do"
SELF_MONEY = f"{SPORTS_APP}/modules/myBooking/getSelfMoney.do"
CANCEL_BOOKING = f"{SPORTS_APP}/sportVenue/cancelSelectBookingInfo.do"
PAY_DEADLINE_MINUTES = 45  # 学校脚本注释：个人包场、个人散场预约后 45 分钟内未支付，系统将自动取消
COURSE_APP = f"{EHALL}/jwapp/sys/wdkb"

ATTACHMENT_MAX_BYTES = 30 * 1024 * 1024


def check_attachment_url(url: str) -> None:
    """只允许下载公文通的附件：下载接口由本机后端带着登录凭证代为请求，
    如果不限制地址，就可能被利用去访问任意内网地址（SSRF）。"""
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.hostname != "www1.szu.edu.cn" or not u.path.startswith("/board/"):
        raise CampusError("PERMISSION_DENIED", "只能下载公文通（www1.szu.edu.cn/board）里的附件")


def pay_deadline(booked_at: str | None) -> str | None:
    """下单时间 + 45 分钟。"""
    try:
        return (datetime.strptime(booked_at or "", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=PAY_DEADLINE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def gbk_url(url: str) -> str:
    """把查询参数按 GBK 重新编码（已经编码过的会先解码），路径部分不动。"""
    from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

    u = urlsplit(url)
    if not u.query:
        return url
    query = "&".join(f"{quote(k, encoding='gbk')}={quote(v, encoding='gbk', safe='')}"
                     for k, v in parse_qsl(u.query, keep_blank_values=True, encoding="gbk"))
    return urlunsplit((u.scheme, u.netloc, u.path, query, u.fragment))


RANGE_LABELS = {"24h": "24小时", "7d": "1周", "30d": "1个月", "6m": "6个月"}
TYPE_LABELS = {"title": "标题", "body": "正文", "full": "全文"}

def _pick_option(options: list[tuple[str, str]], label: str) -> str | None:
    for value, text in options:
        if label == text or label == value or label in text:
            return value
    return None


class RealCampus:
    def __init__(self, http: CampusHttp, contact: ContactStore | None = None) -> None:
        self.http = http
        self.contact = contact or ContactStore()
        self._user_cache: tuple[str, str] | None = None

    # ------------------------------------------------------------ 登录状态

    async def auth_status(self) -> dict[str, Any]:
        resp = await self.http.request(
            "GET", "https://authserver.szu.edu.cn/authserver/login",
            params={"service": f"{EHALL}/login?service={EHALL}/new/index.html"}, require_login=False,
        )
        logged_in = not is_login_page(str(resp.url))
        return {"loggedIn": logged_in, **self.http.store.info()}

    # ------------------------------------------------------------ 公文通（VERIFIED-BY-REFERENCE）

    async def notice_search(self, keyword: str, *, search_in: str = "title", range: str = "6m",
                            limit: int = 10, publisher: str | None = None) -> dict[str, Any]:
        values = {"keyword": keyword}
        for attempt in (0, 1):  # 注意：参数名 range 遮住了内置 range()
            html, resp = await self.http.get_html(BOARD_URL, cache=attempt == 0)
            # 用页面上真实的选项值，而不是写死 "183#6个月内" 这种内部编码
            if v := _pick_option(select_options(html, "dayy"), RANGE_LABELS.get(range, range)):
                values["dayy"] = v
            if v := _pick_option(select_options(html, "search_type"), TYPE_LABELS.get(search_in, search_in)):
                values["search_type"] = v
            if publisher:
                if not (v := _pick_option(select_options(html, "from_username"), publisher)):
                    raise CampusError("NOT_FOUND", f"公文通没有发文单位“{publisher}”")
                values["from_username"] = v
            result_html, result = await self.http.submit_form(
                str(resp.url), html, contains="keyword", values=values, submit="searchb1", cache=True)
            # 会话过期时 POST 会被单点登录重定向，最后落回公文通首页而不是结果页：重新取一次表单再搜
            if "infolist.asp" in str(result.url):
                break
            self.http.clear_cache()
        else:
            raise CampusError("PAGE_CHANGED", "公文通搜索没有返回结果页")
        data = parse_notice_list(result_html)
        items = data["items"]
        if keyword and search_in == "title":
            items = [i for i in items if keyword in i["title"]]  # 保险：标题搜索时结果必须真的含关键词
        return {"total": data["total"], "stats": notice_stats(items), "items": items[:limit]}

    async def notice_view(self, notice_id: str) -> dict[str, Any]:
        if not notice_id.isdigit():
            raise CampusError("NOT_FOUND", "公告 id 应该是数字")
        url = f"{BOARD_URL}view.asp?id={notice_id}"
        html, _ = await self.http.get_html(url, cache=True)
        return parse_notice_detail(html, url)

    async def notice_attachment(self, url: str, notice_id: str | None = None) -> tuple[bytes, str]:
        """下载公文通附件。返回 (内容, Content-Type)。

        2026-11 实测，学校的 down1oad.asp 有两个要求，缺一不可：
          1. 链接里的中文文件名必须按 GBK 编码（浏览器在 GB2312 页面上点链接就是这么编的）；
             按 UTF-8 编码时服务器直接断开连接
          2. 要带详情页的 Referer，否则返回“请通过公文通电脑版或手机企微版下载附件”
        """
        check_attachment_url(url)
        headers = {"Referer": f"{BOARD_URL}view.asp?id={notice_id}" if notice_id and notice_id.isdigit() else BOARD_URL}
        resp = await self.http.request("GET", gbk_url(url), headers=headers)
        if len(resp.content) > ATTACHMENT_MAX_BYTES:
            raise CampusError("NOT_FOUND", "附件太大（超过 30MB），请在公文通网页上下载")
        ctype = resp.headers.get("content-type", "application/octet-stream")
        if ctype.startswith("text/html"):
            alert = re.search(r'alert\("(.*?)"\)', decode(resp, default="gb18030"))
            raise CampusError("NOT_FOUND", alert.group(1) if alert else "学校没有返回附件文件（链接可能已失效）")
        return resp.content, ctype

    # ------------------------------------------------------------ 讲座（VERIFIED-BY-REFERENCE，需校园网）

    async def _lecture_json(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.http.request(method, f"{LECTURE}{path}", **kw)
        try:
            return resp.json()
        except ValueError as exc:
            raise CampusError("PAGE_CHANGED", f"讲座平台 {path} 没有返回 JSON") from exc

    async def lecture_list(self, availability: str = "available") -> dict[str, Any]:
        await self.http.request("GET", f"{LECTURE}/", cache=True)  # 先进首页，走一遍单点登录
        body = await self._lecture_json("GET", "/tLectureSignUp/list", params={"page": 1, "limit": 100}, cache=True)
        rows = body.get("data") or []
        now = datetime.now()
        items = []
        for row in rows:
            rooms = None
            if row.get("status") == "正在报名中":
                rb = await self._lecture_json("GET", "/lectureClassroomSignUp/list",
                                              params={"lectureId": row.get("id"), "page": 1, "limit": 100}, cache=True)
                rooms = rb.get("data") if isinstance(rb.get("data"), list) else None
            items.append(normalize_lecture(row, rooms, now))
        keep = {"available": {"available"}, "open": {"available", "full", "unknown"}}.get(availability)
        if keep:
            items = [x for x in items if x["availabilityState"] in keep]
        return {"items": items}

    async def lecture_progress(self) -> dict[str, Any]:
        await self.http.request("GET", f"{LECTURE}/", cache=True)
        body = await self._lecture_json("POST", "/sysUser/getUserInfo")
        return lecture_progress(body.get("data") if isinstance(body.get("data"), dict) else body)

    # ------------------------------------------------------------ 图书馆（VERIFIED-BY-REFERENCE）

    async def library_search(self, keyword: str, limit: int = 5) -> dict[str, Any]:
        params = {"dt": "ALL", "cl": "ALL", "dp": "20", "sf": "M_PUB_YEAR", "ob": "DESC", "sm": "table",
                  "dept": "ALL", "anywords": keyword, "ecx": "1", "efz": "1"}
        html, _ = await self.http.get_html(f"{OPAC_URL}searchresult.aspx", params=params, cache=True)
        data = parse_library_search(html)
        data["items"] = data["items"][:limit]
        return data

    async def library_book(self, ctrlno: str) -> dict[str, Any]:
        """书目详情 + 每一册的位置和状态。来源：用户保存的真实页面（需要登录）。
        馆藏表不在 bookinfo.aspx 的服务器返回里，而是页面 JS 登录后从 bookinfowithresvAjax.aspx 取来填进去的
        （用户自检时发现：页面能解析出书名，但 0 册）。"""
        if not re.fullmatch(r"\d{1,12}", ctrlno or ""):
            raise CampusError("NOT_FOUND", "书目编号（ctrlno）应该是数字，来自 library_search 结果里的 id")
        html, _ = await self.http.get_html(f"{OPAC_URL}bookinfo.aspx", params={"ctrlno": ctrlno}, cache=True)
        holdings, _ = await self.http.get_html(f"{OPAC_URL}bookinfowithresvAjax.aspx", params={"ctrlno": ctrlno}, cache=True,
                                               headers={"Referer": f"{OPAC_URL}bookinfo.aspx?ctrlno={ctrlno}"})
        data = parse_library_book(html, holdings)
        if not data["copies"] and not data["title"]:
            raise CampusError("NOT_FOUND", f"没有找到书目 {ctrlno}")
        return data

    async def library_prebook(self, ctrlno: str, pickup: str | None, *, confirm: bool) -> dict[str, Any]:
        """预借登记。confirm=False 只预览（不提交）。流程见 library.py。"""
        from .library import prebook

        return await prebook(self, self.contact, ctrlno, pickup, confirm=confirm)

    async def library_my_prebooks(self, page: int = 1) -> dict[str, Any]:
        """我的预借（myfb.aspx，用户保存的真实页面）。"""
        from .library import my_prebooks

        return await my_prebooks(self, page)

    async def library_prebook_cancel(self, fid: str, *, confirm: bool) -> dict[str, Any]:
        """取消预借。confirm=False 只核对记录存在且可取消。接口来自用户抓包 + myfb.aspx 的 deletefb()。"""
        from .library import cancel_prebook

        return await cancel_prebook(self, fid, confirm=confirm)

    # 以下是 library.PrebookBackend：只负责收发，不含业务判断
    async def prebook_form(self, ctrlno: str) -> tuple[str, str]:
        html, resp = await self.http.get_html(f"{OPAC_URL}admin_findbook/addfb.aspx", params={"ctrlno": ctrlno})
        if urlparse(str(resp.url)).path.lower().endswith("/login.aspx"):
            # 统一认证有效时，图书馆的 login.aspx 会经 extcas.aspx 自动登录再跳回来；停在这里说明这条链路没走通
            raise CampusError("PAGE_CHANGED", "图书馆登录跳转没有完成")
        return html, str(resp.url)

    async def prebook_check(self, ctrlno: str, volume: str) -> str:
        resp = await self.http.request("GET", f"{OPAC_URL}admin_findbook/addfbcheckAjax.aspx",
                                       params={"ctrlno": ctrlno, "volume": volume},
                                       headers={"Referer": f"{OPAC_URL}admin_findbook/addfb.aspx?ctrlno={ctrlno}"})
        return decode(resp)

    async def prebook_submit(self, page_url: str, html: str, values: dict[str, str]) -> str:
        # Button1 是“提交”按钮：ASP.NET 靠被点击按钮的 name 判断触发哪个事件，漏了它表单只会重新加载
        text, _ = await self.http.submit_form(page_url, html, contains="ddlfl", values=values, submit="Button1")
        return text

    async def prebook_list_page(self, page: int) -> str:
        html, _ = await self.http.get_html(f"{OPAC_URL}user/myfb.aspx", params={"page": str(page)} if page > 1 else None)
        return html

    async def prebook_delete(self, fid: str) -> str:
        # 与页面一致：prototype.js 发的 XHR GET，带 _= 空参数，Referer 是“我的预借”页
        resp = await self.http.request("GET", f"{OPAC_URL}user/deletefbAjax.aspx", params={"fid": fid, "_": ""},
                                       headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{OPAC_URL}user/myfb.aspx"})
        return decode(resp)

    # ------------------------------------------------------------ 网络面试间（静音舱）。流程和接口来源见 booth.py

    async def booth_list(self, campus: str | None = None, kind: str | None = None) -> dict[str, Any]:
        from .booth import list_booths

        return await list_booths(self._swzx, campus, kind)

    async def booth_reserve(self, booth_id: int, day: date, start: str | None, end: str | None, grade: str | None, *,
                            confirm: bool, slots: list[int] | None = None) -> dict[str, Any]:
        from .booth import reserve

        return await reserve(self._swzx, self.contact, booth_id, day, start, end, grade, confirm=confirm, slots=slots)

    async def booth_cancel(self, reservation_id: int, *, confirm: bool) -> dict[str, Any]:
        from .booth import cancel

        return await cancel(self._swzx, reservation_id, confirm=confirm)

    async def booth_slots(self, booth_id: int, day: date | None = None) -> dict[str, Any]:
        from .booth import booth_slots

        return await booth_slots(self._swzx, booth_id, day)

    async def booth_my_reservations(self, limit: int = 10) -> dict[str, Any]:
        from .booth import my_reservations

        return await my_reservations(self._swzx, limit)

    @property
    def _swzx(self) -> "SwzxApi":
        if not hasattr(self, "_swzx_api"):
            self._swzx_api = SwzxApi(self.http)
        return self._swzx_api

    # ------------------------------------------------------------ 课表（UNVERIFIED：接口参数为推测）

    async def course_today(self, day: date | None = None) -> dict[str, Any]:
        day = day or date.today()
        await self.http.request("GET", f"{COURSE_APP}/*default/index.do", cache=True)
        api: dict[str, Any] = {}
        try:
            api["dqxnxq"] = (await self.http.request("POST", f"{COURSE_APP}/modules/jshkcb/dqxnxq.do", cache=True)).json()
            term = (api["dqxnxq"]["datas"]["dqxnxq"]["rows"] or [{}])[0]
            api["dqzc"] = (await self.http.request("POST", f"{COURSE_APP}/modules/jshkcb/dqzc.do", cache=True, data={
                "XN": term.get("XNDM", ""), "XQ": term.get("XQDM", ""), "RQ": day.isoformat()})).json()
            api["xskcb"] = (await self.http.request("POST", f"{COURSE_APP}/modules/xskcb/xskcb.do", cache=True,
                                                    data={"XNXQDM": term.get("DM", "")})).json()
        except (ValueError, KeyError, TypeError) as exc:
            raise CampusError("PAGE_CHANGED", "课表接口和预期不一致（该接口尚未用真实账号验证，需要抓包确认）") from exc
        return courses_on(build_courses(api), day)

    # ------------------------------------------------------------ 体育预约（全部项目，接口和参数来自学校公开前端脚本）
    #   getSportVenueData.do  校区 / 项目目录          getRqList.do     可约日期
    #   getTimeList.do        某项目某天的时段          getOpeningRoom.do 某时段的具体场地
    #   insertVenueBookingInfo.do  提交（场地的 WID/CGBM/CQBM 原样带回）
    # 预约后 45 分钟内未支付会被自动取消（学校脚本注释）。

    async def whoami(self) -> str:
        """当前登录的学号，只用于多人模式下算 user_id（users.user_id_for），算完就丢，不保存、不返回给模型。
        复用体育预约读 USER_INFO 的办法（已用真实账号验证过）；姓名在这里直接丢掉。"""
        uid, _name = await self._sports_user()
        return uid

    async def _sports_user(self) -> tuple[str, str]:
        """预约接口要求提交预约人学号和姓名，从应用首页的 USER_INFO 里读。只在内存里用，不返回给 LLM。"""
        if self._user_cache:
            return self._user_cache
        html, _ = await self.http.get_html(f"{SPORTS_APP}/index.do")
        info: dict[str, Any] = {}
        if m := re.search(r"USER_INFO\s*=\s*(\{.*?\})\s*;", html, re.S):
            try:
                info = json.loads(m[1])
            except json.JSONDecodeError:
                pass
        values = info.get("info") or []
        uid = str(info.get("id") or (values[0] if values else "") or "")
        if not uid and (m := re.search(r"USERID\s*=\s*['\"]([^'\"]+)['\"]", html)):
            uid = m[1]
        name = str(info.get("name") or info.get("userName") or (values[1] if len(values) > 1 else "") or "")
        if not uid or not name:
            raise CampusError("PAGE_CHANGED", "体育预约页面没有返回当前登录人信息")
        self._user_cache = (uid, name)
        return self._user_cache

    async def _catalog(self) -> dict[str, Any]:
        await self._sports_user()  # 先进入应用（拿到 MOD_AUTH_CAS）
        resp = await self.http.request("POST", f"{SPORTS_APP}/sportVenue/getSportVenueData.do", cache=True,
                                       headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            body = resp.json()
        except ValueError as exc:
            raise CampusError("PAGE_CHANGED", "getSportVenueData.do 没有返回 JSON") from exc
        if str(body.get("code")) == "-1":
            raise CampusError("SPORTS_CLOSED", str(body.get("msg") or "体育场馆预约暂未开放"))
        return parse_sports_catalog(body)

    def _campus_code(self, catalog: dict[str, Any], campus: str) -> dict[str, str]:
        for c in catalog["campuses"]:
            if campus in (c["name"], c["code"]) or campus in c["name"] or c["name"] in campus:
                return c
        raise CampusError("SPORTS_CAMPUS_NOT_FOUND", f"没有校区“{campus}”", campuses=[c["name"] for c in catalog["campuses"]])

    async def _resolve(self, campus: str, project: str) -> tuple[dict[str, str], dict[str, Any]]:
        catalog = await self._catalog()
        c = self._campus_code(catalog, campus)
        here = [p for p in catalog["projects"] if c["code"] in p["campusCodes"]]
        exact = [p for p in here if p["name"] == project]
        hits = exact or [p for p in here if project in p["name"] or p["name"] in project]
        if len(hits) != 1:
            raise CampusError("SPORTS_PROJECT_NOT_FOUND",
                              f"{c['name']}{'没有' if not hits else '有多个匹配的'}项目“{project}”",
                              available=[p["name"] for p in (hits or here)])
        return c, hits[0]

    @staticmethod
    def _mode(p: dict[str, Any], mode: str | None) -> str:
        """预约方式代码。不指定时：只支持一种就用那种，两种都支持时优先散场（个人）。"""
        if mode:
            if mode not in p["modes"]:
                raise CampusError("SPORTS_MODE_UNSUPPORTED", f"{p['name']}不支持{mode}，可选：{'、'.join(p['modes'])}")
            return MODE_CODES[mode]
        if not p["modes"]:
            raise CampusError("SPORTS_MODE_UNSUPPORTED", f"{p['name']}当前没有开放的预约方式")
        return MODE_CODES["散场" if "散场" in p["modes"] else p["modes"][0]]

    async def sports_campuses(self) -> dict[str, Any]:
        return {"items": [{"name": c["name"]} for c in (await self._catalog())["campuses"]]}

    async def sports_projects(self, campus: str) -> dict[str, Any]:
        catalog = await self._catalog()
        c = self._campus_code(catalog, campus)
        items = [{"name": p["name"], "modes": p["modes"]} for p in catalog["projects"] if c["code"] in p["campusCodes"]]
        return {"campus": c["name"], "items": items}

    async def sports_dates(self) -> list[str]:
        await self._sports_user()
        resp = await self.http.request("POST", f"{SPORTS_APP}/sportVenue/getRqList.do", cache=True,
                                       headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            rows = resp.json()
        except ValueError:
            return []
        return [str(r if not isinstance(r, dict) else r.get("CODE") or r.get("RQ") or "") for r in rows if r] if isinstance(rows, list) else []

    async def sports_slots(self, campus: str, project: str, day: str | None = None, mode: str | None = None) -> dict[str, Any]:
        c, p = await self._resolve(campus, project)
        yylx = self._mode(p, mode)
        dates = await self.sports_dates()
        day = day or (dates[0] if dates else date.today().isoformat())
        resp = await self.http.request("POST", f"{SPORTS_APP}/sportVenue/getTimeList.do",
                                       data={"XQ": c["code"], "YYRQ": day, "YYLX": yylx, "XMDM": p["code"]},
                                       headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            rows = resp.json()
        except ValueError as exc:
            raise CampusError("PAGE_CHANGED", "时段接口没有返回 JSON") from exc
        return {"campus": c["name"], "project": p["name"], "mode": MODE_NAMES[yylx], "date": day,
                "openDates": dates, "items": [normalize_slot(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []}

    async def _fields(self, c: dict[str, str], p: dict[str, Any], yylx: str, day: str, slot: str) -> list[dict[str, Any]]:
        start, _, end = slot.partition("-")
        body = await self._sports_post_json(f"{SPORTS_APP}/modules/sportVenue/getOpeningRoom.do", {
            "XMDM": p["code"], "YYRQ": day, "YYLX": yylx, "KSSJ": start, "JSSJ": end, "XQDM": c["code"]})
        rows = ((body.get("datas") or {}).get("getOpeningRoom") or {}).get("rows") or []
        return [normalize_field(r) for r in rows]

    async def sports_fields(self, campus: str, project: str, day: str, slot: str, mode: str | None = None) -> dict[str, Any]:
        c, p = await self._resolve(campus, project)
        yylx = self._mode(p, mode)
        fields = await self._fields(c, p, yylx, day, slot)
        return {"campus": c["name"], "project": p["name"], "mode": MODE_NAMES[yylx], "date": day, "slot": slot,
                "items": [{k: v for k, v in f.items() if not k.startswith("_")} for f in fields]}

    async def sports_reserve(self, campus: str, project: str, day: str, slot: str, field: str | None,
                             *, confirm: bool, mode: str | None = None) -> dict[str, Any]:
        c, p = await self._resolve(campus, project)
        yylx = self._mode(p, mode)
        slots = await self.sports_slots(campus, project, day, MODE_NAMES[yylx])
        target = next((s for s in slots["items"] if s["label"] == slot), None)
        if not target or not target["selectable"]:
            raise CampusError("SPORTS_SLOT_UNAVAILABLE", f"{day} {slot} 不可预约（{target['state'] if target else '没有这个时段'}）")
        free = [f for f in await self._fields(c, p, yylx, day, slot) if f["available"]]
        if not free:
            raise CampusError("SPORTS_SLOT_UNAVAILABLE", f"{day} {slot} 已经没有空闲场地")
        if field is None and len(free) > 1:
            raise CampusError("SPORTS_FIELD_SELECTION_REQUIRED", "有多个场地可选", fields=[f["name"] for f in free])
        chosen = next((f for f in free if field is None or f["name"] == field), None) or \
            next((f for f in free if field and field in f["name"]), None)
        if not chosen:
            raise CampusError("NOT_FOUND", f"场地“{field}”不可用", fields=[f["name"] for f in free])
        preview = {"campus": c["name"], "venue": p["name"], "mode": MODE_NAMES[yylx], "field": chosen["name"], "date": day,
                   "selected": {"label": slot, "state": target["state"]}, "submitted": False}
        if not confirm:
            return preview
        uid, name = await self._sports_user()
        start, end = slot.split("-")
        form = {
            "DHID": "", "YYRGH": uid, "YYRXM": name,
            "CQBM": chosen["_cqbm"] or "", "CGDM": chosen["_cgbm"] or "", "CDWID": chosen["_wid"] or "",
            "XMDM": chosen["_xmdm"] or p["code"], "XQWID": chosen["_xqdm"] or c["code"],
            "KYYSJD": slot, "YYRQ": day, "YYLX": yylx, "YYKS": f"{day} {start}", "YYJS": f"{day} {end}",
            "CYRS": "1" if yylx == "2.0" else "", "PC_OR_PHONE": "pc",
        }
        resp = await self.http.request("POST", f"{SPORTS_APP}/sportVenue/insertVenueBookingInfo.do",
                                       headers={"X-Requested-With": "XMLHttpRequest"}, data=form)
        self.http.clear_cache()
        try:
            body = resp.json()
        except ValueError as exc:
            raise CampusError("SPORTS_SUBMIT_FAILED", "提交后学校没有返回 JSON，请去“我的预约”核实，不要重复提交", url=MY_BOOKING_PAGE) from exc
        msg = str(body.get("msg") or "")
        if str(body.get("code")) == "0":
            return {**preview, "submitted": True, "orderNo": (body.get("data") or {}).get("DHID"),
                    "payment": {"required": True, "deadlineMinutes": PAY_DEADLINE_MINUTES, "url": MY_BOOKING_PAGE}}
        raise CampusError("SPORTS_SUBMIT_FAILED", msg or "学校系统拒绝了预约")

    async def _sports_post_json(self, url: str, data: dict[str, str] | None = None,
                                error_code: str = "PAGE_CHANGED") -> dict[str, Any]:
        await self._sports_user()  # 先进入应用，拿到 MOD_AUTH_CAS
        resp = await self.http.request("POST", url, data=data or {}, headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            body = resp.json()
        except ValueError as exc:
            raise CampusError("PAGE_CHANGED", f"{url.rsplit('/', 1)[-1]} 没有返回 JSON") from exc
        if str(body.get("code")) != "0":
            raise CampusError(error_code, str(body.get("msg") or f"{url.rsplit('/', 1)[-1]} 返回 {body.get('code')}"))
        return body

    async def sports_bookings(self, limit: int = 5) -> dict[str, Any]:
        """我的预约（接口参数和返回来自 2026-11 抓包；地址按 EMAP 命名规律推断：返回的键名就是接口名）。"""
        body = await self._sports_post_json(MY_BOOKING_LIST, {"pageSize": str(limit), "pageNumber": "1"})
        info = (body.get("datas") or {}).get("myBookingInfo") or {}
        return {"total": info.get("totalSize"), "items": [normalize_booking(r) for r in info.get("rows") or []],
                "url": MY_BOOKING_PAGE}

    async def sports_balance(self) -> dict[str, Any]:
        """体育专项经费余额（getSelfMoney.do，2026-11 抓包确认）。"""
        body = await self._sports_post_json(SELF_MONEY)
        rows = ((body.get("datas") or {}).get("getSelfMoney") or {}).get("rows") or [{}]
        from .parsers import _num

        return {"sportsFundCny": _num(rows[0].get("ZJE")) or 0}

    async def sports_payment_info(self, order_no: str) -> dict[str, Any]:
        """付款卡片用：订单当前状态 + 体育经费余额 + 参考价格。只读。"""
        bookings = (await self.sports_bookings(limit=20))["items"]
        booking = next((b for b in bookings if b["orderNo"] == order_no), None)
        if booking is None:
            raise CampusError("NOT_FOUND", f"最近的预约里没有订单 {order_no}")
        balance = (await self.sports_balance())["sportsFundCny"]
        return {"booking": booking, "sportsFundCny": balance, "payDeadline": pay_deadline(booking.get("bookedAt")),
                "referencePriceCny": reference_price(bookings, booking["projectCode"]), "payUrl": MY_BOOKING_PAGE}

    async def sports_cancel(self, order_no: str, *, confirm: bool) -> dict[str, Any]:
        """取消预约（cancelSelectBookingInfo.do，参数是预约记录的 WID；2026-11 抓包）。
        confirm=False 时只核对订单能否取消并返回预览；confirm=True 提交后重新查询，确认学校那边真的取消了。"""
        bookings = (await self.sports_bookings(limit=20))["items"]
        b = next((x for x in bookings if x["orderNo"] == order_no), None)
        if b is None:
            raise CampusError("NOT_FOUND", f"最近的预约里没有订单 {order_no}")
        if b["cancelled"]:
            raise CampusError("SPORTS_ALREADY_CANCELLED", f"订单 {order_no} 已经是取消状态（{b['payState']}）")
        if not b["cancellable"]:
            raise CampusError("SPORTS_NOT_CANCELLABLE", f"这条预约已经不能取消（{b['status']} / {b['payState']}）")
        preview = {k: b[k] for k in ("orderNo", "timeRange", "campus", "project", "venue", "field", "status", "payState", "paid")}
        if not confirm:
            return {**preview, "cancelled": False}
        await self._sports_post_json(CANCEL_BOOKING, {"WID": b["wid"]}, error_code="SPORTS_CANCEL_FAILED")
        self.http.clear_cache()
        after = next((x for x in (await self.sports_bookings(limit=20))["items"] if x["orderNo"] == order_no), None)
        return {**preview, "cancelled": True, "verified": bool(after and after["cancelled"]),
                "statusAfter": after["payState"] if after else None}

    # ------------------------------------------------------------ 电费（协议来自 szu-dianchacha，需校园网）
    #   GET  /cgcSims/              建立会话
    #   POST /cgcSims/login.do      client + buildingId + roomName -> 页面里的 roomId
    #   POST /cgcSims/selectList.do roomId + 日期 + type(2 用电 / 1 充值) + pageNo，逐页读取
    # 表单用 UTF-8 编码提交，返回页面是 GB2312。

    async def electricity_buildings(self) -> dict[str, Any]:
        """楼栋列表是静态配置，不需要联网。"""
        return {"campuses": [{"name": c, "buildings": list(cfg["buildings"])} for c, cfg in CAMPUS_MAP.items()],
                "note": "丽湖二期（梧桐树、青冈栎等）是另一套电费系统，暂不支持"}

    def _resolve_dorm(self, building: str, campus: str | None) -> tuple[str, str, str]:
        """(校区, 楼栋全名, 楼栋编号)。支持“红豆”“乔森11”这类唯一的部分匹配。"""
        hits = [(c, name, bid) for c, cfg in CAMPUS_MAP.items() if campus is None or campus in c or c in campus
                for name, bid in cfg["buildings"].items() if building == name or building in name]
        exact = [h for h in hits if h[1] == building]
        hits = exact or hits
        if len(hits) != 1:
            raise CampusError("ELECTRICITY_ROOM_NOT_FOUND",
                              f"楼栋“{building}”{'没有匹配' if not hits else '匹配到多个'}",
                              candidates=[f"{c} {n}" for c, n, _ in hits][:10])
        return hits[0]

    async def _sims_session(self, client: str) -> str:
        for host in dict.fromkeys([SIMS_HOST, client]):  # 先试统一入口，再试校区自己的服务器
            base = f"http://{host}:9090/cgcSims"
            try:
                await self.http.request("GET", f"{base}/", require_login=False)
                return base
            except CampusError as exc:
                if exc.code not in ("NETWORK_REQUIRED", "NETWORK_ERROR"):
                    raise
        raise CampusError("NETWORK_REQUIRED", "连不上学校电费系统：需要校园网或学校 VPN")

    async def _sims_records(self, base: str, form: dict[str, str], record_type: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        page, pages, total = 1, 1, None
        while page <= pages:
            resp = await self.http.request("POST", f"{base}/selectList.do", require_login=False,
                                           data={**form, "type": str(record_type), "pageNo": str(page)})
            parsed = parse_sims_page(decode(resp, default="gb18030"), record_type)
            if total is None:
                total, pages = parsed["total"], parsed["pages"]
                if pages > 100:
                    raise CampusError("ELECTRICITY_RANGE_TOO_LONG", "记录太多，请缩短日期范围")
            elif (parsed["total"], parsed["pages"]) != (total, pages):
                raise CampusError("PAGE_CHANGED", "查询期间数据发生变化，请重试")
            for r in parsed["rows"]:
                if r["no"] in seen:
                    raise CampusError("PAGE_CHANGED", "电费系统返回了重复的页，已停止以免算错")
                seen.add(r["no"])
                rows.append(r)
            page += 1
        if total and len(rows) != total:
            raise CampusError("PAGE_CHANGED", f"记录不完整：学校声明 {total} 条，实际读到 {len(rows)} 条")
        return rows

    async def _electricity(self, building: str, room: str, campus: str | None,
                           from_date: str | None, to_date: str | None, record_type: int, days: int) -> dict[str, Any]:
        room = room.strip()
        if not re.fullmatch(r"[0-9A-Za-z-]{1,16}", room):
            raise CampusError("ELECTRICITY_ROOM_NOT_FOUND", "房间号格式不对，例如 838")
        campus_name, building_name, building_id = self._resolve_dorm(building, campus)
        client = CAMPUS_MAP[campus_name]["client"]
        to_d = date.fromisoformat(to_date) if to_date else date.today()
        from_d = date.fromisoformat(from_date) if from_date else to_d - timedelta(days=days)
        base = await self._sims_session(client)
        resp = await self.http.request("POST", f"{base}/login.do", require_login=False, data={
            "client": client, "buildingId": building_id, "buildingName": building_name, "roomName": room, "select": " 查询 "})
        room_id = parse_sims_room_id(decode(resp, default="gb18030"))
        if not room_id:
            raise CampusError("ELECTRICITY_ROOM_NOT_FOUND", f"{campus_name} {building_name} 没有房间 {room}")
        form = {"hiddenType": "0", "isHost": "0", "beginTime": from_d.isoformat(), "endTime": to_d.isoformat(),
                "client": client, "roomId": room_id, "roomName": room, "building": building_name}
        records = sorted(await self._sims_records(base, form, record_type),
                         key=lambda r: r.get("recordedAt") or r.get("purchasedAt") or "")
        return {"campus": campus_name, "building": building_name, "room": room,
                "from": from_d.isoformat(), "to": to_d.isoformat(), "records": records}

    async def electricity_query(self, building: str, room: str, campus: str | None = None,
                                from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        data = await self._electricity(building, room, campus, from_date, to_date, record_type=2, days=7)
        latest = data["records"][-1] if data["records"] else None
        return {**data, "remainingKwh": latest["remainingKwh"] if latest else None, "latestRecord": latest}

    async def electricity_purchases(self, building: str, room: str, campus: str | None = None,
                                    from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        data = await self._electricity(building, room, campus, from_date, to_date, record_type=1, days=90)
        return {**data, "totalKwh": round(sum(r["kwh"] or 0 for r in data["records"]), 2),
                "totalCny": round(sum(r["amountCny"] or 0 for r in data["records"]), 2)}


class SwzxApi:
    """事务中心场地系统（venue-api）的收发：booth.BoothBackend 的真实实现。

    swzx.webvpn.szu.edu.cn 是挂在学校 WebVPN 上的站点（2026-11-14 用户在校园网里自检确认）：
    没有 WebVPN 会话时，任何请求都会被带去
        webvpn.szu.edu.cn/vpn_key/update → /users/sign_in → /users/auth/cas
        → authserver-443.webvpn.szu.edu.cn/authserver/login（经 WebVPN 转发的统一认证）
    有 WebVPN 会话后，场地系统自己还有一层登录：接口返回 401 → /venue-api/login?front=页面 → 统一认证 → /venue-api/login/cas。

    WebVPN 会话只能由用户在界面的“登录 WebVPN”卡片里建立（cas.CasClient(realm="webvpn")，扫码或账号密码，
    之后在同一个会话里做二次验证）。这里发现没有会话就报 WEBVPN_LOGIN_REQUIRED，界面据此弹卡片。
    2026-11-15 用调试账号实测：WebVPN 登录 + 二次验证后，/venue-api/login 自动完成，user/current、booth/list 正常返回。
    """

    def __init__(self, http: CampusHttp) -> None:
        self.http = http

    async def login(self) -> httpx.Response:
        from .booth import BOOTH_PAGE, LOGIN_URL
        from .debuglog import where

        try:
            resp = await self.http.request("GET", LOGIN_URL, params={"front": BOOTH_PAGE})
        except CampusError as exc:
            if exc.code == "MFA_REQUIRED":  # WebVPN 的二次验证要和登录在同一个会话里做，单独验证过不了
                raise CampusError("WEBVPN_LOGIN_REQUIRED", "需要重新登录学校 WebVPN（含二次验证）") from exc
            raise
        if not self._on_swzx(resp):
            raise CampusError("WEBVPN_LOGIN_REQUIRED", f"需要登录学校 WebVPN（停在 {where(resp.url)}）")
        return resp

    @staticmethod
    def _on_swzx(resp: httpx.Response) -> bool:
        return urlparse(str(resp.url)).hostname == "swzx.webvpn.szu.edu.cn"

    async def api(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None) -> Any:
        from .booth import VENUE_API, unwrap

        kw: dict[str, Any] = {"params": params, "headers": {"Accept": "application/json"}}
        if json_body is not None:
            kw["json"] = json_body
        for attempt in range(2):
            need_login = False
            try:
                resp = await self.http.request(method, f"{VENUE_API}{path}", **kw)
                need_login = not self._on_swzx(resp)  # 被带去 WebVPN / 统一认证登录页了
            except CampusError as exc:
                if not (exc.code == "PERMISSION_DENIED" and exc.details.get("status") == 401):
                    raise
                need_login = True  # 场地系统自己的会话没建立
            if need_login:
                if attempt == 0:
                    await self.login()
                    continue
                raise CampusError("LOGIN_REQUIRED", "事务中心系统登录没有成功")
            try:
                body = resp.json()
            except ValueError as exc:
                from .debuglog import where

                raise CampusError("PAGE_CHANGED", f"事务中心接口没有返回 JSON（停在 {where(resp.url)}）") from exc
            if resp.status_code != 200:
                msg = body.get("message") if isinstance(body, dict) else None
                raise CampusError("BOOTH_REQUEST_FAILED", msg or f"事务中心接口返回 {resp.status_code}")
            return unwrap(body)
        raise CampusError("LOGIN_REQUIRED", "事务中心系统登录没有成功")
