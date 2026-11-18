"""畅课（TronClass，lms.szu.edu.cn）：我的课程、每门课的作业（交没交、分数、截止时间）。

登录：不用另写。lms 未登录时 302 到 identity.szu.edu.cn（Keycloak，client_id=Tronclass），
它再把人转给 authserver（统一认证）——带着 CASTGC 就能一路自动跳回来（2026-11-18 容器里实测跳转链）。
登录后 lms 发 session（HttpOnly）和 role_token 两个 Cookie，有效期 1 天。

接口（2026-11-18 用户浏览器抓包）：
  POST /api/my-courses              JSON {page, page_size, fields, conditions}，返回 {courses, page, pages, total}
  GET  /api/courses/{id}/homework-activities?conditions={"itemsSortBy":...}&page=1&page_size=20&reloadPage=false
                                    返回 {homework_activities, page, pages, total}；分数在每条作业的 score（字符串，未评分为 null）
  （/api/notifications 是右上角铃铛通知；助教账号收到的是“有学生交了作业”，不是自己的作业，不用）

未验证（UNVERIFIED，`szu-agent check lms` 会检查）：浏览器请求 my-courses 时 conditions 里带了学年 / 学期的内部编号
（academic_year_id=[5]、semester_id=[9]），编号从哪个接口来还没抓到；这里不带这两项，拿全部课程后按每门课自带的
semester.name（如“2026-2027第一学期”）自己筛学期。

隐私：课程里的 instructor_assistants / assistants（助教名单，可能就是用户本人）在解析层丢掉；只保留授课教师姓名
（和课表里的“老师”一样）。用户在畅课里的内部 id（Cookie / role_token 里的 uid）不用、不存、不返回。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .errors import CampusError

LMS = "https://lms.szu.edu.cn"
LMS_HOME = f"{LMS}/user/index"
CST = timezone(timedelta(hours=8))

# 照抄浏览器的 fields（决定返回哪些字段）
COURSE_FIELDS = (
    "id,name,course_code,cover,compulsory,is_instructor,url,instructors(id,name,avatar_big_url),department(id,name),"
    "grade(id,name),klass(id,name),assistants(id,name),instructor_assistants(id,name),course_type,credit,"
    "course_attributes(teaching_class_name,student_count,audience_type),audit_status,user_stick_course_record,"
    "imported_from,start_date,end_date,public_scope,semester(id,name),is_mute,can_withdraw_course,created_user(id)"
)
# 浏览器的 conditions 去掉 academic_year_id / semester_id（见模块说明）
COURSE_CONDITIONS = {"keyword": "", "classify_type": "recently_started", "course_tab": "student_list"}
COURSE_PAGE_SIZE = 50   # 浏览器是 10
HOMEWORK_CONDITIONS = json.dumps({"itemsSortBy": {"predicate": "module", "reverse": False}}, separators=(",", ":"))
HOMEWORK_PAGE_SIZE = 20  # 照抄浏览器
MAX_PAGES = 10
REDIRECTS = (301, 302, 303, 307, 308)
_CN_NUM = {"一": "1", "二": "2", "三": "3"}


# ------------------------------------------------------------------ HTTP

async def enter(http: Any) -> None:
    """打开畅课首页，走一遍单点登录（lms → identity → authserver → 回来），拿到 lms 的 session Cookie。"""
    from .debuglog import where

    resp = await http.request("GET", LMS_HOME)
    if resp.url.host != "lms.szu.edu.cn":
        raise CampusError("PAGE_CHANGED", f"畅课登录没有跳回畅课（停在 {where(resp.url)}）", url=LMS_HOME)


async def api(http: Any, method: str, path: str, **kw: Any) -> Any:
    """调用畅课接口。不自动跟随跳转：没有 session 时接口会 302 去登录，
    httpx 跟随 302 会把 POST 改成 GET，最后拿到的不是我们要的结果；所以看到跳转就先 enter() 再重发一次。"""
    headers = {"Accept": "application/json, text/plain, */*", "Referer": LMS_HOME}
    for attempt in (0, 1):
        try:
            resp = await http.request(method, f"{LMS}{path}", follow_redirects=False, cache=True, headers=headers, **kw)
        except CampusError as exc:
            if exc.code != "PERMISSION_DENIED" or exc.details.get("status") != 401 or attempt:
                raise
            await enter(http)
            continue
        if resp.status_code in REDIRECTS:
            if attempt:
                break
            await enter(http)
            continue
        try:
            return resp.json()
        except ValueError as exc:
            raise CampusError("PAGE_CHANGED", f"畅课接口 {path.split('?')[0]} 没有返回 JSON", url=LMS_HOME) from exc
    raise CampusError("PAGE_CHANGED", "登录畅课后接口仍然要求登录", url=LMS_HOME)


async def fetch_courses(http: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        body = await api(http, "POST", "/api/my-courses", json={
            "page": page, "page_size": COURSE_PAGE_SIZE, "fields": COURSE_FIELDS, "conditions": COURSE_CONDITIONS})
        chunk, pages = _page(body, "courses", "我的课程")
        rows += chunk
        if page >= pages or not chunk:
            break
    return rows


async def fetch_homework(http: Any, course_id: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        body = await api(http, "GET", f"/api/courses/{int(course_id)}/homework-activities", params={
            "conditions": HOMEWORK_CONDITIONS, "page": str(page), "page_size": str(HOMEWORK_PAGE_SIZE), "reloadPage": "false"})
        chunk, pages = _page(body, "homework_activities", "作业列表")
        rows += chunk
        if page >= pages or not chunk:
            break
    return rows


def _page(body: Any, key: str, what: str) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(body, dict) or not isinstance(body.get(key), list):
        raise CampusError("PAGE_CHANGED", f"畅课{what}接口返回的结构和预期不一致", url=LMS_HOME)
    try:
        pages = int(body.get("pages") or 1)
    except (TypeError, ValueError):
        pages = 1
    return [r for r in body[key] if isinstance(r, dict)], pages


# ------------------------------------------------------------------ 解析（纯函数）

def lms_term_key(name: str | None) -> str | None:
    """畅课学期名“2026-2027第一学期” → “20261”（和 term_key("2026-2027-1")、研究生院学期代码同一种写法）。"""
    m = re.search(r"(\d{4})\s*-\s*\d{4}\D*?第?([一二三123])\s*学期", name or "")
    return m[1] + _CN_NUM.get(m[2], m[2]) if m else None


def normalize_course(c: dict[str, Any]) -> dict[str, Any]:
    """白名单取字段。助教名单（instructor_assistants / assistants）不取：里面可能就是用户本人。"""
    sem = c.get("semester") if isinstance(c.get("semester"), dict) else {}
    dept = c.get("department") if isinstance(c.get("department"), dict) else {}
    return {
        "id": c.get("id"),
        "name": str(c.get("name") or "").strip(),
        "term": sem.get("name"),
        "termKey": lms_term_key(sem.get("name")),
        # is_instructor=true：这门课里是老师或助教（抓包里助教账号就是 true），作业列表看到的不是“我的作业”
        "role": "teaching" if c.get("is_instructor") else "student",
        "teachers": [str(t.get("name")) for t in c.get("instructors") or [] if isinstance(t, dict) and t.get("name")],
        "department": dept.get("name"),
        "credit": c.get("credit"),
        "startDate": c.get("start_date"),
        "url": str(c.get("url") or f"{LMS}/course/{c.get('id')}/index").replace(":443/", "/"),
    }


def to_beijing(ts: str | None) -> datetime | None:
    """接口时间是 UTC（“2025-10-26T15:59:00Z”）→ 北京时间（无时区）。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST).replace(tzinfo=None)


def _text(html: str | None, limit: int = 150) -> str | None:
    if not html:
        return None
    from bs4 import BeautifulSoup

    text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).strip()
    return (text[:limit] + "…" if len(text) > limit else text) or None


def normalize_homework(a: dict[str, Any], now: datetime) -> dict[str, Any]:
    deadline = to_beijing(a.get("deadline") or a.get("end_time"))
    start = to_beijing(a.get("start_time"))
    submitted = bool(a.get("submitted"))
    closed = bool(a.get("is_closed")) or (deadline is not None and deadline < now)
    if submitted:
        status = "已交"
    elif start and start > now:
        status = "未开始"
    elif closed:
        status = "可补交" if a.get("can_make_up_homework") else "已截止未交"
    else:
        status = "待交"
    score = a.get("score")
    data = a.get("data") if isinstance(a.get("data"), dict) else {}
    return {
        "id": a.get("id"),
        "title": str(a.get("title") or "").strip(),
        "status": status,
        "score": str(score) if score not in (None, "") else None,  # 照网页显示；null = 未评分
        "deadline": deadline.strftime("%Y-%m-%d %H:%M") if deadline else None,
        "startTime": start.strftime("%Y-%m-%d %H:%M") if start else None,
        "submitCount": a.get("user_submit_count"),
        "groupWork": bool(a.get("submit_by_group")),
        "note": _text(data.get("description")),
        "attachments": [str(u.get("name")) for u in a.get("uploads") or [] if isinstance(u, dict) and u.get("name")],
        "_deadline": deadline,
    }


# ------------------------------------------------------------------ 选课程 / 学期

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def term_list(courses: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """[(termKey, 学期名)]，新学期在前。"""
    seen: dict[str, str] = {}
    for c in courses:
        if c["term"]:
            seen.setdefault(c["termKey"] or c["term"], c["term"])
    return sorted(seen.items(), key=lambda kv: kv[0], reverse=True)


def current_term_key(courses: list[dict[str, Any]], today: date) -> str | None:
    """校历上今天所在（假期里是下一个）学期，畅课里有这个学期的课就用它；否则用课程里最新的学期。"""
    from ..knowledge.calendar import default_calendar
    from .parsers import term_key

    terms = term_list(courses)
    cal = default_calendar().relevant_term(today)
    if cal and any(k == term_key(cal.id) for k, _ in terms):
        return term_key(cal.id)
    return terms[0][0] if terms else None


def pick_term(courses: list[dict[str, Any]], term: str | None, today: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """term：None = 当前学期；“all” = 全部；其他 = “2026-2027-1”“20261”或学期名的一部分。"""
    from .parsers import term_key

    terms = term_list(courses)
    meta: dict[str, Any] = {"availableTerms": [n for _, n in terms]}
    if term == "all":
        meta["term"] = "全部学期"
        return courses, meta
    if term:
        want = term_key(term)
        keys = [k for k, n in terms if want == k or want in _norm(n)]
    else:
        keys = [k] if (k := current_term_key(courses, today)) else []
    if not keys:
        meta["term"] = None
        if term:
            meta["hint"] = f"畅课里没有“{term}”的课程，可选的学期见 availableTerms"
        return [], meta
    meta["term"] = dict(terms)[keys[0]]
    return [c for c in courses if (c["termKey"] or c["term"]) in keys], meta


def find_courses(courses: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """课程名的一部分（忽略空格大小写）或课程 id；多个学期都有同名课时只取最新学期的。"""
    q = _norm(query)
    hits = [c for c in courses if str(c["id"]) == query.strip() or (q and q in _norm(c["name"]))]
    if not hits:
        return []
    latest = max(c["termKey"] or "" for c in hits)
    return [c for c in hits if (c["termKey"] or "") == latest]


def build_homework_report(picked: list[dict[str, Any]], homework: dict[Any, list[dict[str, Any]]],
                          now: datetime, *, pending_only: bool = False) -> dict[str, Any]:
    groups, pending = [], []
    counts = {"total": 0, "submitted": 0, "pending": 0, "missed": 0, "unscored": 0}
    for c in picked:
        items = [normalize_homework(a, now) for a in homework.get(c["id"], []) if a.get("published", True)]
        for h in items:
            counts["total"] += 1
            counts["submitted"] += h["status"] == "已交"
            counts["missed"] += h["status"] == "已截止未交"
            counts["unscored"] += h["status"] == "已交" and h["score"] is None
            if h["status"] in ("待交", "可补交", "未开始"):
                counts["pending"] += 1
                left = h["_deadline"] - now if h["_deadline"] else None
                pending.append({"course": c["name"], "title": h["title"], "status": h["status"], "deadline": h["deadline"],
                                "hoursLeft": round(left.total_seconds() / 3600, 1) if left and left.total_seconds() > 0 else None,
                                "note": h["note"], "_deadline": h["_deadline"]})
        group: dict[str, Any] = {"course": c["name"], "term": c["term"], "url": f"{LMS}/course/{c['id']}/homework"}
        if c["role"] == "teaching":
            group["role"] = "teaching"
        if not pending_only:
            group["homework"] = [{k: v for k, v in h.items() if not k.startswith("_")} for h in items]
        groups.append(group)
    pending.sort(key=lambda p: p["_deadline"] or datetime.max)
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "counts": counts,
        "pending": [{k: v for k, v in p.items() if not k.startswith("_")} for p in pending],
        "courses": groups,
    }


# ------------------------------------------------------------------ 对外（RealCampus / MockCampus 共用）

async def courses_report(load_courses, term: str | None, today: date) -> dict[str, Any]:
    courses = [normalize_course(c) for c in await load_courses()]
    picked, meta = pick_term(courses, term, today)
    return {**meta, "courses": picked, "url": LMS_HOME}


async def homework_report(load_courses, load_homework, *, course: str | None, term: str | None,
                          pending_only: bool, now: datetime) -> dict[str, Any]:
    """course 不填：这个学期（term）所有“我是学生”的课；填了：按课程名找（term 不填时在全部学期里找，取最新学期的）。"""
    courses = [normalize_course(c) for c in await load_courses()]
    if course:
        pool, meta = pick_term(courses, term, now.date()) if term else (courses, {"availableTerms": [n for _, n in term_list(courses)]})
        picked = find_courses(pool, course)
        if not picked:
            current, _ = pick_term(courses, None, now.date())
            raise CampusError("NOT_FOUND", f"畅课里没有找到课程“{course}”", courses=[c["name"] for c in current][:20],
                              availableTerms=meta.get("availableTerms"))
        meta["term"] = picked[0]["term"]
    else:
        picked, meta = pick_term(courses, term, now.date())
        picked = [c for c in picked if c["role"] == "student"]
    lists = await asyncio.gather(*(load_homework(c["id"]) for c in picked))
    report = build_homework_report(picked, {c["id"]: rows for c, rows in zip(picked, lists)}, now, pending_only=pending_only)
    return {**meta, **report, "url": LMS_HOME}
