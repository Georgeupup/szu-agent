"""校园导航：建筑在哪、离哪个小巴站近、两地之间走路还是坐小巴、跨校区通勤班车、实时小巴多久到。

数据：
  campus_map.yaml  手绘地图转录（建筑名 + 图上位置），见文件头
  shuttle.yaml     校内小巴环线（站点经纬度、折线）和通勤班车时刻的快照
  shuttle_guide.yaml  粤海小巴学生攻略（非官方）：内环/外环各两个方向的站序、运营时间、票价、经验。
                   接口只有一条环线，但两个方向停的站不一样（汇文楼方向不停校医院），所以粤海按攻略的站序规划坐车；
                   丽湖没有攻略，按接口环线规划

坐标：手绘图不按比例，图上位置不能直接当距离用。地图上画了“校园小巴站”图标，而小巴接口有这些站的真实经纬度，
  于是用这十几个站做控制点：
    1. 最小二乘拟合一个仿射变换（图上 xy → 以校区为原点的东/北米数），吸收旋转、缩放、斜切；
    2. 再用控制点的残差做反距离加权修正（“橡皮膜”校正），控制点本身变成精确的，附近的建筑跟着纠偏。
  留一法验证：粤海平均偏差约 46 米、丽湖约 64 米（只做仿射是 52 / 67 米）。所以输出的距离都取整到 10 米，
  并且只说“东北方向约 200 米”，不给地图打点链接——偏 50 米的图钉比没有更误导人。

估算参数（都是估算，回答里要说“大约”）：
  步行：直线距离 × 1.3（校园道路绕行）÷ 75 米/分钟（4.5 km/h）
  小巴：沿线路折线的距离 ÷ 200 米/分钟（12 km/h，含进站停车；实时数据里行驶中的车速多在 10-30 km/h）
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
DETOUR = 1.3
WALK_M_PER_MIN = 75
BUS_M_PER_MIN = 200
LAT_M = 110540.0  # 每度纬度的米数
DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
DIR_CN = {"clockwise": "顺时针", "counterclockwise": "逆时针"}
CAMPUS_IDS = {"粤海": "yuehai", "丽湖": "lihu"}
CAMPUS_WORDS = {"粤海": ("粤海", "南山"), "丽湖": ("丽湖", "西丽")}


def _r10(m: float) -> int:
    return int(round(m / 10.0) * 10)


def walk_minutes(straight_m: float) -> int:
    return max(1, round(straight_m * DETOUR / WALK_M_PER_MIN))


def bearing_cn(de: float, dn: float) -> str:
    deg = math.degrees(math.atan2(de, dn)) % 360
    return DIRECTIONS[int((deg + 22.5) // 45) % 8]


def _norm(s: str) -> str:
    s = re.sub(r"[\s()（）·・,，。、\"“”]", "", s.lower())
    return s.replace("校区", "")


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


# --------------------------------------------------------------------------- #
# 拟合：图上 xy → 东/北米数
# --------------------------------------------------------------------------- #

def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [v] for row, v in zip(a, b)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(m[r][i]))
        m[i], m[p] = m[p], m[i]
        for r in range(3):
            if r != i:
                f = m[r][i] / m[i][i]
                m[r] = [x - f * y for x, y in zip(m[r], m[i])]
    return [m[i][3] / m[i][i] for i in range(3)]


class MapFit:
    """仿射最小二乘 + 控制点残差的反距离加权修正。控制点至少 3 个且不共线。"""

    def __init__(self, pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> None:
        if len(pairs) < 3:
            raise ValueError("控制点至少要 3 个")
        ata = [[0.0] * 3 for _ in range(3)]
        be, bn = [0.0] * 3, [0.0] * 3
        for (x, y), (e, n) in pairs:
            v = (x, y, 1.0)
            for i in range(3):
                for j in range(3):
                    ata[i][j] += v[i] * v[j]
                be[i] += v[i] * e
                bn[i] += v[i] * n
        self._ce, self._cn = _solve3(ata, be), _solve3(ata, bn)
        self._res = [(xy, (e - self._affine(xy)[0], n - self._affine(xy)[1])) for xy, (e, n) in pairs]

    def _affine(self, xy: tuple[float, float]) -> tuple[float, float]:
        x, y = xy
        return (self._ce[0] * x + self._ce[1] * y + self._ce[2], self._cn[0] * x + self._cn[1] * y + self._cn[2])

    def __call__(self, xy: tuple[float, float]) -> tuple[float, float]:
        e, n = self._affine(xy)
        for cxy, r in self._res:
            if math.dist(xy, cxy) < 0.5:  # 控制点本身：直接用真实位置
                return (e + r[0], n + r[1])
        ws = [(1.0 / math.dist(xy, cxy) ** 2, r) for cxy, r in self._res]
        total = sum(w for w, _ in ws)
        return (e + sum(w * r[0] for w, r in ws) / total, n + sum(w * r[1] for w, r in ws) / total)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Place:
    name: str
    campus: str
    kind: str
    e: float
    n: float
    aka: list[str] = field(default_factory=list)
    note: str | None = None
    near: str | None = None
    unsure: bool = False
    image: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name}（{self.near}旁）" if self.near else self.name

    def brief(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.label, "campus": self.campus, "kind": self.kind}
        if self.aka:
            out["aka"] = self.aka
        if self.note:
            out["note"] = self.note
        if self.unsure:
            out["unsure"] = True
        if self.image:
            out["image"] = self.image
        return out


@dataclass
class Stop:
    name: str
    e: float
    n: float
    s: float  # 沿线路从第一站起的距离（米）


@dataclass
class Loop:
    campus: str
    id: str
    name: str
    stops: list[Stop]
    line: list[tuple[float, float]]  # 折线（东/北米数），首尾相接
    cum: list[float]                 # 折线每个顶点的累计距离
    order: str                       # 站点列表顺序对应的方向：clockwise / counterclockwise

    @property
    def length(self) -> float:
        return self.cum[-1]

    def stop(self, name: str) -> Stop | None:
        return next((s for s in self.stops if s.name == name), None)

    def project(self, e: float, n: float) -> tuple[float, float]:
        """点投影到折线上：返回 (沿线距离, 离线距离)。"""
        best = (math.inf, 0.0)
        for i in range(len(self.line) - 1):
            (ax, ay), (bx, by) = self.line[i], self.line[i + 1]
            dx, dy = bx - ax, by - ay
            seg = dx * dx + dy * dy
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((e - ax) * dx + (n - ay) * dy) / seg))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(e - px, n - py)
            if d < best[0]:
                best = (d, self.cum[i] + t * math.sqrt(seg))
        return best[1], best[0]

    def ahead(self, s_from: float, s_to: float, direction: str) -> float:
        """按 direction 行驶，从沿线位置 s_from 到 s_to 要走多少米。"""
        forward = (s_to - s_from) % self.length
        return forward if direction == self.order else (self.length - forward) % self.length

    def stops_between(self, a: Stop, b: Stop, direction: str) -> list[str]:
        """从 a 坐到 b（按 direction）依次经过的站，含 b 不含 a。"""
        i, j, k = self.stops.index(a), self.stops.index(b), len(self.stops)
        step = 1 if direction == self.order else -1
        out, cur = [], i
        while cur != j:
            cur = (cur + step) % k
            out.append(self.stops[cur].name)
        return out


@dataclass
class Line:
    """攻略里的一条小巴线路（某个环的某个方向）。stops 是 (站名, 位置)；位置不明的站（美术馆）只用来列出经过的站。"""
    ring: str        # 内环 / 外环
    name: str        # 攻略的叫法：校医院方向 / 汇文楼方向
    direction: str   # 地理方向 clockwise / counterclockwise（和车辆接口的方向字段一致）
    stops: list[tuple[str, tuple[float, float] | None]]
    geo: str = ""    # 按站点位置算出来的方向，加载时填；测试用它核对 direction

    @property
    def label(self) -> str:
        return f"{self.ring}·{self.name}（{DIR_CN[self.direction]}）"


def _orientation(pts: list[tuple[float, float]]) -> str:
    """多边形顶点顺序的方向：有向面积 > 0 是逆时针（x=东、y=北）。"""
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    return "counterclockwise" if area > 0 else "clockwise"


class Frame:
    """以校区小巴站的平均位置为原点的局部平面（东、北方向的米数）。校园只有一两公里，平面近似足够。"""

    def __init__(self, lat0: float, lng0: float) -> None:
        self.lat0, self.lng0 = lat0, lng0
        self.k = 111320.0 * math.cos(math.radians(lat0))

    def en(self, lat: float, lng: float) -> tuple[float, float]:
        return ((lng - self.lng0) * self.k, (lat - self.lat0) * LAT_M)


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #

class CampusMap:
    def __init__(self, places: list[Place], loops: dict[str, Loop], frames: dict[str, Frame],
                 intercampus: dict[str, Any], boarding: dict[str, Place], sources: dict[str, Any],
                 guides: dict[str, dict[str, Any]] | None = None, lines: dict[str, list[Line]] | None = None) -> None:
        self.places, self.loops, self.frames = places, loops, frames
        self.intercampus, self.boarding, self.sources = intercampus, boarding, sources
        self.guides, self.lines = guides or {}, lines or {}

    @classmethod
    def load(cls, map_path: Path = HERE / "campus_map.yaml", shuttle_path: Path = HERE / "shuttle.yaml",
             guide_path: Path | None = HERE / "shuttle_guide.yaml") -> "CampusMap":
        m = yaml.safe_load(map_path.read_text(encoding="utf-8"))
        sh = yaml.safe_load(shuttle_path.read_text(encoding="utf-8"))
        places: list[Place] = []
        loops: dict[str, Loop] = {}
        frames: dict[str, Frame] = {}
        boarding: dict[str, Place] = {}
        ic_stops = {s["id"]: s for s in sh["intercampus"]["stops"]}
        for campus, cm in m["campuses"].items():
            raw = sh["loops"][campus]
            frame = Frame(sum(s["lat"] for s in raw["stops"]) / len(raw["stops"]),
                          sum(s["lng"] for s in raw["stops"]) / len(raw["stops"]))
            frames[campus] = frame
            loop = cls._build_loop(campus, raw, frame)
            loops[campus] = loop
            unknown = set(cm["anchors"]) - {s.name for s in loop.stops}
            if unknown:
                raise ValueError(f"{campus} anchors 里的站点在 shuttle.yaml 里找不到：{unknown}")
            fit = MapFit([(tuple(xy), (loop.stop(n).e, loop.stop(n).n)) for n, xy in cm["anchors"].items()])
            mine: list[Place] = []
            for p in cm["places"]:
                e, n = fit(tuple(p["xy"]))
                mine.append(Place(p["name"], campus, p["kind"], e, n, list(p.get("aka", [])), p.get("note"),
                                  p.get("near"), bool(p.get("unsure"))))
            names = {x for p in mine for x in (p.name, *p.aka)}
            for s in loop.stops:  # 只在接口里出现的站名（“北门”“师范学院”）也能被搜到
                if s.name not in names:
                    mine.append(Place(s.name, campus, "小巴站", s.e, s.n, note="校园小巴站"))
            for sid, refs in (cm.get("boarding") or {}).items():
                pts = [next(p for p in mine if p.name == r) for r in refs]
                info = ic_stops[sid]
                boarding[sid] = Place(info["name"], campus, "班车上车点", sum(p.e for p in pts) / len(pts),
                                      sum(p.n for p in pts) / len(pts), note=info["how"], image=info.get("image"))
            places.extend(mine)
        sources = {"map": {**m["source"], "verifiedAt": str(m["verified_at"])},
                   "shuttle": {**sh["source"], "fetchedAt": str(sh["fetched_at"])}}
        guides: dict[str, dict[str, Any]] = {}
        lines: dict[str, list[Line]] = {}
        if guide_path and guide_path.exists():
            g = yaml.safe_load(guide_path.read_text(encoding="utf-8"))
            campus = g["campus"]
            guides[campus] = g
            sources["guide"] = {**g["source"], "collectedAt": str(g["collected_at"])}
            lines[campus] = [cls._build_line(campus, raw, g, loops[campus], places) for raw in g["lines"]]
        return cls(places, loops, frames, sh["intercampus"], boarding, sources, guides, lines)

    @staticmethod
    def _build_line(campus: str, raw: dict[str, Any], guide: dict[str, Any], loop: Loop, places: list[Place]) -> Line:
        mapping = guide.get("stop_places") or {}
        stops: list[tuple[str, tuple[float, float] | None]] = []
        for name in raw["stops"]:
            target = mapping.get(name, name)
            pos = None
            if target is not None:
                st = loop.stop(target)  # 接口里的同名站有 GPS，优先
                p = None if st else next((x for x in places if x.campus == campus and (x.name == target or target in x.aka)), None)
                if not st and not p:
                    raise ValueError(f"攻略站名“{name}”在地图和小巴接口里都找不到，在 stop_places 里写明对应地点或 null")
                pos = (st.e, st.n) if st else (p.e, p.n)  # type: ignore[union-attr]
            stops.append((name, pos))
        line = Line(raw["ring"], raw["direction"], guide["directions"][raw["direction"]], stops)
        line.geo = _orientation([p for _, p in stops if p])
        return line

    @staticmethod
    def _build_loop(campus: str, raw: dict[str, Any], frame: Frame) -> Loop:
        line = [frame.en(lat, lng) for lat, lng in raw["polyline"]]
        if math.dist(line[0], line[-1]) > 1:
            line.append(line[0])
        cum = [0.0]
        for a, b in zip(line, line[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        stops = []
        for s in raw["stops"]:
            e, n = frame.en(s["lat"], s["lng"])
            i = min(range(len(line) - 1), key=lambda k: math.dist(line[k], (e, n)))
            stops.append(Stop(s["name"], e, n, cum[i]))
        return Loop(campus, raw["id"], raw["name"], stops, line, cum, _orientation([(s.e, s.n) for s in stops]))

    # ------------------------------ 查找 ------------------------------ #

    def find(self, query: str, campus: str | None = None, limit: int = 8) -> list[tuple[float, Place]]:
        """按名字/别名找地点，返回 [(分数, 地点)]，分数高的在前。
        完全等于名字 100、等于别名 95、包含关系 70-80、字二元组重合 20-60。查询开头的“粤海/丽湖”当作校区条件。"""
        q = _norm(query)
        for c, words in CAMPUS_WORDS.items():
            for w in words:
                if q.startswith(w) and len(q) > len(w):
                    campus, q = campus or c, q[len(w):]
        if not q:
            return []
        scored: list[tuple[float, int, Place]] = []
        for i, p in enumerate(self.places):
            if campus and p.campus != campus:
                continue
            best = 0.0
            for j, cand in enumerate([p.name, *p.aka, *([f"{p.near}{p.name}"] if p.near else [])]):
                c = _norm(cand)
                if q == c:
                    sc = 100.0 if j == 0 else 95.0
                elif len(q) >= 2 and q in c:
                    sc = 80.0 - (len(c) - len(q))
                elif len(c) >= 2 and c in q:
                    sc = 75.0 - (len(q) - len(c))
                else:
                    a, b = _bigrams(q), _bigrams(c)
                    sc = 60.0 * len(a & b) / len(a | b)
                    sc = sc if sc >= 20 else 0.0
                best = max(best, sc)
            if best:
                scored.append((best, i, p))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(sc, p) for sc, _, p in scored[:limit]]

    def resolve(self, query: str, campus: str | None = None) -> tuple[Place | None, list[Place]]:
        """(唯一结果, 候选)。有歧义（两处“A座”、两个校区都有“校医院”、“图书馆”有三座）或找不到时第一个是 None，
        交给模型去问用户——比猜一个错的楼更好。"""
        hits = self.find(query, campus)
        if not hits:
            return None, []
        top = hits[0][0]
        exact = [p for sc, p in hits if sc >= 95]
        close = [p for sc, p in hits if sc >= top - 20]
        if len(exact) > 1:
            return None, exact
        if not campus and len({p.campus for p in close}) > 1:
            return None, close
        if exact:
            return exact[0], close
        if len(hits) > 1 and hits[1][0] >= top - 5:
            return None, close
        return hits[0][1], close

    # ------------------------------ 描述一个地点 ------------------------------ #

    def nearest_stops(self, p: Place, k: int = 2) -> list[tuple[Stop, float]]:
        loop = self.loops[p.campus]
        return sorted(((s, math.hypot(s.e - p.e, s.n - p.n)) for s in loop.stops), key=lambda x: x[1])[:k]

    def describe(self, p: Place) -> dict[str, Any]:
        out = p.brief()
        others = [q for q in self.places if q.campus == p.campus and q is not p and q.kind not in ("小巴站",)
                  and q.name != p.name]
        near = sorted(others, key=lambda q: math.hypot(q.e - p.e, q.n - p.n))[:4]
        out["nearby"] = [f"在{q.label}的{bearing_cn(p.e - q.e, p.n - q.n)}边约 {_r10(math.hypot(q.e - p.e, q.n - p.n))} 米"
                         for q in near]
        gates = [q for q in others if q.kind == "校门"]
        if gates:
            g = min(gates, key=lambda q: math.hypot(q.e - p.e, q.n - p.n))
            d = math.hypot(g.e - p.e, g.n - p.n)
            out["nearestGate"] = {"name": g.name, "meters": _r10(d), "walkMinutes": walk_minutes(d)}
        if self.lines.get(p.campus):
            out["shuttleStops"] = [{"stop": name, "meters": _r10(d), "walkMinutes": walk_minutes(d), "servedBy": served}
                                   for name, d, served in self.guide_stops_near(p)]
        else:
            out["shuttleStops"] = [{"stop": s.name, "meters": _r10(d), "walkMinutes": walk_minutes(d)}
                                   for s, d in self.nearest_stops(p)]
        return out

    def guide_stops_near(self, p: Place, k: int = 2) -> list[tuple[str, float, list[str]]]:
        """攻略站点里离 p 最近的 k 个：(站名, 直线距离, 哪些方向停)。"""
        seen: dict[str, tuple[float, list[str]]] = {}
        for ln in self.lines.get(p.campus, []):
            for name, pos in ln.stops:
                if pos:
                    d = math.hypot(pos[0] - p.e, pos[1] - p.n)
                    served = seen.setdefault(name, (d, []))[1]
                    if ln.name not in served:
                        served.append(ln.name)
        return sorted(((n, d, sv) for n, (d, sv) in seen.items()), key=lambda x: x[1])[:k]

    def service_now(self, campus: str, now: datetime | None = None) -> dict[str, Any] | None:
        """据攻略：现在有哪些环在跑，以及跟时间有关的提醒。没有攻略的校区返回 None。"""
        g = self.guides.get(campus)
        if not g:
            return None
        from ..campus.booth import beijing_now

        now = now or beijing_now()
        start, end = (time.fromisoformat(g["hours"][k]) for k in ("from", "to"))
        weekend = now.isoweekday() >= 6
        notes: list[str] = []
        if not start <= now.time() <= end:
            rings: list[str] = []
            notes.append(f"现在不在运营时间（{g['hours']['from']}-{g['hours']['to']}）")
        else:
            rings = list(g["rings"]["weekend" if weekend else "weekday"])
            if weekend:
                notes.append("周末只有外环")
            if now.time() >= time(22, 30):
                notes.append(g["hours"]["note"])
            elif now.time() >= time(19, 0) and not weekend:
                notes.append("晚上 7 点后内环车变少，主要靠外环")
        return {"rings": rings, "notes": notes, "source": "学生攻略（非官方）"}

    # ------------------------------ 路线 ------------------------------ #

    def ride(self, a: Stop, b: Stop, campus: str) -> dict[str, Any]:
        """校内小巴从 a 站到 b 站：两个方向都有车，取经过站数少的方向。"""
        loop = self.loops[campus]
        best = None
        for d in ("clockwise", "counterclockwise"):
            meters = loop.ahead(a.s, b.s, d)
            via = loop.stops_between(a, b, d)
            if best is None or len(via) < len(best["via"]) or (len(via) == len(best["via"]) and meters < best["_m"]):
                best = {"direction": DIR_CN[d], "stops": len(via), "via": via, "_m": meters}
        assert best is not None
        m = best.pop("_m")
        best["rideMinutes"] = max(1, round(m / BUS_M_PER_MIN))
        return best

    def route(self, a: Place, b: Place, now: datetime | None = None) -> dict[str, Any]:
        if a.campus != b.campus:
            return self._cross_campus(a, b, now)
        d = math.hypot(b.e - a.e, b.n - a.n)
        walk = {"meters": _r10(d * DETOUR), "minutes": walk_minutes(d),
                "direction": f"{b.label}在{a.label}的{bearing_cn(b.e - a.e, b.n - a.n)}边"}
        out: dict[str, Any] = {"from": a.brief(), "to": b.brief(), "walk": walk}
        svc = self.service_now(a.campus, now)
        if svc is not None:
            out["shuttleNow"] = svc
            running = [ln for ln in self.lines[a.campus] if ln.ring in svc["rings"]]
            if not running:
                out["shuttle"] = None
                out["advice"] = "现在小巴不运营，只能步行（或打车）。"
                return out
            found = self._plan_on_lines(a, b, running)
            # 攻略里提到某个接口站点的经验（“荔园中巴专跑西南宿舍区”），起终点离那个站最近时带上
            ends = {self.nearest_stops(a, 1)[0][0].name, self.nearest_stops(b, 1)[0][0].name}
            tips = [t for t in self.guides[a.campus]["tips"] if any(n in t for n in ends)]
            if tips:
                out["tips"] = tips
            return self._advise(out, d, walk, found)
        best = None
        for s1, d1 in self.nearest_stops(a, 2):
            for s2, d2 in self.nearest_stops(b, 2):
                if s1 is s2:
                    continue
                r = self.ride(s1, s2, a.campus)
                total = walk_minutes(d1) + r["rideMinutes"] + walk_minutes(d2)
                if best is None or total < best[0]:
                    best = (total, d1 + d2, {"board": s1.name, "walkToBoard": walk_minutes(d1), **r,
                                             "alight": s2.name, "walkFromAlight": walk_minutes(d2),
                                             "totalMinutesExcludingWait": total})
        return self._advise(out, d, walk, best)

    @staticmethod
    def _advise(out: dict[str, Any], d: float, walk: dict[str, Any],
                best: tuple[int, float, dict[str, Any]] | None) -> dict[str, Any]:
        """坐不坐小巴由代码判断，不交给模型：步行 ≥ 700 米、坐车后步行不到原来一半、总时间明显更短才建议坐。"""
        if best is None:
            out["shuttle"] = None
            out["advice"] = "两处离同一个小巴站最近，直接走过去。"
            return out
        total, walked, plan = best
        saves_walk = d - walked
        worth = d * DETOUR >= 700 and walked <= 0.5 * d and total + 3 < walk["minutes"]
        out["shuttle"] = plan
        out["advice"] = (f"可以坐小巴：少走约 {_r10(saves_walk * DETOUR)} 米，不算等车约 {total} 分钟。"
                         "班次间隔不固定，等车前用 shuttle_status 看车在哪。" if worth else
                         "建议直接走：坐小巴省不了多少路（还要走到站、等车）。")
        return out

    def _plan_on_lines(self, a: Place, b: Place, lines: list[Line]) -> tuple[int, float, dict[str, Any]] | None:
        """按攻略站序：在每条正在运营的线路上，取离起点、终点最近的两个站组合，选总时间最短的。
        乘车距离 = 途经各站（有位置的）之间的直线距离 × 绕行系数；内环抄近路不经过北门，用折线算会多算一大段。"""
        cands: list[tuple[int, int, float, Line, dict[str, Any]]] = []
        for ln in lines:
            pos = [(i, n, p) for i, (n, p) in enumerate(ln.stops) if p]
            near_a = sorted(pos, key=lambda x: math.hypot(x[2][0] - a.e, x[2][1] - a.n))[:2]
            near_b = sorted(pos, key=lambda x: math.hypot(x[2][0] - b.e, x[2][1] - b.n))[:2]
            for i, na, pa in near_a:
                for j, nb, pb in near_b:
                    if na == nb:
                        continue
                    via, path, cur = [], [pa], i
                    while cur != j:
                        cur = (cur + 1) % len(ln.stops)
                        via.append(ln.stops[cur][0])
                        if ln.stops[cur][1]:
                            path.append(ln.stops[cur][1])  # type: ignore[arg-type]
                    meters = sum(math.dist(p, q) for p, q in zip(path, path[1:])) * DETOUR
                    d1, d2 = math.hypot(pa[0] - a.e, pa[1] - a.n), math.hypot(pb[0] - b.e, pb[1] - b.n)
                    ride = max(1, round(meters / BUS_M_PER_MIN))
                    total = walk_minutes(d1) + ride + walk_minutes(d2)
                    plan = {"board": na, "walkToBoard": walk_minutes(d1), "line": ln.label,
                            "direction": DIR_CN[ln.direction], "stops": len(via), "via": via, "rideMinutes": ride,
                            "alight": nb, "walkFromAlight": walk_minutes(d2), "totalMinutesExcludingWait": total}
                    cands.append((total, len(via), d1 + d2, ln, plan))
        if not cands:
            return None
        total, _, walked, ln, plan = min(cands, key=lambda x: (x[0], x[1]))
        also = [c[3].label for c in cands if c[3] is not ln and c[4]["board"] == plan["board"]
                and c[4]["alight"] == plan["alight"] and c[0] <= total + 2]
        if also:
            plan["alsoOn"] = list(dict.fromkeys(also))
        return total, walked, plan

    def _cross_campus(self, a: Place, b: Place, now: datetime | None) -> dict[str, Any]:
        out: dict[str, Any] = {"from": a.brief(), "to": b.brief(), "crossCampus": True}
        sched = self.next_departures(a.campus, now)
        legs = []
        for sid, info in sched["byStop"].items():
            bp = self.boarding[sid]
            d = math.hypot(bp.e - a.e, bp.n - a.n)
            legs.append({"boardAt": bp.name, "how": bp.note, **({"image": bp.image} if bp.image else {}),
                         "walkMinutes": walk_minutes(d), "next": {k: v for k, v in info.items() if k != "stop"}})
        legs.sort(key=lambda x: x["walkMinutes"])
        out["intercampusBus"] = {"notice": self.intercampus["notice"], "from": legs,
                                 "arrival": "接口没有写到站地点和行车时长，不要猜；到达后再看下面 atDestination 里离目的地最近的小巴站。"}
        out["atDestination"] = self.describe(b)
        return out

    def next_departures(self, campus: str, now: datetime | None = None, k: int = 3) -> dict[str, Any]:
        """某校区出发的通勤班车：今天还有哪几班（按上车点分组）；今天没有了就给下一个运营日的。now 是北京时间。"""
        from ..campus.booth import beijing_now

        now = now or beijing_now()
        deps = [x for x in self.intercampus["departures"] if x["from"] == campus]
        names = {s["id"]: s["name"] for s in self.intercampus["stops"]}
        by_stop: dict[str, Any] = {}
        for sid in dict.fromkeys(x["stop"] for x in deps):
            mine = [x for x in deps if x["stop"] == sid]
            for offset in range(8):
                day = now.date() + timedelta(days=offset)
                times = sorted(x["time"] for x in mine if day.isoweekday() in x["weekdays"])
                if offset == 0:
                    times = [t for t in times if t >= now.strftime("%H:%M")]
                if times:
                    counts = {t: times.count(t) for t in times}
                    upcoming = [f"{t}（{c} 辆）" if c > 1 else t for t, c in counts.items()][:k]
                    label = "今天" if offset == 0 else ("明天" if offset == 1 else f"{day.isoformat()}（周{'一二三四五六日'[day.weekday()]}）")
                    by_stop[sid] = {"stop": names[sid], "day": label, "times": upcoming}
                    break
        allday = sorted(dict.fromkeys(x["time"] for x in deps))
        return {"campus": campus, "byStop": by_stop, "allTimes": allday,
                "runsOn": "周一至周五（法定节假日和临时调整以学校通知为准）"}

    # ------------------------------ 实时 ------------------------------ #

    def live(self, campus: str, vehicles: list[dict[str, Any]], stop: str | None = None) -> dict[str, Any]:
        """vehicles 来自 campus.shuttle.parse_vehicles（已过滤离线车）。给每辆车标出在哪个站附近；
        传了 stop 就按各车的行驶方向算还要几站、约几分钟到这个站。"""
        loop = self.loops[campus]
        frame = self.frames[campus]
        target = loop.stop(stop) if stop else None
        rows = []
        for v in vehicles:
            e, n = frame.en(v["lat"], v["lng"])
            s_v, off = loop.project(e, n)
            near = min(loop.stops, key=lambda s: math.hypot(s.e - e, s.n - n))
            row: dict[str, Any] = {"vehicle": v["name"], "near": near.name,
                                   "metersFromStop": _r10(math.hypot(near.e - e, near.n - n)),
                                   "direction": DIR_CN.get(v.get("direction") or "", "未知"),
                                   "moving": v["speedKmh"] >= 3, "updatedSecondsAgo": v["ageSeconds"]}
            if off > 150:
                row["offRoute"] = True  # 离线路太远：可能在停车场/维修，不参与到站估算
            if target and v.get("direction") in DIR_CN and not row.get("offRoute"):
                meters = loop.ahead(s_v, target.s, v["direction"])
                passed = [s for s in loop.stops if 0 < loop.ahead(s_v, s.s, v["direction"]) < meters]
                row["toStop"] = {"stopsAway": len(passed) + 1, "minutes": round(meters / BUS_M_PER_MIN)}
            rows.append(row)
        if target:
            rows.sort(key=lambda r: r.get("toStop", {}).get("minutes", 10**6))
        out: dict[str, Any] = {"campus": campus, "line": loop.name, "stops": [s.name for s in loop.stops],
                               "stopOrder": f"按列表顺序是{DIR_CN[loop.order]}方向", "vehicles": rows}
        g = self.guides.get(campus)
        if g:
            names = {DIR_CN[geo]: n for n, geo in g["directions"].items()}
            for r in rows:
                if r["direction"] in names:
                    r["direction"] = f"{r['direction']}（{names[r['direction']]}）"
            out["note"] = "实时位置分不出内环外环；两个方向停的站不同，以 campus_navigate 的站序为准"
        return out

    def guide_summary(self, campus: str, now: datetime | None = None) -> dict[str, Any] | None:
        g = self.guides.get(campus)
        if not g:
            return None
        return {"hours": f"{g['hours']['from']}-{g['hours']['to']}（{g['hours']['note']}）", "fare": g["fare"],
                "now": self.service_now(campus, now), "notes": g["notes"], "tips": g["tips"],
                "lines": {ln.label: [n for n, _ in ln.stops] for ln in self.lines.get(campus, [])},
                "source": f"{g['source']['title']}——学生经验，非官方，以车上和学校通知为准"}


    # ------------------------------ 界面上的小巴地图 ------------------------------ #

    def map_view(self, campus: str) -> dict[str, Any]:
        """画小巴地图卡片用的几何数据（东/北米数，取整）。只有线路、站点和校门，不含任何个人信息。"""
        loop = self.loops[campus]
        stops: list[dict[str, Any]] = [{"name": s.name, "e": round(s.e), "n": round(s.n)} for s in loop.stops]
        for ln in self.lines.get(campus, []):  # 粤海：攻略里的站（听荔餐厅、乔相阁…）也画上，并标出哪个方向停
            for name, pos in ln.stops:
                if not pos:
                    continue
                # 攻略叫法不同但就是接口里那个站（四海楼 = 留学生楼）：并成一个点，名字记进 aka
                st = next((x for x in stops if x["name"] == name or name in x.get("aka", [])
                           or math.hypot(x["e"] - pos[0], x["n"] - pos[1]) < 5), None)
                if st is None:
                    st = {"name": name, "e": round(pos[0]), "n": round(pos[1])}
                    stops.append(st)
                elif st["name"] != name and name not in st.setdefault("aka", []):
                    st["aka"].append(name)
                served = st.setdefault("servedBy", [])
                if ln.name not in served:
                    served.append(ln.name)
        gates = [{"name": p.name, "e": round(p.e), "n": round(p.n)} for p in self.places
                 if p.campus == campus and p.kind in ("校门", "地铁站") and not any(p.name == x["name"] for x in stops)]
        return {"campus": campus, "line": [[round(e), round(n)] for e, n in loop.line],
                "order": DIR_CN[loop.order], "stops": stops, "landmarks": gates,
                "directions": {DIR_CN[geo]: n for n, geo in (self.guides.get(campus) or {}).get("directions", {}).items()}}

    def vehicle_points(self, campus: str, vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """实时车辆换算到地图坐标；离线路太远的（停车场、维修）不画。"""
        loop, frame = self.loops[campus], self.frames[campus]
        out = []
        for v in vehicles:
            e, n = frame.en(v["lat"], v["lng"])
            if loop.project(e, n)[1] > 150:
                continue
            out.append({"name": v["name"], "e": round(e), "n": round(n), "direction": DIR_CN.get(v.get("direction") or ""),
                        "heading": v.get("heading"), "moving": v["speedKmh"] >= 3, "ageSeconds": v["ageSeconds"]})
        return out


_default: CampusMap | None = None


def default_map() -> CampusMap:
    global _default
    if _default is None:
        _default = CampusMap.load()
    return _default
