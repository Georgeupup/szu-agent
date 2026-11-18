"""畅课（lms.szu.edu.cn）：单点登录、请求参数、解析、学期 / 课程选择、隐私。

样例结构照 2026-11-18 用户浏览器抓包，课程名、老师、id 都换成了假的。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from szu_agent.campus import CampusError, SessionStore
from szu_agent.campus import lms
from szu_agent.campus.http import CampusHttp
from szu_agent.campus.service import RealCampus

from .conftest import mock_login


def course(cid, name, sem_id, sem_name, *, teaching=False):
    return {
        "id": cid, "name": name, "course_code": f"20262027{cid}", "is_instructor": teaching, "compulsory": True,
        "instructors": [{"avatar_big_url": "", "id": 6001, "name": "李老师"}],
        "instructor_assistants": [{"id": 100001, "name": "张三"}, {"id": 100002, "name": "王五"}] if teaching else [],
        "assistants": [], "department": {"id": 56, "name": "计算机与软件学院"}, "credit": None,
        "semester": {"id": sem_id, "name": sem_name}, "start_date": "2026-08-31", "end_date": None,
        "url": f"https://lms.szu.edu.cn:443/course/{cid}/index", "created_user": {"id": 58000},
    }


COURSES = [
    course(501, "计算机网络", 9, "2026-2027第一学期"),
    course(502, "大学计算机", 9, "2026-2027第一学期", teaching=True),
    course(401, "无线网络技术", 7, "2025-2026第一学期"),
]


def homework(hid, title, deadline, *, submitted, score=None, closed=False, makeup=False, start=None):
    return {
        "id": hid, "title": title, "type": "homework", "published": True, "deadline": deadline, "end_time": deadline,
        "start_time": start, "submitted": submitted, "score": score, "score_published": False, "is_closed": closed,
        "can_make_up_homework": makeup, "user_submit_count": 1 if submitted else 0, "submit_by_group": False,
        "teaching_unit_id": 401,
        "data": {"description": "<div>提交时<span style=\"font-size: 1.25em;\">前三页</span>单面打印。</div>",
                 "homework_type": "file_upload", "score_rule": "latest"},
        "uploads": [{"id": 9, "name": "作业1.pdf", "key": "99acdf", "created_by_id": 6001, "type": "document"}],
        "rubric_instance": {"id": 0, "rubric": {"created_by": {"id": 0, "name": None}}},
    }


HOMEWORK = {
    401: [homework(1, "作业1", "2025-10-26T15:59:00Z", submitted=True, score="91", closed=True),
          homework(2, "期末大作业", "2025-12-24T15:59:00Z", submitted=True, closed=True)],
    501: [homework(11, "实验1", "2026-10-12T15:59:00Z", submitted=False, closed=True),
          homework(12, "实验2", "2026-11-09T15:59:00Z", submitted=False, closed=True, makeup=True),
          homework(13, "实验3", "2026-11-20T15:59:00Z", submitted=False),
          homework(14, "实验4", "2026-12-01T15:59:00Z", submitted=False, start="2026-11-24T00:00:00Z")],
    # 老师 / 助教视角（2026-11-18 真实账号自检）：没有 submitted / score / deadline，只有全班人数
    502: [{"id": 21, "title": "实验3", "type": "homework", "published": True, "end_time": "2026-11-23T15:59:00Z",
           "start_time": None, "is_closed": False, "submitted_count": 73, "unsubmitted_count": 92, "scored_count": 0,
           "expected_submit_count": 165, "uploads": []},
          {"id": 20, "title": "实验2", "type": "homework", "published": True, "end_time": "2026-11-02T15:59:00Z",
           "start_time": None, "is_closed": True, "submitted_count": 157, "unsubmitted_count": 8, "scored_count": 157}],
}


def lms_server(seen, *, sso_ok=True):
    """未登录时接口 302 去 identity（Keycloak）→ authserver；带 CASTGC 时 authserver 签 ticket 一路跳回畅课。"""
    def handler(req: httpx.Request):
        url = req.url
        seen.append((req.method, url.host, url.path, req.content.decode() if req.content else "", dict(url.params)))
        if url.host == "lms.szu.edu.cn":
            if url.path == "/login" and "ticket" in url.params:
                return httpx.Response(302, headers={"location": "https://lms.szu.edu.cn/user/index",
                                                    "set-cookie": "session=V2-1-test; Path=/; Secure; HttpOnly"})
            if "session=V2-1-test" not in req.headers.get("cookie", ""):
                return httpx.Response(302, headers={"location": "https://identity.szu.edu.cn/auth/realms/szu/protocol/cas/login"
                                                                "?service=https%3A//lms.szu.edu.cn/login"})
            if url.path == "/user/index":
                return httpx.Response(200, text="<html>畅课</html>")
            if url.path == "/api/my-courses":
                return httpx.Response(200, json={"courses": COURSES, "page": 1, "pages": 1, "total": len(COURSES)})
            if url.path.endswith("/homework-activities"):
                cid = int(url.path.split("/")[3])
                return httpx.Response(200, json={"homework_activities": HOMEWORK.get(cid, []), "page": 1, "pages": 1})
        if url.host == "identity.szu.edu.cn":
            if "broker/cas-client/endpoint" in url.path:
                return httpx.Response(302, headers={"location": "https://lms.szu.edu.cn/login?ticket=ST-lms"})
            return httpx.Response(303, headers={"location": "https://authserver.szu.edu.cn/authserver/login?service="
                                                            "https%3A%2F%2Fidentity.szu.edu.cn%2Fauth%2Frealms%2Fszu%2Fbroker%2Fcas-client%2Fendpoint"})
        if url.host == "authserver.szu.edu.cn":
            if sso_ok and "MOCK_TGC" in req.headers.get("cookie", ""):
                return httpx.Response(302, headers={"location": "https://identity.szu.edu.cn/auth/realms/szu/broker/cas-client/endpoint?ticket=ST-1"})
            return httpx.Response(200, text="<html>统一身份认证 登录</html>")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def real(tmp_path, seen, **kw):
    store = SessionStore(tmp_path / "s.json")
    mock_login(store)
    return RealCampus(CampusHttp(store, transport=lms_server(seen, **kw)))


@pytest.fixture
def at_nov18(monkeypatch):
    from szu_agent.campus import booth
    monkeypatch.setattr(booth, "beijing_now", lambda: datetime(2026, 11, 18, 21, 0))


async def test_sso_then_requests_copied_from_browser(tmp_path, at_nov18):
    seen = []
    svc = real(tmp_path, seen)
    data = await svc.lms_homework(pending_only=True)

    hosts = [s[1] for s in seen]
    assert "identity.szu.edu.cn" in hosts and "authserver.szu.edu.cn" in hosts  # 走的是统一认证，不用另外登录
    courses_posts = [s for s in seen if s[2] == "/api/my-courses"]
    assert all(s[0] == "POST" for s in courses_posts)  # 302 不能把 POST 变成 GET
    body = json.loads(courses_posts[-1][3])
    assert body["fields"] == lms.COURSE_FIELDS and body["page"] == 1
    assert body["conditions"] == {"keyword": "", "classify_type": "recently_started", "course_tab": "student_list"}
    hw = [s for s in seen if s[2].endswith("/homework-activities")]
    assert [s[2] for s in hw] == ["/api/courses/501/homework-activities"]  # 只查当前学期、学生身份的课
    assert hw[0][4] == {"conditions": '{"itemsSortBy":{"predicate":"module","reverse":false}}',
                        "page": "1", "page_size": "20", "reloadPage": "false"}

    assert data["term"] == "2026-2027第一学期"
    assert [(p["title"], p["status"]) for p in data["pending"]] == [("实验2", "可补交"), ("实验3", "待交"), ("实验4", "未开始")]
    assert data["pending"][1]["deadline"] == "2026-11-20 23:59" and data["pending"][1]["hoursLeft"] == 51.0  # UTC → 北京时间
    assert data["counts"] == {"total": 4, "submitted": 0, "pending": 3, "missed": 1, "unscored": 0}
    assert "homework" not in data["courses"][0]

    # 第二次不再走登录（session Cookie 存下来了）
    seen.clear()
    svc.http.clear_cache()
    await svc.lms_courses()
    assert {s[1] for s in seen} == {"lms.szu.edu.cn"}


async def test_course_lookup_scores_and_privacy(tmp_path, at_nov18):
    svc = real(tmp_path, [])
    data = await svc.lms_homework(course="无线网络")  # 上学期的课：不填 term 时在全部学期里找
    assert data["term"] == "2025-2026第一学期"
    items = data["courses"][0]["homework"]
    assert [(h["title"], h["status"], h["score"]) for h in items] == [("作业1", "已交", "91"), ("期末大作业", "已交", None)]
    assert items[0]["note"] == "提交时 前三页 单面打印。" and items[0]["attachments"] == ["作业1.pdf"]
    assert data["counts"]["unscored"] == 1

    courses = await svc.lms_courses(term="all")
    dumped = json.dumps(courses, ensure_ascii=False) + json.dumps(data, ensure_ascii=False)
    assert "张三" not in dumped and "王五" not in dumped and "100001" not in dumped  # 助教名单（可能是用户本人）不取
    assert "99acdf" not in dumped  # 附件的下载 key 不给
    assert courses["courses"][0]["teachers"] == ["李老师"]
    teaching = next(c for c in courses["courses"] if c["name"] == "大学计算机")
    assert teaching["role"] == "teaching" and teaching["url"] == "https://lms.szu.edu.cn/course/502/index"

    # 点名要查当老师 / 助教的课：给全班人数，不套“交没交”，也不算进 counts / pending
    ta = await svc.lms_homework(course="大学计算机")
    group = ta["courses"][0]
    assert group["role"] == "teaching" and ta["pending"] == [] and ta["counts"]["total"] == 0
    assert group["homework"] == [
        {"id": 21, "title": "实验3", "status": "进行中", "deadline": "2026-11-23 23:59",
         "submittedCount": 73, "unsubmittedCount": 92, "scoredCount": 0},
        {"id": 20, "title": "实验2", "status": "已截止", "deadline": "2026-11-02 23:59",
         "submittedCount": 157, "unsubmittedCount": 8, "scoredCount": 157}]


async def test_term_with_only_teaching_courses_says_so(tmp_path, at_nov18, monkeypatch):
    # 2026-11-18 真实账号：这学期畅课上只有一门当助教的课
    monkeypatch.setattr(lms, "normalize_course", lambda c, _orig=lms.normalize_course: {
        **_orig(c), "role": "teaching" if c["id"] in (501, 502) else "student"})
    data = await real(tmp_path, []).lms_homework(pending_only=True)
    assert data["courses"] == [] and data["pending"] == []
    assert "没有你以学生身份上的课" in data["note"] and "计算机网络、大学计算机" in data["note"]


async def test_term_selection(tmp_path, at_nov18):
    svc = real(tmp_path, [])
    assert [c["name"] for c in (await svc.lms_courses())["courses"]] == ["计算机网络", "大学计算机"]
    for spelled in ("2025-2026-1", "2025-2026第一学期", "20251"):
        assert [c["name"] for c in (await svc.lms_courses(spelled))["courses"]] == ["无线网络技术"], spelled
    missing = await svc.lms_courses("2019-2020-2")
    assert missing["courses"] == [] and "availableTerms" in missing["hint"]
    assert missing["availableTerms"] == ["2026-2027第一学期", "2025-2026第一学期"]
    with pytest.raises(CampusError) as e:
        await svc.lms_homework(course="量子力学")
    assert e.value.code == "NOT_FOUND" and e.value.details["courses"] == ["计算机网络", "大学计算机"]


async def test_expired_login_is_login_required(tmp_path):
    svc = real(tmp_path, [], sso_ok=False)
    with pytest.raises(CampusError) as e:
        await svc.lms_courses()
    assert e.value.code == "LOGIN_REQUIRED"


def test_term_key_and_status():
    assert lms.lms_term_key("2026-2027第一学期") == "20261"
    assert lms.lms_term_key("2025-2026学年 第二学期") == "20252"
    assert lms.lms_term_key("暑期") is None
    now = datetime(2026, 11, 18, 12, 0)
    h = lms.normalize_homework(homework(1, "x", "2026-11-18T03:59:00Z", submitted=False), now)  # 北京 11:59，刚过
    assert h["status"] == "已截止未交"
    with pytest.raises(CampusError):
        lms._page({"error": "x"}, "courses", "我的课程")


async def test_mock_lms_follows_today(monkeypatch, logged_in):
    from szu_agent.campus.mock import MockCampus

    monkeypatch.setenv("SZU_AGENT_MOCK_TODAY", "2027-01-05")
    data = await MockCampus(logged_in).lms_homework(pending_only=True)
    due = [p for p in data["pending"] if p["status"] == "待交"]
    assert due and all(p["deadline"] > "2027-01-05" and p["hoursLeft"] > 0 for p in due)  # 演示站一直有待交作业
    assert date.fromisoformat(data["now"][:10]) == date(2027, 1, 5)
