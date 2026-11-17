"""校园导航：地图转录 + 坐标拟合、地点解析、路线建议、通勤班车时刻、小巴实时位置。"""

import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from szu_agent.campus import CampusError, SessionStore
from szu_agent.campus import shuttle
from szu_agent.knowledge.campusmap import HERE, CampusMap, MapFit, default_map

from .test_mcp_server import mcp_client


# ------------------------------ 数据和拟合 ------------------------------ #

def test_fit_is_exact_on_affine_data_and_snaps_to_anchors():
    # 纯仿射关系（旋转 30° + 缩放 1.5）应该被完全还原；控制点本身经过残差修正后必须精确
    rot = math.radians(30)
    f = lambda x, y: (1.5 * (x * math.cos(rot) - y * math.sin(rot)) + 10, 1.5 * (x * math.sin(rot) + y * math.cos(rot)) - 5)
    pts = [(0, 0), (100, 0), (0, 100), (80, 90), (40, 10)]
    fit = MapFit([(p, f(*p)) for p in pts])
    for p in [(50, 50), (300, -20)]:
        assert math.dist(fit(p), f(*p)) < 1e-6
    noisy = MapFit([(p, (f(*p)[0] + (7 if i == 3 else 0), f(*p)[1])) for i, p in enumerate(pts)])
    assert math.dist(noisy((80, 90)), (f(80, 90)[0] + 7, f(80, 90)[1])) < 1e-6


def test_map_transcription_fits_the_real_stop_coordinates():
    """留一法：每次拿掉一个小巴站图标去拟合，看它被推算到离真实位置多远。
    转录时把某个站图标的位置抄错（或站名对错号），这里的误差会明显变大。"""
    raw = yaml.safe_load((HERE / "campus_map.yaml").read_text(encoding="utf-8"))
    m = default_map()
    for campus, limit in (("粤海", 60), ("丽湖", 80)):
        anchors = raw["campuses"][campus]["anchors"]
        loop = m.loops[campus]
        errs = []
        for out in anchors:
            rest = [(tuple(xy), (loop.stop(n).e, loop.stop(n).n)) for n, xy in anchors.items() if n != out]
            guess = MapFit(rest)(tuple(anchors[out]))
            errs.append(math.dist(guess, (loop.stop(out).e, loop.stop(out).n)))
        assert sum(errs) / len(errs) < limit, (campus, [round(e) for e in errs])
        assert max(errs) < 160, (campus, [round(e) for e in errs])


def test_places_are_inside_campus_and_shuttle_data_is_consistent():
    m = default_map()
    for p in m.places:
        assert math.hypot(p.e, p.n) < 1600, p.name  # 校区中心 1.6 公里以内
    for campus, loop in m.loops.items():
        assert len(loop.stops) >= 10 and 3000 < loop.length < 6000
        assert [s.s for s in loop.stops] == sorted(s.s for s in loop.stops)  # 站点沿折线顺序排列
        a, b = loop.stops[0], loop.stops[3]
        assert loop.ahead(a.s, b.s, "clockwise") + loop.ahead(a.s, b.s, "counterclockwise") == pytest.approx(loop.length)
    # 站点按列表顺序：粤海是逆时针，丽湖是顺时针（有向面积算出来的，数据变了要重新核对方向含义）
    assert m.loops["粤海"].order == "counterclockwise" and m.loops["丽湖"].order == "clockwise"
    assert {s["id"] for s in m.intercampus["stops"]} == set(m.boarding)


# ------------------------------ 地点解析 ------------------------------ #

def test_resolve_names_aliases_and_ambiguity():
    m = default_map()
    assert m.resolve("汇典楼")[0].name == "汇典楼"
    assert m.resolve("图书馆北馆")[0].name == "汇典楼"            # 地图括号里的说明作为别名
    assert m.resolve("经济学院")[0].name == "明德楼"
    assert m.resolve("丽湖校医院")[0].campus == "丽湖"            # 开头的校区当作条件
    assert m.resolve("北门")[0].kind == "小巴站"                  # 只在小巴接口里出现的站名也能找到
    p, cands = m.resolve("校医院")                               # 两个校区都有：不替用户选
    assert p is None and {c.campus for c in cands} == {"粤海", "丽湖"}
    p, cands = m.resolve("图书馆", "粤海")
    assert p is None and {c.name for c in cands} == {"汇典楼", "汇智楼"}
    p, cands = m.resolve("A座", "粤海")
    assert p is None and {c.label for c in cands} == {"A座（汇德楼旁）", "A座（汇紫楼旁）"}
    assert m.resolve("汇德楼A座", "粤海")[0].label == "A座（汇德楼旁）"
    assert m.resolve("计算机学院") == (None, [])


def test_describe_gives_landmarks_gate_and_stops():
    m = default_map()
    d = m.describe(m.resolve("汇典楼")[0])
    assert d["shuttleStops"][0]["stop"] in {"汇星楼", "师范学院"}
    assert d["nearestGate"]["name"] == "立德门"
    assert any("档案馆" in x for x in d["nearby"])
    assert all(x["meters"] % 10 == 0 for x in d["shuttleStops"])  # 误差几十米，不给假精度


# ------------------------------ 路线 ------------------------------ #

TUE_10AM = datetime(2026, 11, 17, 10, 0)


def test_route_recommends_shuttle_only_when_it_saves_walking():
    m = default_map()
    r = lambda q: m.resolve(q)[0]
    far = m.route(r("韵竹阁"), r("聚翰斋"), TUE_10AM)
    plan = far["shuttle"]
    assert plan["alight"] == "聚翰斋" and plan["via"][-1] == "聚翰斋" and plan["stops"] == len(plan["via"])
    assert plan["line"].endswith("汇文楼方向（逆时针）") and plan["alsoOn"]  # 内环外环这一段都能坐
    assert far["advice"].startswith("可以坐小巴") and far["walk"]["minutes"] > 15
    assert any("中巴" in t for t in far["tips"])  # 起点在西南宿舍区一带：带上攻略里的中巴经验

    near = m.route(r("汇典楼"), r("汇智楼"), TUE_10AM)
    assert near["advice"].startswith("建议直接走") and near["walk"]["minutes"] <= 8

    lihu = m.route(r("紫罗兰"), r("守贤楼"))  # 丽湖没有攻略：按接口环线、取站数少的方向
    assert "shuttleNow" not in lihu and lihu["shuttle"]["board"] == "紫罗兰"


def test_yuehai_guide_lines_agree_with_geometry_and_respect_direction():
    m = default_map()
    # 两个独立来源互相印证：攻略说的“校医院方向/汇文楼方向”，按站点位置算出来分别是顺时针/逆时针，
    # 而小巴接口的站点顺序（逆时针）正好是汇文楼方向的站序
    for ln in m.lines["粤海"]:
        assert ln.geo == ln.direction, ln.label
    assert m.loops["粤海"].order == m.guides["粤海"]["directions"]["汇文楼方向"]
    # 汇文楼方向不停校医院：去校医院只能坐校医院方向；回程反过来
    r = lambda q: m.resolve(q, "粤海")[0]
    go = m.route(r("听荔餐厅"), r("校医院"), TUE_10AM)["shuttle"]
    assert go["alight"] == "校医院" and "校医院方向" in go["line"]
    back = m.route(r("聚翰斋"), r("韵竹阁"), TUE_10AM)["shuttle"]
    assert "校医院方向" in back["line"] and "师范学院" not in back["via"]
    assert m.resolve("西门")[0].name == "立德门"  # 攻略：“立德门（西门）”


def test_yuehai_service_hours_and_weekend_rings():
    m = default_map()
    r = lambda q: m.resolve(q)[0]
    sat = m.route(r("杜衡阁"), r("汇研楼"), datetime(2026, 11, 21, 10, 0))
    assert sat["shuttleNow"]["rings"] == ["外环"] and sat["shuttle"]["line"].startswith("外环")
    night = m.route(r("杜衡阁"), r("汇研楼"), datetime(2026, 11, 17, 23, 45))
    assert night["shuttle"] is None and "不运营" in night["advice"]
    late = m.service_now("粤海", datetime(2026, 11, 17, 22, 40))
    assert "23 点后基本没车" in late["notes"][0]
    assert m.service_now("丽湖") is None
    d = m.describe(r("汇典楼"))
    assert {"stop": "听荔餐厅", "servedBy": ["校医院方向"]}.items() <= d["shuttleStops"][1].items()


def test_cross_campus_route_lists_next_intercampus_buses():
    m = default_map()
    res = m.route(m.resolve("韵竹阁")[0], m.resolve("守贤楼")[0], now=datetime(2026, 11, 17, 16, 0))  # 周二
    bus = res["intercampusBus"]
    assert res["crossCampus"] and "教职工" in bus["notice"]
    leg = bus["from"][0]
    assert leg["boardAt"].startswith("汇光楼") and leg["image"].startswith("https://")
    assert leg["next"] == {"day": "今天", "times": ["17:50（2 辆）", "21:00", "22:00"]}
    assert res["atDestination"]["shuttleStops"]


def test_next_departures_skip_weekend():
    m = default_map()
    fri_night = m.next_departures("丽湖", datetime(2026, 11, 20, 22, 30))
    assert fri_night["byStop"]["lihu-b3"]["day"].startswith("2026-11-23（周一）")
    assert fri_night["byStop"]["lihu-b3"]["times"][0] == "07:10"
    assert fri_night["byStop"]["lihu-weiyi"]["times"] == ["13:00", "17:50"]


# ------------------------------ 实时位置 ------------------------------ #

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)


def _vehicle(name, lat, lng, *, status="online", age=10, direction="clockwise", speed=15):
    t = (NOW - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
    return {"id": name, "name": name, "plate": "粤B12345", "status": status, "position": {"lat": lat, "lng": lng},
            "speed": speed, "updatedAt": t, "stableDirection": {"direction": direction}}


def test_parse_vehicles_drops_offline_stale_and_plate():
    data = {"data": [_vehicle("1号车", 22.53, 113.93), _vehicle("2号车", 22.53, 113.93, status="offline"),
                     _vehicle("3号车", 22.53, 113.93, age=900), _vehicle("4号车", 22.53, 113.93, direction="weird")]}
    out = shuttle.parse_vehicles(data, now=NOW)
    assert [v["name"] for v in out] == ["1号车", "4号车"]
    assert out[1]["direction"] is None and "粤B" not in json.dumps(out, ensure_ascii=False)
    with pytest.raises(CampusError):
        shuttle.parse_vehicles({"error": "x"})


async def test_get_vehicles_requests_campus_endpoint(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        fresh = _vehicle("1号车", 22.6, 113.99)
        fresh["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return httpx.Response(200, json={"data": [fresh]})

    monkeypatch.setattr(shuttle, "_cache", {})
    monkeypatch.setenv("SZU_AGENT_SHUTTLE_API", "https://bus.example/preview/abc/")
    out = await shuttle.get_vehicles("丽湖", transport=httpx.MockTransport(handler))
    assert str(seen[0]) == "https://bus.example/preview/abc/api/campuses/lihu/vehicles" and out

    monkeypatch.setattr(shuttle, "_cache", {})
    with pytest.raises(CampusError) as exc:
        await shuttle.get_vehicles("粤海", transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    assert exc.value.code == "NETWORK_ERROR"


def test_live_counts_stops_in_each_vehicles_direction():
    m = default_map()
    loop, frame = m.loops["粤海"], m.frames["粤海"]

    def at(stop_name, direction):
        s = loop.stop(stop_name)
        return {"name": f"{stop_name}车", "lat": frame.lat0 + s.n / 110540.0, "lng": frame.lng0 + s.e / frame.k,
                "speedKmh": 10, "ageSeconds": 5, "direction": direction}

    # 列表顺序（逆时针）：北门 → 元平体育场 → 汇星楼；顺时针从聚翰斋出发要绕一大圈
    res = m.live("粤海", [at("北门", "counterclockwise"), at("元平体育场", "clockwise")], stop="汇星楼")
    first, second = res["vehicles"]
    assert first["vehicle"] == "北门车" and first["near"] == "北门" and first["toStop"]["stopsAway"] == 2
    assert second["toStop"]["stopsAway"] == len(loop.stops) - 1 and second["direction"] == "顺时针（校医院方向）"


# ------------------------------ 工具 ------------------------------ #

async def test_navigation_tools(home, monkeypatch):
    monkeypatch.setenv("SZU_AGENT_MOCK", "1")
    async with mcp_client(SessionStore(mock=True)) as client:
        where = await client("campus_navigate", destination="经济学院")
        assert where["ok"] and where["data"]["name"] == "明德楼" and where["data"]["shuttleStops"]

        amb = await client("campus_navigate", destination="校医院")
        assert amb["data"]["ambiguous"] and len(amb["data"]["candidates"]) == 2

        route = await client("campus_navigate", destination="聚翰斋", origin="韵竹阁", campus="粤海")
        assert "shuttleNow" in route["data"] and "学生攻略" in route["data"]["source"]

        missing = await client("campus_navigate", destination="霍格沃茨")
        assert missing["data"]["found"] is False and "不要凭印象" in missing["data"]["hint"]

        bus = await client("shuttle_status", campus="粤海", stop="汇典楼")  # 楼名换成最近的站
        assert bus["ok"] and bus["data"]["stop"] in {"汇星楼", "师范学院"}
        assert len(bus["data"]["vehicles"]) == 2 and all("toStop" in v for v in bus["data"]["vehicles"])
        assert bus["data"]["intercampus"]["allTimes"][0] == "07:10"
        guide = bus["data"]["guide"]
        assert "1 元" in guide["fare"] and guide["hours"].startswith("06:30-23:30") and "非官方" in guide["source"]
        assert any("（校医院方向）" in v["direction"] or "（汇文楼方向）" in v["direction"] for v in bus["data"]["vehicles"])
        lihu = await client("shuttle_status", campus="丽湖")
        assert "guide" not in lihu["data"]
