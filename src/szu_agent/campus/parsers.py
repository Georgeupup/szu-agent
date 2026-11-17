"""学校网页/接口的解析器（纯函数，不做任何网络请求，便于用固定样例测试）。

公文通、图书馆、讲座、课表的解析规则移植自 szu-cli（MIT License, github.com/AwesomeHou/szu-cli）；
电费（SIMS）的解析和分页校验参考 szu-dianchacha（MIT License, github.com/yuyiwei01/szu-dianchacha）。
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

BOARD_URL = "https://www1.szu.edu.cn/board/"
OPAC_URL = "https://www.lib.szu.edu.cn/opac/"

CATEGORY_ALIASES = {"教务教学": "教务", "科研动态": "科研", "党务行政": "行政", "学生工作": "学工", "学术讲座": "讲座", "校园生活": "生活"}
ATTACHMENT_RE = re.compile(r"\.(docx?|xlsx?|pptx?|pdf|zip|rar|7z)(?:$|\?)", re.I)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(text or "")).strip()


def _id_from_url(url: str) -> str | None:
    return (parse_qs(urlparse(url).query).get("id") or [None])[0]


def _num(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        f = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


# ------------------------------------------------------------------ 公文通

def parse_board(html: str, *, today: date | None = None) -> list[dict[str, Any]]:
    """公文通首页：按类别分组的 fieldset，日期形如 6/18。"""
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for fs in soup.find_all("fieldset"):
        strong = fs.find("legend") and fs.find("legend").find("strong")
        if not strong:
            continue
        category = CATEGORY_ALIASES.get(clean(strong.get_text()), clean(strong.get_text()))
        for tr in fs.find_all("tr"):
            tds = tr.find_all("td")
            a = tds[0].find("a", href=re.compile(r"view\.asp\?id=")) if len(tds) >= 2 else None
            if not a:
                continue
            title_attr = clean(a.get("title"))
            title = clean(a.get_text())
            if title_attr and "专题：" not in title_attr and "时间：" not in title_attr:
                title = title_attr
            m = re.match(r"^(\d{1,2})/(\d{1,2})", clean(tds[1].get_text()))
            url = urljoin(BOARD_URL, a["href"])
            out.append({
                "id": _id_from_url(url), "category": category, "title": title,
                "date": f"{today.year}-{int(m[1]):02d}-{int(m[2]):02d}" if m else None,
                "isPinned": "|置顶|" in tds[0].get_text(), "url": url,
            })
    return out


def parse_notice_list(html: str) -> dict[str, Any]:
    """列表页/搜索结果页：序号|类别|发文单位|标题|附件|日期。"""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False) or tr.find_all("td")
        if len(tds) < 6 or not clean(tds[0].get_text()).isdigit():
            continue
        a = tds[3].find("a", href=re.compile(r"view\.asp\?id="))
        if not a:
            continue
        url = urljoin(BOARD_URL, a["href"])
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", clean(tds[5].get_text()))
        items.append({
            "id": _id_from_url(url),
            "category": clean(tds[1].get_text()),
            "publisher": clean(tds[2].get_text()),
            "title": re.sub(r"^(\|置顶\|\s*|·\s*)", "", clean(a.get_text())),
            "date": f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None,
            "isPinned": "|置顶|" in tds[3].get_text(),
            "hasAttachment": bool(tds[4].find("img", src=re.compile("attach", re.I))),
            "url": url,
        })
        items[-1].update(classify_notice(items[-1]["title"]))
    total = re.search(r"共\s*(\d+)\s*条", soup.get_text())
    return {"total": int(total[1]) if total else len(items), "items": items}


# 按标题给通知打标签。用规则而不是 LLM：几百条结果 LLM 读不过来，规则又快又可解释。
# 顺序有意义：“评选结果公示”应归为结果而不是申请。
NOTICE_KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("公示/结果", ("公示", "名单", "结果", "拟推荐", "拟获", "拟录取", "获奖", "表彰")),
    ("申请/报名", ("申报", "申请", "评选", "评定", "报名", "启动", "征集", "推荐", "选拔", "招募", "遴选")),
    ("活动/讲座", ("讲座", "活动", "比赛", "大赛", "论坛", "讲坛", "沙龙", "晚会", "运动会")),
    ("安排/提醒", ("安排", "时间", "调整", "放假", "考试", "缴费", "批扣", "提醒")),
]


def classify_notice(title: str) -> dict[str, str]:
    kind = next((k for k, words in NOTICE_KINDS if any(w in title for w in words)), "通知")
    if any(w in title for w in ("研究生", "博士", "硕士")):
        audience = "研究生"
    elif "本科" in title:
        audience = "本科生"
    else:
        audience = "不限/未注明"
    return {"kind": kind, "audience": audience}


def notice_stats(items: list[dict[str, Any]], top: int = 6) -> dict[str, Any]:
    """结果分布：让 agent 在几百条结果里先看清全貌，再决定读哪几篇。"""
    from collections import Counter

    def count(key: str) -> dict[str, int]:
        return dict(Counter(i.get(key) or "未知" for i in items).most_common(top))

    return {"byKind": count("kind"), "byAudience": count("audience"), "byPublisher": count("publisher")}


# 学校会在详情页插入“（姓名学号 时间浏览）”这一行，导航栏里也有“姓名 个人中心｜注销”。
# 这些是当前登录用户的身份信息，绝不能进入工具结果（会被发给 LLM）。
IDENTITY_LINE = re.compile(r"^[（(].*\d{6,}.*[）)]$|浏览[）)]\s*$")
NAV_LINE = re.compile(r"个人中心|注销")
LEGACY_META = re.compile(r"^(.+?)\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)$")


def _block_lines(el: Any) -> list[str]:
    for br in el.find_all("br"):
        br.replace_with("\n")
    for block in el.find_all(["p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4"]):
        block.append("\n")  # 只在块级元素后换行，行内链接保持在原句里
    return [x for x in (clean(t) for t in el.get_text("").split("\n")) if x and x not in ("｜", "|")]


def _attachments(el: Any, url: str) -> list[dict[str, str]]:
    out = []
    for a in el.find_all("a", href=True):
        name = clean(a.get_text())
        if name and (ATTACHMENT_RE.search(a["href"]) or ATTACHMENT_RE.search(name)):
            out.append({"name": name.lstrip("·• "), "url": urljoin(url, a["href"])})
    return out


def parse_notice_detail(html: str, url: str) -> dict[str, Any]:
    """两种页面布局：
    - 新版：正文在 td#content，标题在 td.info_title，元信息写成“发布单位：…发布时间：…”
    - 老版（目前线上实际的样子）：“关闭窗口｜打印张贴版”之后依次是 标题 / 发文单位 时间 / （浏览记录）/ 正文 / 撰稿：…
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    attachments = _attachments(soup.body or soup, url)
    title = publisher = published = None

    content_el = soup.find("td", id=re.compile("content|neirong|zw")) or soup.find("td", class_=re.compile("content|neirong|zw"))
    if content_el:
        title_el = soup.find("td", class_=re.compile("info_title|title")) or soup.find("td", id=re.compile("title"))
        title = clean(title_el.get_text()) if title_el else None
        text = soup.get_text(" ")
        if m := re.search(r"发布单位[:：]\s*(\S+)", text):
            publisher = m[1]
        if m := re.search(r"发布时间[:：]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)", text):
            published = _normalize_dt(m[1])
        lines = _block_lines(content_el)
    else:
        lines = _block_lines(soup.body or soup)
        start = next((i for i, l in enumerate(lines) if "打印" in l and i > 0 and "关闭窗口" in " ".join(lines[max(0, i - 2):i + 1])), None)
        if start is not None:
            lines = lines[start + 1:]
            end = next((i for i, l in enumerate(lines) if l.startswith(("撰稿", "（更新于", "(更新于")) or l == "校园公文通"), len(lines))
            lines = lines[:end]
            if lines:
                title, lines = lines[0], lines[1:]
            if lines and (m := LEGACY_META.match(lines[0])):
                publisher, published, lines = m[1].strip(), _normalize_dt(m[2].replace("/", "-")), lines[1:]
        if not title:
            title = re.sub(r"[—-]深圳大学内部网$", "", clean(soup.title.get_text() if soup.title else ""))

    lines = [l for l in lines if not IDENTITY_LINE.search(l) and not NAV_LINE.search(l)]
    return {
        "id": _id_from_url(url), "title": title, "publisher": publisher, "publishedAt": published,
        "contentText": "\n".join(lines), "attachments": attachments, "url": url,
    }


def _normalize_dt(value: str) -> str:
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", value)
    if not m:
        return value
    d = f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    return f"{d} {int(m[4]):02d}:{m[5]}:{m[6] or '00'}" if m[4] else d


# ------------------------------------------------------------------ 图书馆

def parse_library_search(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8 or not clean(tds[0].get_text()).isdigit():
            continue
        cells = [clean(td.get_text()) for td in tds]
        a = tr.find("a", href=re.compile("bookinfo.aspx", re.I))
        href = urljoin(OPAC_URL, a["href"]) if a else None
        items.append({
            "id": (parse_qs(urlparse(href).query).get("ctrlno") or [None])[0] if href else None,
            "title": cells[1] or None, "authors": cells[2] or None, "publisher": cells[3] or None,
            "publishYear": cells[4] or None, "callNumber": cells[5] or None,
            "holdings": _num(cells[6]), "available": _num(cells[7]), "detailUrl": href,
        })
    text = clean(soup.get_text(" "))
    total = re.search(r"结果数[:：]\s*(\d+)", text)
    return {"total": int(total[1]) if total else len(items), "items": items}


# ------------------------------------------------------------------ 讲座

def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    return clean(BeautifulSoup(value, "html.parser").get_text(" ")) or None


def normalize_lecture(row: dict[str, Any], classrooms: list[dict[str, Any]] | None, now: datetime) -> dict[str, Any]:
    def ts(v: str | None) -> datetime | None:
        try:
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S") if v else None
        except ValueError:
            return None

    start, deadline = ts(row.get("startRegistration")), ts(row.get("deadlineRegistration"))
    reg_open = row.get("status") == "正在报名中" and (not start or start <= now) and (not deadline or now <= deadline)
    rooms = classrooms or []
    remaining = sum(max(int(_num(r.get("remainSeats")) or 0), 0) for r in rooms)
    state = "closed" if not reg_open else "unknown" if classrooms is None else "available" if remaining > 0 else "full"
    return {
        "id": str(row.get("id")), "title": row.get("name"), "type": row.get("lectureType"),
        "teacher": row.get("teacherName"), "department": row.get("deptName"), "sponsor": row.get("nameOfSponsor"),
        "registrationStart": row.get("startRegistration"), "registrationDeadline": row.get("deadlineRegistration"),
        "lectureStart": row.get("lectureStartTime"), "lectureEnd": row.get("lectureEndTime"),
        "status": row.get("status"), "registrationOpen": reg_open, "availabilityState": state,
        "totalRemainingSeats": remaining,
        "rooms": [{"campus": r.get("campus"), "building": r.get("building"), "roomNumber": r.get("roomNumber"),
                   "remainingSeats": _num(r.get("remainSeats"))} for r in rooms],
        "introduction": _html_text(row.get("introduceOfLecture")),
    }


def lecture_progress(info: dict[str, Any]) -> dict[str, Any]:
    """只取进度相关字段，丢掉姓名、学号等身份信息。"""
    def part(done_key: str, need_key: str) -> dict[str, Any]:
        done, need = int(_num(info.get(done_key)) or 0), int(_num(info.get(need_key)) or 0)
        return {"completed": done, "required": need, "remaining": max(need - done, 0), "passed": need > 0 and done >= need}
    return {"offline": part("offlineTimes", "sumOfflineTimes"), "online": part("onlineTimes", "sumOnlineTimes")}


# ------------------------------------------------------------------ 课表

def _rows(api: dict | None, key: str) -> list[dict[str, Any]]:
    return ((api or {}).get("datas", {}).get(key) or {}).get("rows") or []


def active_in_week(weeks_text: str | None, week: int | None) -> bool:
    if not week or not weeks_text:
        return True
    # 真实写法（2026-11-17 研究生课表自检）：“1-16周”“16周”“1,3-4,6-7,9,11-12周”（“周”只在最后写一次）、
    # “2,5,8-10双周”（单双只管它所在的那一段区间：第 5 周是单数，照样上）、“1-15周(单)”
    for seg in re.split(r"[,，]", str(weeks_text)):
        if m := re.search(r"(\d+)\s*-\s*(\d+)", seg):
            if not int(m[1]) <= week <= int(m[2]):
                continue
            if ("单" in seg and week % 2 == 0) or ("双" in seg and week % 2 == 1):
                continue
            return True
        if (m := re.search(r"\d+", seg)) and int(m[0]) == week:
            return True
    return False


def build_courses(api: dict[str, Any]) -> dict[str, Any]:
    term = (_rows(api.get("dqxnxq"), "dqxnxq") or [{}])[0]
    week = (_rows(api.get("dqzc"), "dqzc") or [{}])[0].get("ZC")
    items = [{
        "courseName": r.get("KCM"),
        "teachers": [t.strip() for t in re.split(r"[,，]", str(r.get("SKJS") or "")) if t.strip()],
        "weeksText": r.get("ZCMC"), "weekday": _num(r.get("SKXQ")),
        "startSection": _num(r.get("KSJC")), "endSection": _num(r.get("JSJC")), "location": r.get("JASMC"),
    } for r in _rows(api.get("xskcb"), "xskcb")]
    return {"term": {"id": term.get("DM"), "name": term.get("MC"), "currentWeek": _num(week)}, "items": items}


def courses_on(payload: dict[str, Any], day: date) -> dict[str, Any]:
    weekday = day.isoweekday()
    week = payload["term"].get("currentWeek")
    items = [x for x in payload["items"] if x["weekday"] == weekday and active_in_week(x["weeksText"], week)]
    return {"term": payload["term"]["name"], "date": day.isoformat(), "items": sorted(items, key=lambda x: x["startSection"] or 0)}


# ------------------------------------------------------------------ 电费（SIMS）

SIMS_PAGER = re.compile(r"共找到\s*(\d+)\s*条数据\s*[,，]\s*每页显示\s*(\d+)\s*条记录\s*[,，]\s*当前页\s*[:：]\s*(\d+)\s*/\s*(\d+)")


def parse_sims_room_id(html: str) -> str | None:
    """login.do 返回的页面里，房间存在时会有 <input name="roomId" value="...">。"""
    el = BeautifulSoup(html, "html.parser").find("input", attrs={"name": "roomId"})
    return (el.get("value") or "").strip() or None if el else None


def parse_sims_page(html: str, record_type: int) -> dict[str, Any]:
    """selectList.do 的一页。record_type: 2=用电记录(6 列)，1=充值记录(7 列)。

    分页信息识别不出来就直接报错——宁可失败，也不要把不完整的数据当成完整结果。
    """
    from .errors import CampusError

    width = 6 if record_type == 2 else 7
    rows = []
    for tr in BeautifulSoup(html, "html.parser").find_all("tr"):
        cells = [clean(td.get_text()) for td in tr.find_all("td")]
        if len(cells) != width or not cells[0].isdigit():
            continue
        at = re.sub(r"\.0$", "", cells[-1])
        if record_type == 2:
            rows.append({"no": int(cells[0]), "room": cells[1], "remainingKwh": _num(cells[2]),
                         "totalUsedKwh": _num(cells[3]), "totalPurchasedKwh": _num(cells[4]), "recordedAt": at})
        else:
            # cells[2] 是购电人姓名，属于个人信息，不返回
            rows.append({"no": int(cells[0]), "room": cells[1], "purchaseType": cells[3],
                         "kwh": _num(cells[4]), "amountCny": _num(cells[5]), "purchasedAt": at})
    m = SIMS_PAGER.search(clean(BeautifulSoup(html, "html.parser").get_text(" ")))
    if not m:
        raise CampusError("PAGE_CHANGED", "电费页面里找不到分页信息，为避免展示不完整的数据已停止")
    return {"rows": rows, "total": int(m[1]), "page": int(m[3]), "pages": max(1, int(m[4]))}


# ------------------------------------------------------------------ 体育预约 · 我的预约

def normalize_booking(row: dict[str, Any]) -> dict[str, Any]:
    """myBookingInfo 的一行。只保留对用户有用的字段；YYRGH（学号）等身份字段直接丢掉。

    状态字段（2026-11 抓包）：
      YYZT  CG_YY 已预约 / CG_QX 取消预约 / CG_WC 已完成
      VERIFY_TYPE  YY_WZF 未支付 / QX_WZF 未支付取消预约，预约作废 / WC 已入场
      SFZF  是否已支付 "1"/"0"；KYQX 能否取消 "Y"/"N"
    """
    amount = _num(row.get("ACTULAMT")) or _num(row.get("TRANAMT")) or 0
    return {
        "orderNo": row.get("DHID"),
        "wid": row.get("WID"),  # 预约记录的内部编号，取消预约接口要的是它而不是订单号
        "timeRange": row.get("YYSJD"),
        "bookedAt": row.get("CJSJ"),
        "campus": row.get("XQWID_DISPLAY"),
        "project": row.get("XMDM_DISPLAY"),
        "venue": row.get("CGDM_DISPLAY"),
        "field": row.get("CDWID_DISPLAY"),
        "status": row.get("YYZT_DISPLAY"),
        "payState": row.get("VERIFY_TYPE_DISPLAY"),
        "paid": row.get("SFZF") == "1",
        "awaitingPayment": row.get("VERIFY_TYPE") == "YY_WZF",
        "cancelled": str(row.get("YYZT", "")).endswith("_QX"),
        "cancellable": row.get("KYQX") == "Y",
        "amountCny": amount,
        "projectCode": row.get("XMDM"),
    }


def reference_price(bookings: list[dict[str, Any]], project_code: str | None) -> float | None:
    """待支付订单里金额是 0，真实价格要到支付页才知道。用同项目最近一次实付金额作为参考价。"""
    for b in bookings:
        if b["paid"] and b["amountCny"] and (project_code is None or b["projectCode"] == project_code):
            return b["amountCny"]
    return None


# ------------------------------------------------------------------ 体育预约 · 项目目录 / 场地
# 字段名来自学校前端脚本 sportVenue.js / sportVenueSave2.js（公开 JS，2026-11 读取）：
#   getSportVenueData.do -> campusList[{XQDM}], xmList[{XMDM, XQDM(所属校区,可多个), DCFS(支持的预约方式 1.0 包场/2.0 散场)}]
#   getOpeningRoom.do    -> datas.getOpeningRoom.rows[{WID, CGBM, CQBM, XMDM, XQDM, disabled}]
# 名称字段经 2026-11 真实账号自检确认：校区 XQDM_DISPLAY、项目 XMMC、场地 CDMC、场馆 CGBM_DISPLAY；
# 场地行里没有 CQBM（学校脚本此时提交空值，这里保持一致）。仍保留其他候选名以防学校改字段。

MODE_NAMES = {"1.0": "包场", "2.0": "散场"}
MODE_CODES = {v: k for k, v in MODE_NAMES.items()}


def _first(row: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        if row.get(k) not in (None, ""):
            return str(row[k])
    return None


def parse_sports_catalog(data: dict[str, Any]) -> dict[str, Any]:
    campuses = [{"code": str(c.get("XQDM")), "name": _first(c, "XQMC", "XQDM_DISPLAY", "MC", "NAME") or str(c.get("XQDM"))}
                for c in data.get("campusList") or []]
    projects = []
    for x in data.get("xmList") or []:
        modes = [MODE_NAMES[m] for m in ("1.0", "2.0") if m in str(x.get("DCFS") or "")]
        projects.append({
            "code": str(x.get("XMDM")),
            "name": _first(x, "XMMC", "XMDM_DISPLAY", "MC", "NAME") or str(x.get("XMDM")),
            # 实测（2026-11 自检）：同一项目在每个校区各是一条记录，XQDM 是单个校区代码；按逗号拆分兼容多校区写法
            "campusCodes": [c.strip() for c in str(x.get("XQDM") or "").split(",") if c.strip()],
            "modes": modes,
        })
    return {"campuses": campuses, "projects": projects}


def normalize_field(row: dict[str, Any]) -> dict[str, Any]:
    disabled = row.get("disabled")
    return {
        "name": _first(row, "CDMC", "CDWID_DISPLAY", "CDMC_DISPLAY", "MC", "NAME") or str(row.get("WID")),
        "venue": _first(row, "CGMC", "CGBM_DISPLAY", "CGDM_DISPLAY"),
        "available": not (disabled is True or str(disabled).lower() == "true"),
        "state": _first(row, "text", "ZT_DISPLAY", "STATE"),
        # 以下是提交预约时要原样带回去的内部编号，不返回给 LLM
        "_wid": row.get("WID"), "_cgbm": row.get("CGBM"), "_cqbm": row.get("CQBM"),
        "_xmdm": row.get("XMDM"), "_xqdm": row.get("XQDM"),
    }


def normalize_slot(row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("text") or "")
    disabled = row.get("disabled")
    blocked = disabled is True or str(disabled).lower() == "true" or state in {"已过期", "已满员", "已约满"}
    return {"label": row.get("CODE"), "state": state, "selectable": not blocked}


# ------------------------------------------------------------------ 图书馆：书目详情、预借登记
# 来源：用户在登录状态下保存的真实页面（2026-11-14）：
#   bookinfo.aspx?ctrlno=      书目详情（服务器返回的页面里没有馆藏表）
#   bookinfowithresvAjax.aspx?ctrlno=  馆藏表：登录后页面 JS（listbookwithresv）请求它，把 <div id="bardiv"> 填进页面
#   admin_findbook/addfb.aspx  预借登记表单（ASP.NET WebForms，提交按钮 Button1）
#   admin_findbook/addfbcheckAjax.aspx?ctrlno=&volume=  预检，返回一个数字（含义来自 addfb.aspx 里的 showResponse()）
#   user/myfb.aspx?page=       我的预借列表（取消链接 deletefb(fid, status)，status==0 才能取消）

# addfbcheckAjax 的返回码 -> (错误码, 学校原文提示)
PREBOOK_CHECK: dict[str, tuple[str, str]] = {
    "1": ("LIBRARY_ALREADY_PREBOOKED", "您已预借过这本书，不能重复提交请求"),
    "2": ("LIBRARY_NOT_PREBOOKABLE", "当前没有可预借图书"),
    "3": ("LIBRARY_NOT_PREBOOKABLE", "您所属的用户类型对于可预借的典藏地没有预借权，请到馆阅览"),
    "4": ("LIBRARY_PREBOOK_QUOTA_FULL", "您的预借数量已满，请到馆阅览"),
    "5": ("LIBRARY_PREBOOK_QUOTA_FULL", "您的预借数量已满，请到馆阅览"),
    "6": ("LIBRARY_PREBOOK_QUOTA_FULL", "书库预借量已满，请到馆阅览"),
    "7": ("NETWORK_ERROR", "图书馆系统查询超时，请稍后重试"),
}
PREBOOK_EXPRESS = "5"  # “顺丰快递到付”：要填收件地址，不支持（地址属于个人信息，也不该经过对话）


def parse_library_book(html: str, holdings_html: str | None = None) -> dict[str, Any]:
    """html：bookinfo.aspx；holdings_html：bookinfowithresvAjax.aspx 的返回（没有时从 html 里找馆藏表）。"""
    soup = BeautifulSoup(html, "html.parser")
    card = soup.find(id=re.compile(r"bookcardinfolbl$"))
    lines = [clean(x) for x in card.get_text("\n").split("\n")] if card else []
    lines = [x for x in lines if x]
    title = clean(re.split(r"[／/]", lines[0])[0]) if lines else None
    isbn = re.search(r"ISBN\s*([\d\-Xx]+)", card.get_text(" ") if card else "")
    ctrl = soup.find(id=re.compile(r"ctrlnolbl$"))

    copies = []
    tables = BeautifulSoup(holdings_html, "html.parser").find_all("table") if holdings_html else []
    table = next((t for t in [*tables, *soup.find_all("table")] if "索取号" in t.get_text() and "状态" in t.get_text()), None)
    for tr in table.find_all("tr") if table else []:
        tds = tr.find_all("td")
        if len(tds) < 9 or tr.find_parent("thead"):
            continue
        loc = tds[0].find("a") or tds[0]
        shelf = loc.find("font")
        shelf_text = clean(shelf.get_text()).strip("()（）") if shelf else None
        if shelf:
            shelf.extract()
        status = clean(tds[5].get_text())
        copies.append({
            "location": clean(loc.get_text()), "shelf": shelf_text or None, "callNumber": clean(tds[1].get_text()) or None,
            "barcode": clean(tds[2].get_text()) or None, "volume": clean(tds[3].get_text()) or None,
            "status": status, "onShelf": status.startswith("可供出借"),
            "dueDate": (m[0] if (m := re.search(r"\d{4}-\d{2}-\d{2}", status)) else None),
            "loanType": clean(tds[6].get_text()) or None, "reserveQueue": _num(clean(tds[7].get_text())),
        })
    on_shelf = sum(c["onShelf"] for c in copies)
    return {
        "ctrlno": (m[1] if ctrl and (m := re.search(r"(\d+)", ctrl.get_text())) else None),
        "title": title, "description": " / ".join(lines[:2]) or None, "isbn": isbn[1] if isbn else None,
        "copies": copies, "onShelf": on_shelf, "lent": len(copies) - on_shelf,
    }


def parse_prebook_form(html: str) -> dict[str, Any]:
    """预借登记页：书名、取书地点选项、预填的邮箱（只返回是否有，不返回内容）。"""
    soup = BeautifulSoup(html, "html.parser")
    info = soup.find(id="lblbookinfo")
    sel = soup.find("select", attrs={"name": "ddlfl"})
    pickups = []
    for o in sel.find_all("option") if sel else []:
        value = o.get("value", "")
        if value in ("", "-1", PREBOOK_EXPRESS):
            continue
        pickups.append({"value": value, "name": clean(o.get_text())})
    email = soup.find("input", attrs={"name": "txtemail"})
    return {
        "hasForm": sel is not None and soup.find("input", attrs={"name": "Button1"}) is not None,
        "bookInfo": clean(info.get_text()) if info else None,
        "pickups": pickups,
        "hasVolume": soup.find("select", attrs={"name": "ddlvolume"}) is not None,
        "prefilledEmail": (email.get("value") or "").strip() if email else "",
    }


def page_alerts(html: str) -> list[str]:
    """ASP.NET 老系统常用 <script>alert('…')</script> 返回提交结果。"""
    return [htmllib.unescape(m).replace("\\n", " ").strip() for m in re.findall(r"alert\(\s*['\"](.+?)['\"]\s*\)", html)]


def match_pickup(pickups: list[dict[str, str]], wanted: str | None) -> dict[str, str] | None:
    """用户说“北馆”“丽湖”“南区取书柜”时对应到具体取书点；对不上或有歧义返回 None。"""
    if not wanted:
        return None
    w = wanted.strip()
    exact = [p for p in pickups if p["value"] == w or p["name"] == w]
    if exact:
        return exact[0]
    aliases = {"南区": "致理楼", "取书柜": "取书柜", "南馆": "致理楼", "北馆": "汇典楼", "汇典": "汇典楼", "中央图书馆": "启明楼"}
    keys = [k for k in re.split(r"[\s,，、/]+", w) if k]
    keys += [v for k, v in aliases.items() if k in w]
    hits = [p for p in pickups if any(k in p["name"] for k in keys)]
    return hits[0] if len(hits) == 1 else None


def parse_my_prebooks(html: str) -> dict[str, Any]:
    """我的预借：每条记录的书目编号、取书点、提交时间、状态、能否取消（fid 只有能取消的记录才有）。"""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    table = next((t for t in soup.find_all("table") if "预借取书地点" in t.get_text()), None)
    for tr in table.find_all("tr") if table else []:
        tds = tr.find_all("td")
        if len(tds) < 7 or tr.find_parent("thead"):
            continue
        a = tds[1].find("a", href=True)
        cancel = re.search(r"deletefb\(\s*(\d+)\s*,\s*(\d+)\s*\)", str(tds[6]))
        items.append({
            "fid": cancel[1] if cancel else None,
            "ctrlno": (parse_qs(urlparse(a["href"]).query).get("ctrlno") or [None])[0] if a else None,
            "title": clean(tds[1].get_text()) or None, "volume": clean(tds[2].get_text()) or None,
            "pickup": clean(tds[3].get_text()) or None, "submittedAt": clean(tds[4].get_text()) or None,
            "status": clean(tds[5].get_text()) or None,
            "cancellable": bool(cancel and cancel[2] == "0"),
        })
    page = soup.find(id=re.compile(r"dplbl2$"))
    pages = soup.find(id=re.compile(r"gplbl2$"))
    return {"page": int(_num(clean(page.get_text())) or 1) if page else 1,
            "pages": int(_num(clean(pages.get_text())) or 1) if pages else 1, "items": items}


# ------------------------------------------------------------------ 研究生成绩（研究生院 szdxwdcjapp）
# 接口：2026-11-17 浏览器抓包；字段名参考 szu-score-helper，2026-11-17 真实账号自检（check grades）确认：
#   POST /gsapp/sys/szdxwdcjapp/modules/wdcj/xscjcx.do，EMAP 标准分页 → datas.xscjcx.{totalSize, rows}，一行 49 个字段
#   KCMC 课程名  XF 学分  CJ 成绩（可能是“优秀”“通过”）  DYBFZCJ 对应百分制成绩  JDZ 绩点  KCLBMC 课程类别
#   XNXQDM 学期代码（20251 = 2025-2026 学年第一学期）/ XNXQDM_DISPLAY “2025-2026学年 第一学期”  KSXZDM_DISPLAY 考试性质
#   BY1~BY10 是空的备用字段；XH 学号、LRRXM/CZRXM/FZJSXM 录入人/操作人/教师姓名，一律不取
# JDZ 实测有值；万一学校不给就是 None，不推算（不用“按 JDZ 条件反复筛选”的办法反推学校没显示的绩点）。

def parse_grade_page(body: Any) -> tuple[list[dict[str, Any]], int]:
    """一页 xscjcx.do 的响应 → (原始行, totalSize)。结构不对就报 PAGE_CHANGED，不返回半截数据。"""
    from .errors import CampusError

    data = (body.get("datas") or {}).get("xscjcx") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        code = body.get("code") if isinstance(body, dict) else None
        msg = str(body.get("msg") or "") if isinstance(body, dict) else ""
        raise CampusError("PAGE_CHANGED", f"成绩接口返回的结构和预期不一致（code={code}{'，' + msg if msg else ''}）")
    rows = [r for r in data["rows"] if isinstance(r, dict)]
    return rows, int(_num(data.get("totalSize")) or len(rows))


def normalize_grade(row: dict[str, Any]) -> dict[str, Any]:
    """白名单取字段：学号（XH）、姓名（XM）等身份字段在这里就丢掉。"""
    def text(key: str) -> str | None:
        v = row.get(key)
        return str(v).strip() or None if v is not None else None

    return {
        "termCode": text("XNXQDM"),
        "term": text("XNXQDM_DISPLAY") or text("XNXQDM"),
        "course": text("KCMC"),
        "credit": _num(row.get("XF")),
        "score": text("CJ") or text("DYBFZCJ"),  # 照学校显示的写法
        "scorePercent": _num(row.get("DYBFZCJ")) if _num(row.get("DYBFZCJ")) is not None else _num(row.get("CJ")),
        "gradePoint": _num(row.get("JDZ")),
        "category": text("KCLBMC"),
        "examType": text("KSXZDM_DISPLAY"),
    }


def term_key(text: str) -> str:
    """用户写的学期 → 便于匹配的形式：去空格；“2025-2026-1”转成学校的学期代码“20251”。"""
    text = re.sub(r"\s+", "", text)
    if m := re.fullmatch(r"(\d{4})-\d{4}-([12])", text):
        return m[1] + m[2]
    return text


def _grade_summary(courses: list[dict[str, Any]]) -> dict[str, Any]:
    """按学分加权。只用学校给了的数：没有百分制成绩的课（“通过”）不进平均分和 GPA。"""
    with_credit = [c for c in courses if c["credit"]]
    scored = [c for c in with_credit if c["scorePercent"] is not None]
    pointed = [c for c in with_credit if c["gradePoint"] is not None]

    def avg(rows: list[dict[str, Any]], key: str) -> float | None:
        total = sum(c["credit"] for c in rows)
        return round(sum(c["credit"] * c[key] for c in rows) / total, 2) if total else None

    # 只有一部分课有绩点时不给 GPA：拿半数课程算出来的数会被当成真的 GPA 报给用户
    missing = len([c for c in scored if c["gradePoint"] is None])
    return {"courseCount": len(courses), "credits": round(sum(c["credit"] for c in with_credit), 2),
            "weightedScore": avg(scored, "scorePercent"), "gpa": avg(pointed, "gradePoint") if not missing else None,
            "gradePointMissing": missing}


def build_grade_report(rows: list[dict[str, Any]], term: str | None = None) -> dict[str, Any]:
    """原始行 → 按学期分组的成绩单（新学期在前）+ 汇总。

    term：None = 全部；"latest" = 最近一个有成绩的学期；其他 = 学期名或代码的一部分（忽略空格）。
    overall 永远是全部课程的汇总（问“总加权”时不用再查一次）。"""
    courses = [normalize_grade(r) for r in rows]
    order: list[tuple[str, str]] = []
    for c in courses:
        key = (c["termCode"] or "", c["term"] or "未标学期")
        if key not in order:
            order.append(key)
    order.sort(key=lambda k: k[0], reverse=True)
    all_terms = [name for _, name in order]

    picked = order
    if term == "latest":
        picked = order[:1]
    elif term:
        want = term_key(term)
        picked = [k for k in order if want in k[0] or want in re.sub(r"\s+", "", k[1])]

    groups = []
    for code, name in picked:
        items = [c for c in courses if (c["termCode"] or "", c["term"] or "未标学期") == (code, name)]
        groups.append({"term": name, **_grade_summary(items),
                       "courses": [{k: c[k] for k in ("course", "credit", "score", "scorePercent", "gradePoint", "category", "examType")}
                                   for c in items]})
    report: dict[str, Any] = {"terms": groups, "overall": _grade_summary(courses), "availableTerms": all_terms}
    if term and not groups:
        report["hint"] = f"没有和“{term}”对应的学期，可选的学期见 availableTerms"
    return report


# ------------------------------------------------------------------ 研究生课表（研究生院 wdkbapp）
# 接口和字段来自学校公开的前端脚本（不登录也能读）：
#   https://ehall.szu.edu.cn/gsapp/sys/wdkbapp/modules/xskcb/xskcb.js、xskcbBS.js
#   kfdxnxqcx.do  开放查询的学期（第一项是页面默认学期）  rows: XNXQDM / XNXQDM_DISPLAY（另有 DM / MC）
#   xspkjgcx.do   排课结果（课表格子）{XNXQDM, XH: ""}  rows: KCMC 课程 BJMC 班级 JSXM 教师 JASMC 教室
#                 ZCMC 周次（“2-17周”） XQ 星期（1-7，7=周日） KSJCDM 开始节次 JCFADM 节次方案
#   xsjxrwcx.do   本学期的课程列表（含没排课的课）{XNXQDM, XH: ""} + 分页  rows: KCMC PKSJDD（上课时间地点，没排课为空）
# 学生看自己的课表时页面发的 XH 是空串（_reqXH 只在管理员带 ?reqXH= 打开时才有），学校按登录会话认人。

def gs_term_code(calendar_term_id: str) -> str:
    """校历的学期 id “2025-2026-1” → 研究生院系统的学期代码 “20251”。"""
    return term_key(calendar_term_id)


def emap_rows(body: Any, key: str, what: str) -> list[dict[str, Any]]:
    """EMAP 接口的 datas.<key>.rows；结构不对报 PAGE_CHANGED，不返回半截数据。"""
    from .errors import CampusError

    data = (body.get("datas") or {}).get(key) if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise CampusError("PAGE_CHANGED", f"{what}接口返回的结构和预期不一致（code={body.get('code') if isinstance(body, dict) else None}）")
    return [r for r in data["rows"] if isinstance(r, dict)]


def _hhmm(value: Any) -> str | None:
    """KSSJ / JSSJ → “08:30”。学校前端的 formatSj 把 830 补成 0830 再插冒号；已经带冒号的原样用。"""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        return text[:5].zfill(5)
    if re.fullmatch(r"\d{3,4}", text):
        text = text.zfill(4)
        return f"{text[:2]}:{text[2:]}"
    return None


def build_gs_courses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """xspkjgcx 的行 → 和本科课表一样的条目（courseName / teachers / weeksText / weekday / startSection / endSection / location）。

    页面把每条记录放进“开始节次 × 星期”的格子，再把上下内容相同的格子合并（mergeTable.mergeTableByRow），
    所以一节课可能是一节一行。这里按“同一门课、同一天、同样周次和教室”把连续的节次并成一段；
    行里如果另有结束节次（JSJCDM）也照用。"""
    groups: dict[tuple, set[int]] = {}
    times: dict[tuple, dict[int, str | None]] = {}
    for r in rows:
        start = _num(r.get("KSJCDM"))
        if start is None:
            continue
        end = _num(r.get("JSJCDM")) or start
        key = (str(r.get("KCMC") or ""), str(r.get("BJMC") or ""), _num(r.get("XQ")), str(r.get("ZCMC") or ""),
               str(r.get("JASMC") or ""), str(r.get("JSXM") or ""))
        groups.setdefault(key, set()).update(range(int(start), int(end) + 1))
        times.setdefault(key, {})[int(start)] = _hhmm(r.get("KSSJ"))
        times[key].setdefault(-int(end), _hhmm(r.get("JSSJ")))  # 负数键 = 这一节的下课时间
    items = []
    for (course, klass, weekday, weeks, room, teachers), periods in groups.items():
        ordered = sorted(periods)
        run = [ordered[0]]
        for p in ordered[1:] + [None]:
            if p is not None and p == run[-1] + 1:
                run.append(p)
                continue
            key = (course, klass, weekday, weeks, room, teachers)
            begin, finish = times[key].get(run[0]), times[key].get(-run[-1])
            items.append({"courseName": course or None, "className": klass or None,
                          "teachers": [t.strip() for t in re.split(r"[,，、]", teachers) if t.strip()],
                          "weeksText": weeks or None, "weekday": weekday, "startSection": run[0], "endSection": run[-1],
                          "time": f"{begin}-{finish}" if begin and finish else None, "location": room or None})
            if p is not None:
                run = [p]
    return sorted(items, key=lambda x: (x["weekday"] or 0, x["startSection"]))
