"""校园层测试：解析器、密码加密、HTTP 会话、统一认证登录流程、真实服务的请求构造。

用 httpx.MockTransport 模拟学校服务器，离线验证完整的 HTTP 交互（跳转、Cookie、表单编码）。
"""

import base64
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote_to_bytes

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from szu_agent.campus import CampusError, SessionStore
from szu_agent.campus.cas import CasClient, encrypt_password, parse_login_page
from szu_agent.campus.http import CampusHttp, encode_form, parse_form
from szu_agent.campus.parsers import (
    active_in_week, build_courses, courses_on, normalize_lecture, parse_board,
    parse_library_search, parse_sims_page, parse_notice_detail, parse_notice_list,
)
from szu_agent.campus.service import RealCampus

FIX = Path(__file__).parent / "fixtures"
MOCK_FIX = Path(__file__).parents[1] / "src/szu_agent/mock_fixtures"
AUTH = "https://authserver.szu.edu.cn/authserver"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 解析器（与 szu-cli 原实现的期望结果对齐）

def test_notice_list_parser_matches_szu_cli():
    data = parse_notice_list(read(FIX / "notice-list.html"))
    assert data["total"] == 4 and len(data["items"]) == 4
    first, third = data["items"][0], data["items"][2]
    assert first["id"] == "577164" and first["isPinned"] and first["hasAttachment"]
    assert first["title"] == "关于2025-2026学年第二学期本科期末考试缓考申请的通知"  # “|置顶|”前缀被去掉
    assert third == {**third, "category": "教务", "publisher": "金融科技学院", "date": "2026-06-22", "isPinned": False}


def test_board_homepage_parser():
    items = parse_board(read(FIX / "notice-homepage.html"), today=date(2026, 6, 23))
    assert [i["category"] for i in items] == ["教务", "教务", "行政", "学工"]  # “教务教学”等被归一
    assert items[0]["date"] == "2026-06-18" and items[0]["isPinned"]


def test_notice_detail_parser():
    d = parse_notice_detail(read(FIX / "notice-view.html"), "https://www1.szu.edu.cn/board/view.asp?id=577444")
    assert d["id"] == "577444" and d["publisher"] == "深圳南特金融科技学院"
    assert d["publishedAt"] == "2026-06-22 10:30:00"
    assert "领取地点：汇元楼。" in d["contentText"]
    assert "附件：领取名单.docx" in d["contentText"]  # 行内链接不被拆行
    assert [a["name"] for a in d["attachments"]] == ["领取名单.docx", "办理说明.pdf"]
    assert d["attachments"][0]["url"] == "https://www1.szu.edu.cn/board/uploadfiles/file.docx"


def test_library_parser():
    data = parse_library_search(read(MOCK_FIX / "library-search.html"))
    assert data["total"] == 96
    assert data["items"][0] == {
        "id": "3706432", "title": "交通设计", "authors": "刘立新, 孟祥海, 陈亮主编", "publisher": "北京理工大学出版社",
        "publishYear": "2025", "callNumber": "U491/L73", "holdings": 2, "available": 2,
        "detailUrl": "https://www.lib.szu.edu.cn/opac/bookinfo.aspx?ctrlno=3706432",
    }


def test_electricity_parser():
    page = parse_sims_page(read(FIX / "electricity-usage.html"), 2)
    assert [r["remainingKwh"] for r in page["rows"]] == [342.48, 338.88]
    assert page["rows"][-1]["recordedAt"] == "2026-01-02 23:59:01"  # 去掉了 .0
    assert (page["total"], page["page"], page["pages"]) == (2, 1, 1)
    with pytest.raises(CampusError):  # 没有分页信息就拒绝，避免把不完整数据当完整结果
        parse_sims_page("<table><tr><td>1</td></tr></table>", 2)


def test_course_week_filter_and_today():
    assert active_in_week("1-8周,10-17周", 9) is False and active_in_week("1-8周,10-17周", 10) is True
    assert active_in_week("3周", 3) and not active_in_week("3周", 4)
    payload = build_courses(json.loads(read(MOCK_FIX / "course-api.json")))
    friday = courses_on(payload, date(2026, 11, 13))
    assert [c["courseName"] for c in friday["items"]] == ["马克思主义基本原理"]
    assert "XH" not in json.dumps(friday) and "演示用户" not in json.dumps(friday)  # 不带学号姓名


def test_lecture_normalize_states():
    from datetime import datetime

    now = datetime(2026, 11, 13, 12)
    row = {"id": 1, "name": "x", "status": "正在报名中", "startRegistration": "2026-11-12 08:00:00", "deadlineRegistration": "2026-11-18 18:00:00"}
    assert normalize_lecture(row, [{"remainSeats": 3}], now)["availabilityState"] == "available"
    assert normalize_lecture(row, [{"remainSeats": 0}], now)["availabilityState"] == "full"
    assert normalize_lecture(row, None, now)["availabilityState"] == "unknown"
    assert normalize_lecture({**row, "status": "报名已结束"}, [], now)["availabilityState"] == "closed"


# ------------------------------------------------------------------ 统一认证

def test_password_encryption_matches_login_page_js():
    """AES-CBC(随机64字符 + 密码)。IV 随机不可知，但前 64 字节随机前缀正好 4 个分组，
    从第 4 个分组开始解密不受 IV 影响，应当还原出密码。"""
    salt = parse_login_page(read(FIX / "cas-login.html"), "pwdFromId").salt
    ct = base64.b64decode(encrypt_password("p@ss 中文", salt))
    dec = Cipher(algorithms.AES(salt.encode()), modes.CBC(ct[48:64])).decryptor()
    plain = dec.update(ct[64:]) + dec.finalize()
    assert plain[: -plain[-1]].decode() == "p@ss 中文"
    assert encrypt_password("same", salt) != encrypt_password("same", salt)  # 每次随机


def test_login_page_parser_on_real_page():
    page = parse_login_page(read(FIX / "cas-login.html"), "pwdFromId")
    assert page.execution and page.salt


def cas_server(*, captcha=False, qr_states=("0", "2", "1"), wrong_password=False):
    """模拟 authserver：登录页、二维码接口、提交登录（成功时设置 CASTGC 并跳回 service）。"""
    login_html = read(FIX / "cas-login.html")
    states = list(qr_states)
    seen: dict = {"posts": []}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and req.url.path == "/authserver/login":
            if "CASTGC" in req.headers.get("cookie", ""):
                return httpx.Response(302, headers={"Location": "https://ehall.szu.edu.cn/new/index.html"})
            return httpx.Response(200, text=login_html, headers={"Content-Type": "text/html; charset=utf-8"})
        if "/qrCode/getToken" in url:
            return httpx.Response(200, text="QR-abc")
        if "/qrCode/getCode" in url:
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")
        if "/qrCode/getStatus.htl" in url:
            return httpx.Response(200, text=states.pop(0) if states else "1")
        if "/checkNeedCaptcha.htl" in url:
            return httpx.Response(200, json={"isNeed": captcha})
        if req.method == "POST" and req.url.path == "/authserver/login":
            form = parse_qs(req.content.decode())
            seen["posts"].append(form)
            if wrong_password:
                return httpx.Response(200, text='<span id="showErrorTip">您提供的用户名或者密码有误</span>')
            return httpx.Response(302, headers={"Location": "https://ehall.szu.edu.cn/new/index.html",
                                                "Set-Cookie": "CASTGC=TGT-123; Path=/authserver; Secure"})
        if req.url.host == "ehall.szu.edu.cn":
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


async def test_qr_login_full_flow(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    transport, seen = cas_server()
    cas = CasClient(store, transport=transport)
    assert (await cas.qr_image()).startswith("data:image/png;base64,")
    assert await cas.qr_poll() == "waiting"
    assert await cas.qr_poll() == "scanned"
    assert store.info()["hasSession"] is False
    assert await cas.qr_poll() == "confirmed"
    post = seen["posts"][0]
    assert post["cllt"] == ["qrLogin"] and post["uuid"] == ["QR-abc"] and post["execution"][0]
    assert store.info() == {**store.info(), "hasSession": True, "method": "qr"}
    assert oct(store.path.stat().st_mode)[-3:] == "600"  # 凭证文件仅本人可读


async def test_password_login_sends_encrypted_password_and_remember_me(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    transport, seen = cas_server()
    cas = CasClient(store, transport=transport)
    await cas.password_login("2020123456", "secret")
    form = seen["posts"][0]
    assert form["username"] == ["2020123456"] and form["rememberMe"] == ["true"]
    assert form["password"][0] != "secret" and len(base64.b64decode(form["password"][0])) % 16 == 0
    assert "secret" not in store.path.read_text()  # 密码绝不落盘


async def test_password_login_refuses_captcha(tmp_path):
    transport, seen = cas_server(captcha=True)
    with pytest.raises(CampusError) as e:
        await CasClient(SessionStore(tmp_path / "s.json"), transport=transport).password_login("u", "p")
    assert e.value.code == "CAPTCHA_REQUIRED" and seen["posts"] == []  # 不提交、不尝试破解


async def test_password_login_wrong_password(tmp_path):
    transport, _ = cas_server(wrong_password=True)
    store = SessionStore(tmp_path / "s.json")
    with pytest.raises(CampusError) as e:
        await CasClient(store, transport=transport).password_login("u", "bad")
    assert e.value.code == "LOGIN_FAILED" and "密码有误" in e.value.message
    assert store.info()["hasSession"] is False


# ------------------------------------------------------------------ HTTP 会话

async def test_redirect_to_login_page_means_login_required(tmp_path):
    def handler(req):
        if req.url.host == "www1.szu.edu.cn":
            return httpx.Response(302, headers={"Location": f"{AUTH}/login?service=x"})
        return httpx.Response(200, text="<form id='pwdFromId'></form>")

    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    with pytest.raises(CampusError) as e:
        await http.get_html("https://www1.szu.edu.cn/board/")
    assert e.value.code == "LOGIN_REQUIRED"


async def test_session_written_by_another_process_is_picked_up(tmp_path):
    """Web 进程登录后写会话文件，MCP 进程下一次请求就应该带上新 Cookie。"""
    store = SessionStore(tmp_path / "s.json")
    sent = []

    def handler(req):
        sent.append(req.headers.get("cookie", ""))
        return httpx.Response(200, text="ok")

    http = CampusHttp(store, transport=httpx.MockTransport(handler))
    await http.request("GET", "https://authserver.szu.edu.cn/authserver/x")
    jar = httpx.Cookies()
    jar.set("CASTGC", "TGT-new", domain="authserver.szu.edu.cn", path="/")
    SessionStore(tmp_path / "s.json").save(jar)  # 模拟另一个进程
    await http.request("GET", "https://authserver.szu.edu.cn/authserver/x")
    assert "CASTGC" not in sent[0] and "CASTGC=TGT-new" in sent[1]


def test_intranet_and_gbk_helpers():
    assert encode_form({"keyword": "奖学金"}, "gb2312") == b"keyword=%BD%B1%D1%A7%BD%F0"
    html = '<form action="infolist.asp" method="post"><select name="dayy"><option value="7#1周内">1周内</option>' \
           '<option value="183#6个月内" selected>6个月内</option></select><input name="keyword"><input type="submit" name="b"></form>'
    method, action, fields = parse_form(html, "https://www1.szu.edu.cn/board/", contains="keyword")
    assert (method, action) == ("post", "https://www1.szu.edu.cn/board/infolist.asp")
    assert fields == {"dayy": "183#6个月内", "keyword": ""}  # submit 按钮不算字段


# ------------------------------------------------------------------ 真实服务的请求构造

async def test_notice_search_submits_real_form_in_gbk(tmp_path):
    board = ('<html><head><meta charset="gb2312"></head><body><form action="infolist.asp" method="post">'
             '<select name="dayy"><option value="1#24小时内">24小时内</option><option value="30#1个月内">1个月内</option></select>'
             '<select name="search_type"><option value="标题">标题</option><option value="全文">全文</option></select>'
             '<select name="from_username"><option value="">全部发文单位</option><option value="学生部">12.学生部</option></select>'
             '<input name="keyword" value="(关键字)"><input name="tnull" type="text"><input type="submit" name="searchb1" value="搜索"></form></body></html>')
    posted = {}

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, content=board.encode("gb18030"))
        posted["body"] = req.content
        return httpx.Response(200, content=read(FIX / "notice-list.html").encode("gb18030"),
                              headers={"Content-Type": "text/html; charset=gb2312"})

    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
    data = await svc.notice_search("考试", search_in="full", range="30d", limit=2, publisher="学生部")
    form = {k: unquote_to_bytes(v).decode("gb18030") for k, v in (p.split("=") for p in posted["body"].decode().split("&"))}
    # searchb1=搜索 是关键：浏览器点按钮时会带上它，学校靠它判断“这是搜索”；漏了它关键词会被忽略（2026-11 实测）
    assert form == {"dayy": "30#1个月内", "search_type": "全文", "from_username": "学生部", "keyword": "考试",
                    "tnull": "", "searchb1": "搜索"}
    assert [i["id"] for i in data["items"]] == ["577164", "577097"]
    assert data["items"][0]["kind"] == "申请/报名" and "byKind" in data["stats"]  # “缓考申请的通知”


def sports_booking_server(calls):
    """按学校前端脚本还原的预约流程：目录 -> 日期 -> 时段 -> 场地 -> 提交。"""
    # 真实格式（2026-11 自检）：校区名在 XQDM_DISPLAY；同一项目每个校区一条记录，预约方式可以不同
    catalog = {"code": "0", "campusList": [{"XQDM": "1", "XQDM_DISPLAY": "粤海校区"}, {"XQDM": "2", "XQDM_DISPLAY": "丽湖校区"}],
               "xmList": [{"XMDM": "001", "XMMC": "羽毛球", "XQDM": "1", "DCFS": "1.0", "STORE_NAME": None},
                          {"XMDM": "001", "XMMC": "羽毛球", "XQDM": "2", "DCFS": "1.0", "STORE_NAME": None},
                          {"XMDM": "005", "XMMC": "篮球", "XQDM": "1", "DCFS": "2.0", "STORE_NAME": None},
                          {"XMDM": "005", "XMMC": "篮球", "XQDM": "2", "DCFS": "1.0,2.0", "STORE_NAME": None},
                          {"XMDM": "007", "XMMC": "一楼重量型健身", "XQDM": "1", "DCFS": "2.0", "STORE_NAME": None},
                          {"XMDM": "008", "XMMC": "二楼有氧健身", "XQDM": "1", "DCFS": "2.0", "STORE_NAME": None},
                          {"XMDM": "020", "XMMC": "健身中心", "XQDM": "2", "DCFS": "2.0", "STORE_NAME": None}],
               "dismissalVenueList": [], "packageVenueList": [], "tzggList": [], "venueDescList": [], "colList": []}

    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        form = parse_qs(req.content.decode()) if req.method == "POST" else {}
        calls.append((name, {k: v[0] for k, v in form.items()}))
        if name == "index.do":
            return httpx.Response(200, text='<script>var USER_INFO = {"id":"2020123456","name":"张三"};</script>')
        if name == "getSportVenueData.do":
            return httpx.Response(200, json=catalog)
        if name == "getRqList.do":
            return httpx.Response(200, json=["2026-11-15", "2026-11-16"])
        if name == "getTimeList.do":
            return httpx.Response(200, json=[{"CODE": "20:00-21:00", "NAME": "20:00-21:00", "WID": "t1", "text": "可预约", "disabled": False},
                                             {"CODE": "21:00-22:00", "NAME": "21:00-22:00", "WID": "t2", "text": "已满员", "disabled": True}])
        if name == "getOpeningRoom.do":
            return httpx.Response(200, json={"code": "0", "datas": {"getOpeningRoom": {"rows": [
                # 真实字段（2026-11 自检）：没有 CQBM，场馆名在 CGBM_DISPLAY
                {"WID": "cd-a4", "CDMC": "羽毛球场A4", "CGBM": "004", "CGBM_DISPLAY": "运动广场东馆羽毛球场", "XMDM": "001",
                 "XQDM": "1", "DCFS": "1.0", "disabled": False, "text": "可预约"},
                {"WID": "cd-a5", "CDMC": "羽毛球场A5", "CGBM": "004", "CGBM_DISPLAY": "运动广场东馆羽毛球场", "XMDM": "001",
                 "XQDM": "1", "DCFS": "1.0", "disabled": True, "text": "已满员"}]}}})
        if name == "insertVenueBookingInfo.do":
            return httpx.Response(200, json={"code": "0", "msg": "成功", "data": {"DHID": "D1"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_sports_catalog_and_project_matching(tmp_path):
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_booking_server([])))
    yuehai = (await svc.sports_projects("粤海"))["items"]
    assert [i["name"] for i in yuehai] == ["羽毛球", "篮球", "一楼重量型健身", "二楼有氧健身"]
    lihu = {i["name"]: i["modes"] for i in (await svc.sports_projects("丽湖校区"))["items"]}
    assert lihu == {"羽毛球": ["包场"], "篮球": ["包场", "散场"], "健身中心": ["散场"]}
    assert next(i for i in yuehai if i["name"] == "篮球")["modes"] == ["散场"]  # 同一项目，两个校区方式不同
    with pytest.raises(CampusError) as e:
        await svc.sports_slots("粤海校区", "健身")  # 两个都像：必须让用户选，不能随便挑
    assert e.value.code == "SPORTS_PROJECT_NOT_FOUND" and len(e.value.details["available"]) == 2
    with pytest.raises(CampusError) as e:
        await svc.sports_slots("粤海校区", "篮球", mode="包场")  # 粤海篮球只有散场
    assert e.value.code == "SPORTS_MODE_UNSUPPORTED"
    slots = await svc.sports_slots("粤海校区", "羽毛球")
    assert slots["date"] == "2026-11-15" and slots["mode"] == "包场"  # 默认最近开放日期；粤海羽毛球只有包场
    assert (await svc.sports_slots("丽湖校区", "篮球"))["mode"] == "散场"  # 两种都支持时优先散场
    assert [s["selectable"] for s in slots["items"]] == [True, False]


async def test_sports_reservation_request(tmp_path):
    """提交的参数完全按学校脚本 sportVenueSave2.js 拼装；场地的内部编号原样带回，姓名学号不出现在返回值里。"""
    calls = []
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_booking_server(calls)))
    fields = await svc.sports_fields("粤海校区", "羽毛球", "2026-11-15", "20:00-21:00", mode="包场")
    assert [(f["name"], f["venue"], f["available"]) for f in fields["items"]] == [
        ("羽毛球场A4", "运动广场东馆羽毛球场", True), ("羽毛球场A5", "运动广场东馆羽毛球场", False)]
    assert all(not k.startswith("_") for f in fields["items"] for k in f)  # 内部编号不给 LLM
    room = next(f for n, f in calls if n == "getOpeningRoom.do")
    assert room == {"XMDM": "001", "YYRQ": "2026-11-15", "YYLX": "1.0", "KSSJ": "20:00", "JSSJ": "21:00", "XQDM": "1"}

    preview = await svc.sports_reserve("粤海校区", "羽毛球", "2026-11-15", "20:00-21:00", None, confirm=False, mode="包场")
    assert preview["field"] == "羽毛球场A4" and preview["mode"] == "包场"  # 唯一可用的场地自动选中
    assert not any(n == "insertVenueBookingInfo.do" for n, _ in calls)
    with pytest.raises(CampusError) as e:
        await svc.sports_reserve("粤海校区", "羽毛球", "2026-11-15", "21:00-22:00", None, confirm=True, mode="包场")
    assert e.value.code == "SPORTS_SLOT_UNAVAILABLE"

    done = await svc.sports_reserve("粤海校区", "羽毛球", "2026-11-15", "20:00-21:00", "A4", confirm=True, mode="包场")
    form = next(f for n, f in calls if n == "insertVenueBookingInfo.do")
    assert {k: form.get(k) for k in ("CDWID", "CGDM", "CQBM", "XMDM", "XQWID", "YYLX", "KYYSJD", "YYKS", "YYJS", "YYRGH")} == {
        "CDWID": "cd-a4", "CGDM": "004", "CQBM": None, "XMDM": "001", "XQWID": "1", "YYLX": "1.0",  # CQBM 空值，和学校脚本一致
        "KYYSJD": "20:00-21:00", "YYKS": "2026-11-15 20:00", "YYJS": "2026-11-15 21:00", "YYRGH": "2020123456"}
    assert "CYRS" not in form  # 包场不填人数（散场是 1）
    assert done["submitted"] and done["orderNo"] == "D1" and done["payment"]["deadlineMinutes"] == 45
    assert "张三" not in json.dumps(done, ensure_ascii=False) and "2020123456" not in json.dumps(done)


# ------------------------------------------------------------------ 二次验证（手写的最小页面，真实页面含个人信息不入库）

MFA_PAGE = """<html><body><script>
var reAuthParams = {"reAuthType":"2","isMultifactor":"true","ipAnomaly":"0","isSleepAccount":"0",
 "reAuthDec0":"你即将访问深圳大学内部网，您设置了该系统需要二次认证，","service":"http://www1.szu.edu.cn/manage/caslogin.asp?rurl=%2Fboard%2F",
 "reAuthUserId":"2020123456","pwdEncryptSalt":"abcdefghijklmnop"};
</script>
<div data-type="2" data-name="账号密码"></div><div data-type="4" data-name="企业微信验证码"></div>
<a class="reauth-tab-more-item" href="javascript:;" id="3" title="短信验证码" data-name="短信验证码">短信验证码</a>
<a class="reauth-tab-more-item" href="javascript:;" id="10" data-name="安全令牌">x</a>
</body></html>"""


def mfa_server(*, good_code="123456"):
    posts = []
    passed = {"v": False}

    def handler(req):
        path = req.url.path
        if req.url.host == "www1.szu.edu.cn":
            if passed["v"]:
                return httpx.Response(200, text="<html>公文通</html>")
            return httpx.Response(302, headers={"Location": f"{AUTH}/reAuthCheck/reAuthLoginView.do?isMultifactor=true"})
        if path.endswith("/reAuthLoginView.do"):
            return httpx.Response(200, text=MFA_PAGE, headers={"Content-Type": "text/html; charset=utf-8"})
        if req.method == "POST":
            form = parse_qs(req.content.decode())
            posts.append((path.rsplit("/", 1)[-1], form))
            if path.endswith("/getDynamicCodeByReauth.do"):
                return httpx.Response(200, json={"res": "success", "returnMessage": "验证码已发送至", "mobile": "138****0000"})
            if path.endswith("/reAuthSubmit.do"):
                ok = form.get("dynamicCode") == [good_code] or form.get("reAuthType") == ["2"]
                passed["v"] = ok
                return httpx.Response(200, json={"code": "reAuth_success"} if ok else {"code": "reAuth_failed", "msg": "验证码错误"})
            if path.endswith("/changeReAuthType.do"):
                return httpx.Response(200, json={"code": 1, "data": {"reAuthType": form["reAuthType"][0]}})
            return httpx.Response(200, json={"code": 1})
        if path.endswith("/login"):
            return httpx.Response(302, headers={"Location": "https://www1.szu.edu.cn/board/"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), posts


async def test_mfa_page_is_detected_with_target(tmp_path):
    transport, _ = mfa_server()
    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=transport)
    with pytest.raises(CampusError) as e:
        await http.get_html("https://www1.szu.edu.cn/board/")
    assert e.value.code == "MFA_REQUIRED" and e.value.details["target"] == "https://www1.szu.edu.cn/board/"


async def test_mfa_flow_send_code_and_submit(tmp_path):
    from szu_agent.campus.mfa import MfaClient

    store = SessionStore(tmp_path / "s.json")
    transport, posts = mfa_server()
    mfa = MfaClient(store, transport=transport)
    info = await mfa.start("https://www1.szu.edu.cn/board/")
    assert info["needed"] and [m["type"] for m in info["methods"]] == ["2", "4", "3"]  # 不支持的安全令牌被过滤
    assert "2020123456" not in json.dumps(info, ensure_ascii=False)  # 学号不返回给前端
    assert "138****0000" in await mfa.send_code("4")
    sent = dict(posts)
    assert sent["changeReAuthType.do"]["reAuthType"] == ["4"]
    assert sent["getDynamicCodeByReauth.do"] == {"userName": ["2020123456"], "authCodeTypeName": ["reAuthWChatDynamicCodeType"]}

    with pytest.raises(CampusError) as e:
        await mfa.submit("4", "000000")
    assert e.value.code == "MFA_FAILED" and "验证码错误" in e.value.message
    await mfa.submit("4", "123456")
    submit = [f for p, f in posts if p == "reAuthSubmit.do"][-1]
    # 实测只有 false（仅本次登录）才能通过，true 会被学校拦回二次验证页
    assert submit["skipTmpReAuth"] == ["false"] and submit["isMultifactor"] == ["true"]
    assert submit["service"] == ["http://www1.szu.edu.cn/manage/caslogin.asp?rurl=%2Fboard%2F"]
    # 验证通过后，同一份会话访问公文通不再被拦
    http = CampusHttp(store, transport=transport)
    html, _ = await http.get_html("https://www1.szu.edu.cn/board/")
    assert "公文通" in html


async def test_intranet_timeout_means_campus_network_required(tmp_path):
    """代理 TUN 会让内网地址“握手成功但永远不响应”，这类超时应提示连校园网，而不是笼统的网络错误。"""
    def handler(req):
        raise httpx.ReadTimeout("hang", request=req)

    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    with pytest.raises(CampusError) as e:
        await http.request("GET", "http://192.168.84.3:9090/cgcSims/login.do", require_login=False)
    assert e.value.code == "NETWORK_REQUIRED" and "校园网" in e.value.message
    with pytest.raises(CampusError) as e:
        await http.request("GET", "https://www1.szu.edu.cn/board/")
    assert e.value.code == "NETWORK_ERROR"  # 公网系统超时仍是普通网络错误



# ------------------------------------------------------------------ 电费（SIMS 协议）

def sims_server(*, room_exists=True, usage_pages=2):
    """模拟电控系统：会话 -> login.do 返回 roomId -> selectList.do 分页返回记录（GB2312 编码）。"""
    seen = {"login": None, "pages": []}

    def page_html(record_type: int, page: int, pages: int) -> bytes:
        rows = []
        per = 2
        total = per * pages
        for i in range(per):
            no = (page - 1) * per + i + 1
            if record_type == 2:
                rows.append(f"<tr><td>{no}</td><td>838</td><td>{50 - no}.5</td><td>{1000 + no}</td><td>1050</td><td>2026-11-{no:02d} 23:59:00.0</td></tr>")
            else:
                rows.append(f"<tr><td>{no}</td><td>838</td><td>张三</td><td>微信购电</td><td>71.43</td><td>50</td><td>2026-11-{no:02d} 12:00:00.0</td></tr>")
        html = (f"<html><body><table>{''.join(rows)}<tr><td>共找到&nbsp;{total}&nbsp;条数据, 每页显示{per}条记录, "
                f"当前页: {page} / {pages}</td></tr></table></body></html>")
        return html.encode("gb18030")

    def handler(req):
        if req.url.path == "/cgcSims/":
            return httpx.Response(200, text="ok", headers={"Set-Cookie": "JSESSIONID=abc; Path=/cgcSims"})
        form = {k: unquote_to_bytes(v).decode("utf-8") for k, v in (p.split("=", 1) for p in req.content.decode().split("&"))}
        assert "JSESSIONID=abc" in req.headers.get("cookie", "")
        if req.url.path == "/cgcSims/login.do":
            seen["login"] = form
            body = '<input type="hidden" name="roomId" value="R-9">' if room_exists else "<p>无此房间</p>"
            return httpx.Response(200, content=body.encode("gb18030"))
        if req.url.path == "/cgcSims/selectList.do":
            seen["pages"].append(form)
            t, p = int(form["type"]), int(form["pageNo"])
            return httpx.Response(200, content=page_html(t, p, usage_pages if t == 2 else 1))
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


async def test_electricity_query_follows_sims_protocol(tmp_path):
    transport, seen = sims_server(usage_pages=2)
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=transport))
    data = await svc.electricity_query("红豆", "838", from_date="2026-11-01", to_date="2026-11-07")
    login = seen["login"]
    assert login["buildingName"] == "红豆斋" and login["buildingId"] == "18120" and login["client"] == "192.168.84.87"
    assert {p["roomId"] for p in seen["pages"]} == {"R-9"} and [p["pageNo"] for p in seen["pages"]] == ["1", "2"]
    assert data["campus"] == "新斋区" and len(data["records"]) == 4  # 两页合并
    assert data["remainingKwh"] == 46.5 and data["latestRecord"]["recordedAt"] == "2026-11-04 23:59:00"


async def test_electricity_purchases_hide_buyer_name(tmp_path):
    transport, seen = sims_server()
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=transport))
    data = await svc.electricity_purchases("红豆斋", "838")
    assert seen["pages"][0]["type"] == "1" and data["totalCny"] == 100.0
    assert "张三" not in json.dumps(data, ensure_ascii=False)


async def test_electricity_room_and_building_errors(tmp_path):
    transport, _ = sims_server(room_exists=False)
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=transport))
    with pytest.raises(CampusError) as e:
        await svc.electricity_query("红豆斋", "999")
    assert e.value.code == "ELECTRICITY_ROOM_NOT_FOUND"
    with pytest.raises(CampusError) as e:
        await svc.electricity_query("乔", "101")  # 北校区好几栋“乔×阁”
    assert e.value.code == "ELECTRICITY_ROOM_NOT_FOUND" and len(e.value.details["candidates"]) > 1
    with pytest.raises(CampusError):
        await svc.electricity_query("红豆斋", "838; drop")  # 房间号格式校验



def test_parse_form_includes_only_the_clicked_button():
    html = '<form><input name="q"><input type="submit" name="go" value="搜索"><input type="submit" name="reset" value="重置"></form>'
    assert parse_form(html, "https://x/", contains="q")[2] == {"q": ""}
    assert parse_form(html, "https://x/", contains="q", submit="go")[2] == {"q": "", "go": "搜索"}


async def test_notice_search_retries_when_sso_lands_on_homepage(tmp_path):
    board = ('<html><head><meta charset="gb2312"></head><form action="infolist.asp" method="post">'
             '<input name="keyword"><input type="submit" name="searchb1" value="搜索"></form></html>').encode("gb18030")
    posts = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, content=board)
        posts.append(1)
        if len(posts) == 1:  # 第一次：会话刚过期，POST 被单点登录重定向，最后落在首页
            return httpx.Response(302, headers={"Location": "https://www1.szu.edu.cn/board/"})
        return httpx.Response(200, content=read(FIX / "notice-list.html").encode("gb18030"))

    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
    data = await svc.notice_search("通知", search_in="full")
    assert len(posts) == 2 and data["total"] == 4


def test_legacy_detail_page_and_privacy():
    """公文通线上实际的老版布局；样例里登录用户的姓名学号已替换成“张三2020123456”。"""
    d = parse_notice_detail(read(FIX / "notice-view-legacy.html"), "https://www1.szu.edu.cn/board/view.asp?id=1")
    assert d["title"] == "关于评选2026年深圳大学学术启航奖学金的通知"
    assert d["publisher"] == "国际交流与合作部" and d["publishedAt"] == "2026-10-27 16:47:00"
    assert "申请截止时间：2026年12月18日" in d["contentText"]
    assert d["contentText"].startswith("为进一步鼓励学生") and not d["contentText"].startswith("深圳大学官网")
    # 当前登录用户的身份信息绝不能进入正文（会被发给 LLM）
    assert "张三" not in d["contentText"] and "2020123456" not in d["contentText"] and "个人中心" not in d["contentText"]


def test_notice_classification():
    from szu_agent.campus.parsers import classify_notice

    assert classify_notice("计算机学院2026年研究生学业奖学金拟推荐名单公示") == {"kind": "公示/结果", "audience": "研究生"}
    assert classify_notice("关于启动2026年应届本科毕业生奖学金评定工作的通知") == {"kind": "申请/报名", "audience": "本科生"}
    assert classify_notice("荔园之星奖学金评选结果公示")["kind"] == "公示/结果"  # “评选”+“结果”应判为结果


async def test_attachment_download_rejects_html_and_foreign_hosts(tmp_path):
    def handler(req):
        if req.url.path.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.4", headers={"Content-Type": "application/pdf"})
        return httpx.Response(200, text="<html>文件不存在</html>", headers={"Content-Type": "text/html"})

    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
    content, ctype = await svc.notice_attachment("https://www1.szu.edu.cn/board/uploadfiles/a.pdf")
    assert content.startswith(b"%PDF") and ctype == "application/pdf"
    with pytest.raises(CampusError):  # 学校返回网页而不是文件（链接失效/要求登录）
        await svc.notice_attachment("https://www1.szu.edu.cn/board/uploadfiles/gone.doc")
    with pytest.raises(CampusError) as e:
        await svc.notice_attachment("https://authserver.szu.edu.cn/authserver/login")
    assert e.value.code == "PERMISSION_DENIED"


async def test_attachment_request_uses_gbk_and_referer(tmp_path):
    """公文通附件下载的两个硬性要求（2026-11 实测）：文件名按 GBK 编码 + 带详情页 Referer。"""
    from szu_agent.campus.service import gbk_url

    seen = {}

    def handler(req):
        seen["query"], seen["referer"] = req.url.query, req.headers.get("referer")
        return httpx.Response(200, content=b"%PDF-1.4", headers={"Content-Type": "application/octet-stream"})

    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
    await svc.notice_attachment("https://www1.szu.edu.cn/board/down1oad.asp?fn=2026单位推荐表.pdf", "586077")
    assert seen["query"] == "fn=2026%B5%A5%CE%BB%CD%C6%BC%F6%B1%ED.pdf".encode()  # GBK 编码，不是 UTF-8
    assert seen["referer"] == "https://www1.szu.edu.cn/board/view.asp?id=586077"
    # 已经编码过的链接不会被二次编码
    assert gbk_url(gbk_url("https://www1.szu.edu.cn/board/d.asp?fn=中文.pdf")) == gbk_url("https://www1.szu.edu.cn/board/d.asp?fn=中文.pdf")


async def test_attachment_school_refusal_message_is_shown(tmp_path):
    refusal = '<script>alert("抱歉！请通过公文通电脑版或手机企微版下载附件");</script>'.encode("gb18030")

    def handler(req):
        return httpx.Response(200, content=refusal, headers={"Content-Type": "text/html"})

    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
    with pytest.raises(CampusError) as e:
        await svc.notice_attachment("https://www1.szu.edu.cn/board/down1oad.asp?fn=a.pdf")
    assert "电脑版" in e.value.message  # 把学校的原话转给用户，而不是乱码


# ------------------------------------------------------------------ 体育预约 · 我的预约 / 付款信息

def sports_app_server(seen, *, cancel_code="0"):
    my = json.loads(read(FIX / "my-booking.json"))

    def handler(req):
        path = req.url.path
        seen.append((path.rsplit("/", 1)[-1], req.content.decode()))
        if path.endswith("/index.do"):
            return httpx.Response(200, text='<script>var USER_INFO = {"id":"2020123456","name":"张三"};</script>')
        if path.endswith("/myBookingInfo.do"):
            return httpx.Response(200, json=my)
        if path.endswith("/cancelSelectBookingInfo.do"):
            if cancel_code != "0":
                return httpx.Response(200, json={"code": cancel_code, "msg": "距开始不足2小时，不能取消", "data": None})
            wid = parse_qs(req.content.decode())["WID"][0]
            for row in my["datas"]["myBookingInfo"]["rows"]:  # 模拟学校把这条预约改成已取消
                if row["WID"] == wid:
                    row.update(YYZT="CG_QX", YYZT_DISPLAY="取消预约", VERIFY_TYPE="QX_WZF", VERIFY_TYPE_DISPLAY="已取消", KYQX="N")
            return httpx.Response(200, json={"code": "0", "msg": "成功", "data": None})
        if path.endswith("/getSelfMoney.do"):
            return httpx.Response(200, json={"datas": {"getSelfMoney": {"totalSize": 1, "rows": [{"ZJE": "0.00"}]}}, "code": "0"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_my_bookings_from_real_response(tmp_path):
    seen = []
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_app_server(seen)))
    data = await svc.sports_bookings(limit=10)
    assert ("myBookingInfo.do", "pageSize=10&pageNumber=1") in seen
    first, cancelled, done = data["items"][0], data["items"][1], data["items"][2]
    assert first["awaitingPayment"] and not first["paid"] and first["cancellable"] and first["payState"] == "未支付"
    assert cancelled["cancelled"] and "作废" in cancelled["payState"]
    assert done["paid"] and done["amountCny"] == 5.0
    assert "2020123456" not in json.dumps(data)  # 学号字段在解析时就被丢掉


async def test_payment_info_balance_and_reference_price(tmp_path):
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_app_server([])))
    info = await svc.sports_payment_info("202611131638195253")
    assert info["booking"]["awaitingPayment"] and info["sportsFundCny"] == 0
    assert info["referencePriceCny"] == 5.0  # 待支付订单金额是 0，参考同项目上次实付
    with pytest.raises(CampusError):
        await svc.sports_payment_info("999")



async def test_cancel_booking_uses_wid_and_verifies(tmp_path):
    seen = []
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_app_server(seen)))
    preview = await svc.sports_cancel("202611131638195253", confirm=False)
    assert preview["cancelled"] is False and preview["timeRange"].startswith("2026-11-14 10:00")
    assert not any(p == "cancelSelectBookingInfo.do" for p, _ in seen)  # 预览绝不提交
    done = await svc.sports_cancel("202611131638195253", confirm=True)
    assert ("cancelSelectBookingInfo.do", "WID=w1") in seen  # 接口要的是 WID 不是订单号
    assert done["cancelled"] and done["verified"] and done["statusAfter"] == "已取消"


async def test_cancel_booking_refusals(tmp_path):
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=sports_app_server([])))
    with pytest.raises(CampusError) as e:
        await svc.sports_cancel("202608122046572738", confirm=False)  # 已完成的
    assert e.value.code == "SPORTS_NOT_CANCELLABLE"
    with pytest.raises(CampusError) as e:
        await svc.sports_cancel("202611131322034362", confirm=False)  # 早就因未支付被取消
    assert e.value.code == "SPORTS_ALREADY_CANCELLED"
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "t.json"), transport=sports_app_server([], cancel_code="1")))
    with pytest.raises(CampusError) as e:
        await svc.sports_cancel("202611131638195253", confirm=True)
    assert e.value.code == "SPORTS_CANCEL_FAILED" and "2小时" in e.value.message  # 学校的原话


# ------------------------------------------------------------------ 天气

OPEN_METEO = {
    "current": {"time": "2026-11-14T10:45", "temperature_2m": 24.3, "apparent_temperature": 25.6, "relative_humidity_2m": 68,
                "weather_code": 2, "cloud_cover": 55},
    "daily": {"temperature_2m_max": [27.1, 26.0], "temperature_2m_min": [19.8, 20.2]},
    "hourly": {"time": [f"2026-11-14T{h:02d}:00" for h in range(24)],
               "precipitation_probability": [5] * 15 + [60, 70] + [20] * 7},
}


def test_weather_parsers():
    from datetime import datetime

    from szu_agent.campus.weather import parse_open_meteo, parse_wttr

    om = parse_open_meteo(OPEN_METEO)
    assert (om["kind"], om["desc"], om["tempC"], om["todayMin"], om["todayMax"]) == ("cloudy", "多云", 24, 20, 27)
    assert om["rainAt"] == "15:00" and om["rainChance"] == 70 and om["raining"] is False  # 只看接下来的小时
    wt = parse_wttr(json.loads(read(FIX / "wttr.json")), now=datetime(2026, 11, 14, 10, 41))
    assert wt["kind"] == "drizzle" and wt["raining"] and wt["tempC"] == 23 and 0 <= wt["cloud"] <= 1


async def test_weather_falls_back_to_wttr_and_caches():
    from szu_agent.campus import weather as W

    W._cache.clear()
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "api.open-meteo.com":
            raise httpx.ConnectTimeout("blocked", request=req)
        return httpx.Response(200, text=read(FIX / "wttr.json"), headers={"Content-Type": "application/json"})

    data = await W.get_weather("丽湖", transport=httpx.MockTransport(handler))
    assert data["source"] == "wttr.in" and data["campus"] == "丽湖校区"
    assert calls == ["api.open-meteo.com", "wttr.in"]
    await W.get_weather("丽湖校区", transport=httpx.MockTransport(handler))
    assert len(calls) == 2  # 15 分钟内走缓存，不重复请求

    W._cache.clear()

    def down(req):
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(CampusError) as e:
        await W.get_weather("粤海校区", transport=httpx.MockTransport(down))
    assert e.value.code == "NETWORK_ERROR" and len(e.value.details["sources"]) == 2
    W._cache.clear()


# ------------------------------------------------------------------ 电费充值方案

def test_recharge_plan():
    from szu_agent.campus.recharge import PRICE_PER_KWH, daily_usage, qr_svg, recharge_plan

    recs = [{"recordedAt": "2026-11-10 23:59:00", "remainingKwh": 41.7}, {"recordedAt": "2026-11-11 23:59:00", "remainingKwh": 35.2},
            {"recordedAt": "2026-11-12 23:59:00", "remainingKwh": 28.9}]
    assert daily_usage(recs) == 6.4
    # 中间充过值（剩余电量上升）的那一天不计入用电
    with_topup = recs + [{"recordedAt": "2026-11-13 23:59:00", "remainingKwh": 95.0}, {"recordedAt": "2026-11-14 23:59:00", "remainingKwh": 88.6}]
    assert daily_usage(with_topup) == 6.4
    assert daily_usage(recs[:1]) is None  # 数据不够就不估算
    plan = recharge_plan(28.9, recs)
    assert round(PRICE_PER_KWH, 2) == 0.7 and plan["daysLeft"] == 4.5
    assert plan["options"][1] == {"amountCny": 50, "kwh": 71.4, "extraDays": 11.2}  # 50 元 ≈ 71.4 度（抓包：10 元 = 14.29 度）
    assert recharge_plan(None, [])["options"][0]["extraDays"] is None
    assert qr_svg("http://example.com/x").startswith("<svg")


def test_pay_url_never_carries_personal_params():
    from szu_agent.campus.recharge import PAY_URL, strip_personal

    leaked = ("https://weixiao.qq.com/apps/public/service/index.html?ADTAG=1768&card_id=CARD&code=ONE_TIME"
              "&encrypt_code=CARDNO&hid=HALL&media_id=gh_x&openid=WECHAT_ID&state=login#")
    clean = strip_personal(leaked)
    for secret in ("ONE_TIME", "CARDNO", "WECHAT_ID", "openid", "encrypt_code", "state="):
        assert secret not in clean
    assert "card_id=CARD" in clean and "hid=HALL" in clean and "media_id=gh_x" in clean
    assert "openid" not in PAY_URL and "code=" not in PAY_URL.replace("encrypt_code", "")


def test_mfa_start_rejects_non_school_targets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from szu_agent.web.app import create_app

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    with TestClient(create_app(mock=True)) as client:
        assert client.post("/api/auth/mfa", json={"target": "http://127.0.0.1:8080/admin"}).status_code == 400
        assert client.post("/api/auth/mfa", json={"target": "https://evil.example/szu.edu.cn"}).status_code == 400


async def test_mfa_second_step_is_returned_not_failed(tmp_path):
    """经 WebVPN 访问时学校可能连要两步（企业微信验证码 → 手机短信）：第一步通过后回跳又落在验证页，
    应该把第二步的验证方式交给界面继续，而不是报“验证失败”。"""
    from szu_agent.campus.mfa import MfaClient

    state = {"passed": 0}

    def handler(req):
        path = req.url.path
        if req.url.host == "www1.szu.edu.cn":
            return httpx.Response(200, text="<html>公文通</html>") if state["passed"] >= 2 else \
                httpx.Response(302, headers={"Location": f"{AUTH}/reAuthCheck/reAuthLoginView.do"})
        if path.endswith("/reAuthLoginView.do"):
            page = MFA_PAGE if state["passed"] == 0 else MFA_PAGE.replace('"reAuthType":"4"', '"reAuthType":"3"')
            return httpx.Response(200, text=page, headers={"Content-Type": "text/html; charset=utf-8"})
        if path.endswith("/reAuthSubmit.do"):
            state["passed"] += 1
            return httpx.Response(200, json={"code": "reAuth_success"})
        if path.endswith("/login"):
            return httpx.Response(302, headers={"Location": "https://www1.szu.edu.cn/board/"})
        if path.endswith("/changeReAuthType.do"):
            return httpx.Response(200, json={"code": 1, "data": {}})
        return httpx.Response(200, json={"res": "success"})

    store = SessionStore(tmp_path / "s.json")
    mfa = MfaClient(store, transport=httpx.MockTransport(handler))
    assert (await mfa.start("https://www1.szu.edu.cn/board/"))["needed"]
    step2 = await mfa.submit("4", "111111")
    assert step2["needed"] and step2["step"] == 2 and step2["methods"]
    assert "2020123456" not in json.dumps(step2, ensure_ascii=False)
    assert await mfa.submit("3", "222222") == {"needed": False}



async def test_mfa_follows_reauth_js(tmp_path):
    """照学校 reAuth.js：当前已是该方式就不切换；切换失败（code!=1）直接报错；
    页面没有 isSleepAccount=0 时不发 skipTmpReAuth；isMultifactor 用页面上的值。"""
    from szu_agent.campus.mfa import MfaClient

    page = MFA_PAGE.replace('"reAuthType":"2","isMultifactor":"true","ipAnomaly":"0","isSleepAccount":"0",',
                            '"reAuthType":"4","isMultifactor":"false","ipAnomaly":"0",')
    posts = []

    def handler(req):
        path = req.url.path
        if req.url.host == "www1.szu.edu.cn":
            return httpx.Response(302, headers={"Location": f"{AUTH}/reAuthCheck/reAuthLoginView.do"})
        if path.endswith("/reAuthLoginView.do"):
            return httpx.Response(200, text=page, headers={"Content-Type": "text/html; charset=utf-8"})
        if path.endswith("/bfp/info"):
            return httpx.Response(200, json={})
        form = parse_qs(req.content.decode(), keep_blank_values=True)
        posts.append((path.rsplit("/", 1)[-1], form))
        if path.endswith("/changeReAuthType.do"):
            return httpx.Response(200, json={"code": "0", "message": "切换失败"})
        if path.endswith("/getDynamicCodeByReauth.do"):
            return httpx.Response(200, json={"res": "other_success", "returnMessage": "验证码已发送成功"})
        return httpx.Response(200, json={"code": "reAuth_failed", "msg": "认证失败"})

    mfa = MfaClient(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    await mfa.start("https://www1.szu.edu.cn/board/")
    await mfa.send_code("4")
    assert [p for p, _ in posts] == ["getDynamicCodeByReauth.do"]  # 已是企业微信，不切换
    with pytest.raises(CampusError):
        await mfa.submit("4", "123456")
    submit = dict(posts)["reAuthSubmit.do"]
    assert submit["isMultifactor"] == ["false"] and "skipTmpReAuth" not in submit
    with pytest.raises(CampusError) as e:
        await mfa.send_code("3")  # 要切换，但学校回 code=0
    assert e.value.code == "MFA_FAILED" and "切换失败" in e.value.message


async def test_mfa_registers_stable_device_fingerprint(tmp_path, monkeypatch):
    """照 common-header.js：进入验证页后 GET /bfp/info?bfp=设备指纹 登记，之后再发验证码；指纹每次都一样。"""
    from szu_agent.campus.mfa import MfaClient, device_id

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    transport, posts = mfa_server()
    calls = []

    def handler(req):
        if req.url.path.endswith("/bfp/info"):
            calls.append(req.url.params["bfp"])
            return httpx.Response(200, json={}, headers={"Set-Cookie": f"MULTIFACTOR_BROWSER_FINGERPRINT={req.url.params['bfp']}; Path=/"})
        return transport.handle_request(req)

    for _ in range(2):
        mfa = MfaClient(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
        await mfa.start("https://www1.szu.edu.cn/board/")
        assert "MULTIFACTOR_BROWSER_FINGERPRINT" in {c.name for c in mfa.client.cookies.jar}
    assert len(calls) == 2 and calls[0] == calls[1] == device_id()
    assert re.fullmatch(r"[0-9A-F]{32}", calls[0])
    assert (tmp_path / "device-id").stat().st_mode & 0o077 == 0  # 只有自己可读


def webvpn_server():
    """按 2026-11-15 调试抓到的链路模拟 WebVPN 登录：
    swzx/venue-api/login → (没有 WebVPN 会话) → authserver-443.webvpn 登录页 → 提交密码 → 二次验证页
    → reAuthSubmit 成功 → /authserver/login?service=回调 → WebVPN 回调种会话 → swzx/venue-api/login/cas → swzx。
    认证服务器只认“在本会话里登录过”的验证（JSESSIONID 必须是登录页发的那个）。"""
    vpn = "https://authserver-443.webvpn.szu.edu.cn/authserver"
    login_html = read(FIX / "cas-login.html")
    st = {"logged_in_session": None, "verified": False, "hosts": set()}

    def handler(req: httpx.Request) -> httpx.Response:
        host, path, cookie = req.url.host, req.url.path, req.headers.get("cookie", "")
        st["hosts"].add(host)
        if host == "swzx.webvpn.szu.edu.cn":
            if "wengine_vpn_ticket=" not in cookie:
                return httpx.Response(302, headers={"Location": f"{vpn}/login?service=https%3A%2F%2Fwebvpn.szu.edu.cn%2Fusers%2Fauth%2Fcas%2Fcallback%3Furl"})
            return httpx.Response(200, text="<html>事务中心</html>")
        if host == "webvpn.szu.edu.cn" and path == "/users/auth/cas/callback":
            return httpx.Response(302, headers={"Location": "https://swzx.webvpn.szu.edu.cn/",
                                                "Set-Cookie": "wengine_vpn_ticket=T; Domain=.webvpn.szu.edu.cn; Path=/"})
        assert host == "authserver-443.webvpn.szu.edu.cn", req.url  # 不会碰主认证服务器
        if path == "/authserver/login" and req.method == "GET":
            if st["verified"]:
                return httpx.Response(302, headers={"Location": "https://webvpn.szu.edu.cn/users/auth/cas/callback?ticket=ST"})
            return httpx.Response(200, text=login_html, headers={"Content-Type": "text/html; charset=utf-8",
                                                                  "Set-Cookie": "JSESSIONID=S1; Path=/authserver"})
        if path == "/authserver/checkNeedCaptcha.htl":
            return httpx.Response(200, json={"isNeed": False})
        if path == "/authserver/login" and req.method == "POST":
            assert "service=" in str(req.url)  # 提交到带 service 的登录地址
            st["logged_in_session"] = "S1" if "JSESSIONID=S1" in cookie else None
            return httpx.Response(302, headers={"Location": f"{vpn}/reAuthCheck/reAuthLoginView.do?isMultifactor=true",
                                                "Set-Cookie": "CASTGC=TGT-VPN; Path=/authserver"})
        if path.endswith("/reAuthLoginView.do"):
            return httpx.Response(200, text=MFA_PAGE.replace('"reAuthType":"2"', '"reAuthType":"4"'),
                                  headers={"Content-Type": "text/html; charset=utf-8"})
        if path.endswith("/bfp/info"):
            return httpx.Response(200, json={})
        if path.endswith("/getDynamicCodeByReauth.do"):
            return httpx.Response(200, json={"res": "other_success", "returnMessage": "验证码已发送成功"})
        if path.endswith("/reAuthSubmit.do"):
            ok = st["logged_in_session"] == "S1" and "JSESSIONID=S1" in cookie  # 必须是登录时的同一个会话
            st["verified"] = ok
            return httpx.Response(200, json={"code": "reAuth_success"} if ok else {"code": "reAuth_failed", "msg": "认证失败"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), st


async def test_webvpn_login_then_mfa_in_same_session(tmp_path, monkeypatch):
    from szu_agent.campus.mfa import MfaClient

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))  # device-id 写到临时目录
    if True:
        store = SessionStore(tmp_path / "s.json")
        jar = httpx.Cookies()
        jar.set("CASTGC", "TGT-MAIN", domain="authserver.szu.edu.cn", path="/authserver")  # 主登录要保留
        jar.set("route", "stale", domain="authserver-443.webvpn.szu.edu.cn", path="/authserver")  # 旧 WebVPN Cookie 要丢掉
        store.save(jar, method="qr", loggedInAt=1)
        transport, st = webvpn_server()
        cas = CasClient(store, transport=transport, realm="webvpn")
        await cas.password_login("2020123456", "secret")
        assert cas.mfa_response is not None  # 停在二次验证页，交给同一个会话继续

        mfa, info = await MfaClient.adopt(store, cas.client, cas.mfa_response)
        assert info["needed"] and info["current"] == "4" and mfa.client is cas.client
        await mfa.send_code("4")
        assert await mfa.submit("4", "123456") == {"needed": False}
        assert st["verified"] and "authserver.szu.edu.cn" not in st["hosts"]

        saved = {(c.name, c.domain) for c in store.cookies().jar}
        assert ("CASTGC", "authserver.szu.edu.cn") in saved            # 主登录没被冲掉
        assert ("wengine_vpn_ticket", ".webvpn.szu.edu.cn") in saved   # WebVPN 会话存下来了
        assert ("route", "authserver-443.webvpn.szu.edu.cn") not in saved
        assert store.info()["method"] == "qr"                          # 不改主登录的方式
        await mfa.aclose()


async def test_webvpn_mfa_in_a_new_client_fails_like_the_real_server(tmp_path, monkeypatch):
    """反例（就是之前踩的坑）：登录后换一个客户端去验证，学校回“认证失败”。"""
    from szu_agent.campus.mfa import MfaClient

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "s.json")
    transport, _ = webvpn_server()
    cas = CasClient(store, transport=transport, realm="webvpn")
    await cas.password_login("2020123456", "secret")
    other = MfaClient(store, transport=transport)
    other.client.cookies.clear()  # 新会话（没有登录页发的 JSESSIONID）
    await other.start("https://authserver-443.webvpn.szu.edu.cn/authserver/reAuthCheck/reAuthLoginView.do")
    with pytest.raises(CampusError) as e:
        await other.submit("4", "123456")
    assert "认证失败" in e.value.message



async def test_webvpn_mfa_requests_carry_browser_origin(tmp_path, monkeypatch):
    """二次验证的 XHR 和登录表单都要带 Origin（浏览器一定会带；WebVPN 上没有它时验证“认证失败”）。"""
    from szu_agent.campus.mfa import MfaClient

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    transport, _ = webvpn_server()
    origins = []

    def handler(req):
        if req.method == "POST":
            origins.append((req.url.path.rsplit("/", 1)[-1], req.headers.get("origin"), req.headers.get("referer", "")[:60]))
        return transport.handle_request(req)

    store = SessionStore(tmp_path / "s.json")
    cas = CasClient(store, transport=httpx.MockTransport(handler), realm="webvpn")
    await cas.password_login("2020123456", "secret")
    mfa, _ = await MfaClient.adopt(store, cas.client, cas.mfa_response)
    await mfa.send_code("4")
    await mfa.submit("4", "123456")
    want = "https://authserver-443.webvpn.szu.edu.cn"
    assert [o for _, o, _ in origins] == [want] * len(origins) and len(origins) == 3
    assert all(r.startswith(want) for _, _, r in origins)


async def test_check_webvpn_cli_runs_end_to_end(tmp_path, monkeypatch):
    """交互式诊断命令：用模拟的 WebVPN 跑通 登录 → 验证页参数 → 发码 → 提交；输出不含密码和学号。"""
    import builtins

    from szu_agent import selfcheck
    from szu_agent.campus import cas as cas_mod

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    transport, st = webvpn_server()
    real_init = cas_mod.CasClient.__init__
    monkeypatch.setattr(cas_mod.CasClient, "__init__",
                        lambda self, store, **kw: real_init(self, store, **{**kw, "transport": transport}))
    answers = iter(["2020123456", "", "123456"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-pw")

    async def no_booths(self, campus=None, kind=None):
        return {"booths": [{"name": "时光社区共享琴房1"}]}
    monkeypatch.setattr(selfcheck.RealCampus, "booth_list", no_booths)

    lines = []
    await selfcheck.check_webvpn(out=lines.append)
    text = "\n".join(lines)
    assert "[1] WebVPN 登录：✓" in text and "[3] 发送" in text and "[4] 提交（和浏览器一致）：✓" in text
    assert "[5] 查琴房：✓" in text and st["verified"]
    assert "secret-pw" not in text and "2020123456" not in text


async def test_mfa_sends_empty_multifactor_when_page_has_none(tmp_path, monkeypatch):
    """2026-11-15 浏览器抓包：页面的 isMultifactor 为空时，浏览器提交 isMultifactor=（空），这样才能通过。
    不能自作主张填 true——切换方式和提交都会被服务器按多因素规则拒绝。"""
    from szu_agent.campus.mfa import MfaClient, parse_mfa_page

    monkeypatch.setenv("SZU_AGENT_HOME", str(tmp_path))
    empty = MFA_PAGE.replace('"isMultifactor":"true"', '"isMultifactor":""').replace('"reAuthType":"2"', '"reAuthType":"4"')
    assert parse_mfa_page(empty)["multifactor"] == ""
    missing = MFA_PAGE.replace('"isMultifactor":"true",', '')
    assert parse_mfa_page(missing)["multifactor"] == ""

    posts = {}

    def handler(req):
        if req.url.host == "www1.szu.edu.cn":
            return httpx.Response(302, headers={"Location": f"{AUTH}/reAuthCheck/reAuthLoginView.do"})
        if req.url.path.endswith("/reAuthLoginView.do"):
            return httpx.Response(200, text=empty, headers={"Content-Type": "text/html; charset=utf-8"})
        if req.method == "POST":
            posts[req.url.path.rsplit("/", 1)[-1]] = parse_qs(req.content.decode(), keep_blank_values=True)
            if req.url.path.endswith("/changeReAuthType.do"):
                return httpx.Response(200, json={"code": 1, "data": {"reAuthType": "3"}})
            if req.url.path.endswith("/reAuthSubmit.do"):
                return httpx.Response(200, json={"code": "reAuth_success"})
            return httpx.Response(200, json={"res": "success"})
        if req.url.path.endswith("/login"):
            return httpx.Response(302, headers={"Location": "https://www1.szu.edu.cn/board/x"})
        return httpx.Response(200, json={})

    mfa = MfaClient(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    await mfa.start("https://www1.szu.edu.cn/board/")
    await mfa.send_code("3")
    await mfa.submit("3", "123456")
    assert posts["changeReAuthType.do"]["isMultifactor"] == [""]
    assert posts["reAuthSubmit.do"]["isMultifactor"] == [""]


# ------------------------------ 校外访问：网络模式与 WebVPN 路由 ------------------------------ #

def test_webvpn_url_rule():
    from szu_agent.campus.http import webvpn_url

    # 规律来自实测：www1-443 / ehall-443 / authserver-443（https），swzx（http）
    assert webvpn_url("https://lecture.szu.edu.cn/tLectureSignUp/list?page=1") == \
        "https://lecture-443.webvpn.szu.edu.cn/tLectureSignUp/list?page=1"
    assert webvpn_url("http://swzx.szu.edu.cn/mini/") == "https://swzx.webvpn.szu.edu.cn/mini/"


async def test_offcampus_routes_lecture_via_webvpn(monkeypatch, tmp_path):
    import httpx

    from szu_agent.campus import CampusError, SessionStore
    from szu_agent.campus.http import CampusHttp

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "lecture-443.webvpn.szu.edu.cn" and "logged" in req.headers.get("cookie", ""):
            return httpx.Response(200, json={"data": []})
        if req.url.host == "lecture-443.webvpn.szu.edu.cn":  # 没有 WebVPN 会话：被带去门户登录
            return httpx.Response(302, headers={"Location": "https://webvpn.szu.edu.cn/vpn_key/update"})
        return httpx.Response(200, text="<html>login</html>")

    monkeypatch.setenv("SZU_AGENT_NETWORK", "offcampus")
    monkeypatch.setattr("szu_agent.campus.http.VIA_WEBVPN", {"lecture.szu.edu.cn"})  # 机制保留，目前没启用
    monkeypatch.setattr("szu_agent.campus.http.OFFCAMPUS_UNAVAILABLE", set())
    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    with pytest.raises(CampusError) as e:
        await http.request("GET", "https://lecture.szu.edu.cn/tLectureSignUp/list")
    assert e.value.code == "WEBVPN_LOGIN_REQUIRED"
    assert seen[0] == "https://lecture-443.webvpn.szu.edu.cn/tLectureSignUp/list"

    http.cookies.set("wengine_vpn_ticket", "logged", domain=".webvpn.szu.edu.cn")
    resp = await http.request("GET", "https://lecture.szu.edu.cn/tLectureSignUp/list")
    assert resp.json() == {"data": []}

    # 内网 IP（电费）：校外不用等超时，直接说明服务器版用不了
    with pytest.raises(CampusError) as e:
        await http.request("GET", "http://192.168.84.3/cgcSims/")
    assert e.value.code == "NETWORK_REQUIRED" and "本机版" in e.value.message
    assert not any("192.168" in u for u in seen)


async def test_campus_mode_keeps_direct_access(monkeypatch, tmp_path):
    import httpx

    from szu_agent.campus import SessionStore
    from szu_agent.campus.http import CampusHttp

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        return httpx.Response(200, json={})

    monkeypatch.delenv("SZU_AGENT_NETWORK", raising=False)
    monkeypatch.delenv("SZU_AGENT_MULTIUSER", raising=False)
    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler))
    await http.request("GET", "https://lecture.szu.edu.cn/")
    assert seen == ["lecture.szu.edu.cn"]  # 单人模式默认在校园网：直连，行为和以前一样


async def test_offcampus_lecture_is_unavailable_for_now(monkeypatch, tmp_path):
    import httpx

    from szu_agent.campus import CampusError, SessionStore
    from szu_agent.campus.http import CampusHttp

    monkeypatch.setenv("SZU_AGENT_NETWORK", "offcampus")
    http = CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(CampusError) as e:
        await http.request("GET", "https://lecture.szu.edu.cn/")
    assert e.value.code == "NETWORK_REQUIRED" and "服务器版" in e.value.message


def test_mock_sports_dates_follow_today(monkeypatch, tmp_path):
    # 演示站的场馆开放日期跟着今天走，不会因为样例过期而约不到
    from szu_agent.campus.mock import MockCampus, shift_dates
    assert shift_dates('{"d": "2026-11-15", "no": "202611091405"}', 3) == '{"d": "2026-11-18", "no": "202611091405"}'
    monkeypatch.setenv("SZU_AGENT_MOCK_TODAY", "2027-03-01")
    m = MockCampus(SessionStore(tmp_path / "s.json"))
    assert m.sports["dates"] == ["2027-03-01", "2027-03-02"]
    assert set(m.sports["slotsByDate"]) == {"2027-03-01", "2027-03-02"}


# ------------------------------------------------------------------ 研究生成绩

def grade_server(seen, rows, *, json_api=True):
    def handler(req):
        seen.append((req.method, req.url.path.rsplit("/", 1)[-1], req.content.decode(), req.headers.get("x-requested-with")))
        if req.url.path.endswith("/index.do"):
            return httpx.Response(200, text="<html>我的成绩</html>", headers={"set-cookie": "_WEU=abc; path=/gsapp/"})
        if req.url.path.endswith("/xscjcx.do"):
            if not json_api:  # 本科生账号 / 入口变了：落在 HTML 页面上
                return httpx.Response(200, text="<html>您没有访问该应用的权限</html>")
            q = parse_qs(req.content.decode())
            size, page = int(q["pageSize"][0]), int(q["pageNumber"][0])
            return httpx.Response(200, json={"code": "0", "datas": {"xscjcx": {
                "totalSize": len(rows), "rows": rows[(page - 1) * size: page * size]}}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


GRADE_ROWS = [
    {"XH": "2099123456", "XM": "张三", "XNXQDM": "20251", "XNXQDM_DISPLAY": "2025-2026学年 第一学期",
     "KCMC": "机器学习", "XF": "3", "CJ": "92", "DYBFZCJ": "92", "JDZ": "4.0", "KSXZDM_DISPLAY": "正常考试",
     "KCLBMC": "学位专业课", "LRRXM": "李四", "BY1": ""},
    {"XH": "2099123456", "XM": "张三", "XNXQDM": "20251", "XNXQDM_DISPLAY": "2025-2026学年 第一学期",
     "KCMC": "论文写作", "XF": "1", "CJ": "通过", "DYBFZCJ": "", "JDZ": ""},
    {"XH": "2099123456", "XM": "张三", "XNXQDM": "20252", "XNXQDM_DISPLAY": "2025-2026学年 第二学期",
     "KCMC": "自然语言处理", "XF": "3", "CJ": "94", "DYBFZCJ": "94", "JDZ": ""},
    {"XH": "2099123456", "XM": "张三", "XNXQDM": "20252", "XNXQDM_DISPLAY": "2025-2026学年 第二学期",
     "KCMC": "深度学习专题", "XF": "2", "CJ": "", "DYBFZCJ": "84", "JDZ": "", "KSXZDM_DISPLAY": "补考"},
]


async def test_grades_request_pagination_and_privacy(tmp_path, monkeypatch):
    from szu_agent.campus import service

    monkeypatch.setattr(service, "GRADE_PAGE_SIZE", 3)  # 4 行、每页 3 行 → 必须翻到第 2 页
    seen = []
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=grade_server(seen, GRADE_ROWS)))
    data = await svc.grades()
    assert seen[0][:2] == ("GET", "index.do")  # 先进应用拿 /gsapp/ 的 Cookie
    posts = [s for s in seen if s[1] == "xscjcx.do"]
    assert [p[2] for p in posts] == ["pageSize=3&pageNumber=1", "pageSize=3&pageNumber=2"]
    assert all(p[3] == "XMLHttpRequest" for p in posts)
    assert data["availableTerms"] == ["2025-2026学年 第二学期", "2025-2026学年 第一学期"]  # 新学期在前
    dumped = json.dumps(data, ensure_ascii=False)
    assert not any(x in dumped for x in ("2099123456", "张三", "李四"))  # 学号、姓名、录入人姓名都不取
    assert data["terms"][1]["courses"][0]["category"] == "学位专业课"

    first_term = data["terms"][1]
    assert first_term["gpa"] == 4.0 and first_term["weightedScore"] == 92  # “通过”不进平均分和 GPA
    assert first_term["courses"][1]["score"] == "通过" and first_term["courses"][1]["scorePercent"] is None
    latest = data["terms"][0]
    assert latest["gpa"] is None and latest["gradePointMissing"] == 2  # 学校没给绩点：不推算、不给半截 GPA
    assert latest["courses"][1]["score"] == "84" and latest["courses"][1]["examType"] == "补考"
    assert data["overall"] == {"courseCount": 4, "credits": 9, "weightedScore": 90.75, "gpa": None, "gradePointMissing": 2}


async def test_grades_term_filter(tmp_path):
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=grade_server([], GRADE_ROWS)))
    latest = await svc.grades("latest")
    assert [t["term"] for t in latest["terms"]] == ["2025-2026学年 第二学期"]
    assert latest["overall"]["courseCount"] == 4  # overall 始终是全部课程
    for spelled in ("2025-2026学年第一学期", "2025-2026-1", "20251"):  # 学期名（空格随意）、学年-学期、学校代码
        assert [t["term"] for t in (await svc.grades(spelled))["terms"]] == ["2025-2026学年 第一学期"], spelled
    missing = await svc.grades("2030")
    assert missing["terms"] == [] and "availableTerms" in missing["hint"]


async def test_grades_non_json_is_page_changed(tmp_path):
    svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=grade_server([], [], json_api=False)))
    with pytest.raises(CampusError) as e:
        await svc.grades()
    assert e.value.code == "PAGE_CHANGED" and "研究生" in e.value.message
