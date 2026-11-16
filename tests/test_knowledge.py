"""校园资料库：数据文件校验、检索评测集、工具输出。"""

from datetime import date, timedelta
from pathlib import Path

import yaml

from szu_agent.campus import SessionStore
from szu_agent.knowledge import KnowledgeBase, default_kb, tokenize

from .test_mcp_server import mcp_client

EVAL = Path(__file__).parent / "fixtures" / "knowledge_eval.yaml"


def test_tokenize_uses_cjk_bigrams_and_keeps_numbers():
    assert tokenize("山楂树 204") == ["山楂", "楂树", "204"]
    assert tokenize("Room-A") == ["room", "a"]


def test_data_files_are_valid():
    kb = default_kb()
    assert len(kb.entries) >= 40
    for src in kb.sources.values():
        assert src["url"].startswith("https://") and src["title"]
    for e in kb.entries:
        assert e.name and e.data.get("rules"), e.id
        assert e.campus in {"粤海", "丽湖", "全校"}, e.id
        assert e.capacity is None or isinstance(e.capacity, int), e.id
        booking = e.data.get("booking") or {}
        assert booking.get("url") or booking.get("url_offcampus"), f"{e.id} 没有申请入口"


def test_retrieval_eval_set():
    """评测集命中率。低于阈值说明数据或检索算法退化了——先看打印出来的失败用例。"""
    kb = default_kb()
    cases = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    failures = []
    for c in cases:
        ids = [e.id for e, _ in kb.search(c["query"], campus=c.get("campus"), min_capacity=c.get("min_capacity"))]
        ok = not ids if not c["expect"] else bool(set(ids) & set(c["expect"]))
        if not ok:
            failures.append(f"{c['query']} → {ids}")
    rate = 1 - len(failures) / len(cases)
    print(f"\n资料库检索命中率 {rate:.0%}（{len(cases) - len(failures)}/{len(cases)}）", *failures, sep="\n  ")
    assert rate >= 0.9, failures
    # “资料库里没有”的问题必须返回空：宁可说不知道，也不拿不相关的条目硬答
    for c in cases:
        if not c["expect"]:
            assert not kb.search(c["query"]), c["query"]


def test_filters():
    kb = default_kb()
    big = kb.search("", campus="丽湖校区", min_capacity=30, limit=20)
    assert big and all(e.campus in {"丽湖", "全校"} and e.capacity >= 30 for e, _ in big)
    assert {e.id for e, _ in big} == {"lh-huyanglin-214", "lh-shanzhashu-209", "lh-qinggangli-301"}


def test_stale_and_duplicate_ids(tmp_path):
    doc = {"source": {"id": "s", "title": "t", "url": "https://x"}, "verified_at": date(2020, 1, 1),
           "entries": [{"id": "a", "name": "甲", "rules": "r"}]}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    kb = KnowledgeBase.load(tmp_path)
    assert kb.is_stale(kb.entries[0])
    assert not kb.is_stale(kb.entries[0], today=date(2020, 1, 1) + timedelta(days=30))

    (tmp_path / "b.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    try:
        KnowledgeBase.load(tmp_path)
    except ValueError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复 id 应该报错")


async def test_campus_guide_tool(home):
    # 资料库不访问学校系统：没登录也能用
    async with mcp_client(SessionStore(mock=True)) as client:
        res = await client("campus_guide", query="会议", campus="丽湖", min_capacity=30)
        assert res["ok"]
        names = [e["name"] for e in res["data"]["entries"]]
        assert "丽湖山楂树活动室 209" in names
        e = res["data"]["entries"][0]
        assert e["booking"]["url_offcampus"].startswith("https://swzx.webvpn.szu.edu.cn")
        assert res["data"]["sources"][0]["verifiedAt"]

        booth = await client("campus_guide", query="静音舱")
        assert booth["data"]["entries"][0]["name"] == "网络面试间"
        assert "提前两小时" in booth["data"]["entries"][0]["rules"]
        heart = await client("campus_guide", query="心灵小屋")
        assert "不要猜" in heart["data"]["entries"][0]["note"]  # 没录入规则的条目带着“不要猜”的提示

        none = await client("campus_guide", query="健身房")
        assert none["data"]["entries"] == [] and "不知道" in none["data"]["hint"]


# ------------------------------ 校历 ------------------------------ #

def test_calendar_weeks_match_official_table():
    """对照校历表图片里的行：每行周日到周六，行首是周次。"""
    from szu_agent.knowledge.calendar import default_calendar

    cal = default_calendar()
    week = lambda d: cal.phase(date.fromisoformat(d)).get("teachingWeek")  # noqa: E731
    # 2025-2026-2：3/8（周日）、3/9（周一开始上课）同属第一周；7/5-7/11 第十八周；7/12-7/17 第十九周
    assert week("2026-03-08") == week("2026-03-14") == 1
    assert week("2026-03-07") is None  # 报到、缓考那几天表里没有周次
    assert week("2026-07-05") == 18 and week("2026-07-17") == 19
    # 2026-2027-1：8/30 第一周；11/15 第十二周；1/3-1/9 第十九周；1/10-1/16 第二十周；1/17 之后不标
    assert week("2026-08-30") == 1 and week("2026-11-15") == 12
    assert week("2027-01-03") == 19 and week("2027-01-16") == 20
    assert week("2027-01-20") is None
    assert cal.phase(date(2027, 2, 1))["phase"] == "寒假"
    assert cal.phase(date(2026, 8, 1))["phase"] == "暑假"
    assert cal.label(date(2026, 11, 14)) == "第11教学周" and cal.label(date(2026, 8, 1)) == "暑假"
    assert cal.label(date(2030, 1, 1)) is None


def test_calendar_events_are_inside_reasonable_range():
    from szu_agent.knowledge.calendar import default_calendar

    for t in default_calendar().terms:
        assert t.week1_sunday.weekday() == 6
        last_saturday = t.week1_sunday + timedelta(weeks=t.weeks) - timedelta(days=1)
        assert t.start <= t.week1_sunday + timedelta(days=1) and last_saturday <= t.end + timedelta(days=1), t.id
        for ev in t.raw["events"]:
            s, e = ev.get("start"), ev.get("end")
            assert s or e or ev.get("note"), ev
            assert not (s and e) or s <= e, ev
            for d in (s, e):  # 缓考可能在学期开始前几天
                assert d is None or t.start - timedelta(days=7) <= d <= t.end, ev


async def test_academic_calendar_tool(home):
    async with mcp_client(SessionStore(mock=True)) as client:
        res = await client("academic_calendar", date="2026-11-18", week=15, with_periods=True)
        d = res["data"]
        assert d["day"]["teachingWeek"] == 12 and d["day"]["weekday"] == "周三"
        assert d["term"]["name"] == "2026-2027 学年第一学期"
        exam = next(e for e in d["term"]["events"] if e["name"] == "本科生期末考试")
        assert exam["from"] == "2027-01-04" and exam["status"] == "还有 47 天"
        assert d["week"]["days"]["周日"] == "2026-12-06" and d["week"]["days"]["周三"] == "2026-12-09"
        assert d["periods"]["times"][2] == {"period": 3, "from": "10:15", "to": "10:55"}

        summer = (await client("academic_calendar", date="2026-08-01"))["data"]
        assert summer["day"]["phase"] == "暑假" and summer["term"]["name"] == "2026-2027 学年第一学期"  # 假期里看下学期

        future = (await client("academic_calendar", date="2027-03-10"))["data"]
        assert "term" not in future and "hint" in future
        assert "periodsHint" in (await client("academic_calendar", with_periods=True, campus="丽湖"))["data"]


# ------------------------------ 规章细则库（奖学金评选细则） ------------------------------ #

POLICY_EVAL = Path(__file__).parent / "fixtures" / "policy_eval.yaml"


def test_policy_docs_are_valid_and_redacted():
    import re

    from szu_agent.knowledge.policy import default_policy

    kb = default_policy("scholarship")
    assert len(kb.docs) >= 100
    for d in kb.docs:
        assert d.title and d.publisher and d.meta.get("source", "").startswith("https://"), d.id
        assert d.meta.get("level") in {"本科生", "研究生", "博士生", "不限"}, d.id
        text = d.path.read_text(encoding="utf-8")
        assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text), f"{d.id} 里有手机号"
        assert not re.search(r"(?<!\d)20\d{8}(?!\d)", text), f"{d.id} 里有像学号的数字"
    assert max(len(c.text) for c in kb.chunks) <= 1800


def test_policy_retrieval_eval_set():
    """评测集命中率：前 5 段里要有期望文档的期望原文。低于阈值先看打印出来的失败用例，再决定改数据、改切段还是上向量。"""
    from szu_agent.knowledge.policy import default_policy

    kb = default_policy("scholarship")
    cases = yaml.safe_load(POLICY_EVAL.read_text(encoding="utf-8"))
    failures = []
    for c in cases:
        hits = kb.search(c["query"], college=c.get("college"), level=c.get("level"), limit=5)
        if not any(c["doc"] in ch.doc.id and c["text"] in ch.text for ch, _ in hits):
            failures.append((c["query"], [ch.doc.id for ch, _ in hits[:3]]))
    print("\n".join(f"未命中：{q} → {ids}" for q, ids in failures))
    assert len(failures) <= 1, failures


def test_match_college_does_not_confuse_similar_names():
    from szu_agent.knowledge.policy import default_policy, match_college

    pubs = default_policy("scholarship").publishers
    assert match_college("管理学院", pubs) == {"管理学院"}
    assert match_college("政府管理学院", pubs) == {"政府管理学院"}
    assert match_college("计软", pubs) == {"计算机与软件学院"}
    assert match_college("计算机学院", pubs) == {"计算机与软件学院"}
    assert match_college("不存在学院", pubs) == set()


def test_table_split_repeats_header():
    from szu_agent.knowledge.policy import MAX_CHARS, _split_long

    rows = [f"| 项目{i} | {'很长的说明' * 20} |" for i in range(40)]
    pieces = _split_long(["| 项目 | 说明 |", "| --- | --- |", *rows])
    assert len(pieces) > 1
    assert all(p[0] == "| 项目 | 说明 |" and p[1] == "| --- | --- |" for p in pieces)
    assert all(sum(len(x) for x in p) <= MAX_CHARS + 200 for p in pieces)


async def test_policy_search_tool():
    async with mcp_client(SessionStore(mock=True)) as client:
        res = (await client("policy_search", query="卓越班 一等奖 金额", college="计软"))["data"]
        assert res["colleges"] == ["计算机与软件学院"]
        assert "12000" in res["chunks"][0]["text"] and res["sources"][0]["url"].startswith("https://")
        listing = (await client("policy_search", college="计算机与软件学院", level="本科生"))["data"]
        assert listing["documents"] and all(d["level"] in {"本科生", "不限"} for d in listing["documents"])
        unknown = (await client("policy_search", query="奖学金", college="魔法学院"))["data"]
        assert unknown["chunks"] == [] and "管理学院" in unknown["colleges"]
        no_college = (await client("policy_search", query="国家励志奖学金 申请条件"))["data"]
        assert "hint" in no_college
