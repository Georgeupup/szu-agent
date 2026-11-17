"""校园小巴实时位置：不需要登录，不发送任何个人信息。

数据源：“荔园小巴”网页（kpeak.szu.edu.cn）的公开接口，它接的是深大小巴的 GPS 数据。
  GET {base}/api/campuses/{yuehai|lihu}/vehicles  → {"data": [车辆…]}
接口来源：该页面的前端脚本（assets/index-*.js 里的 `/api/campuses/${id}/vehicles`），2026-11-17 抓包核对字段：
  name（“7号车”）、status（online）、position{lat,lng}（腾讯地图坐标系，和站点同一套）、speed（km/h）、
  updatedAt（UTC ISO 时间）、stableDirection.direction / directionAssignment.direction（clockwise / counterclockwise，
  按当天轨迹判定的环线方向；用车头朝向对照折线核对过，是地理意义上的顺/逆时针）。
页面地址带一段预览路径，换了就改环境变量 SZU_AGENT_SHUTTLE_API，不用改代码。
线路、站点是静态的，在 knowledge/shuttle.yaml；这里只取车辆。
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .errors import CampusError

DEFAULT_BASE = "https://kpeak.szu.edu.cn:8443/preview/c929e9d7e4d6801814d7d004e6d9ba43ff6f"
CAMPUS_IDS = {"粤海": "yuehai", "丽湖": "lihu"}
CACHE_TTL = 10       # 页面自己也是 10 秒刷新一次
STALE_SECONDS = 300  # 接口的 vehicleLocationMaxAgeSeconds：超过 5 分钟没更新的位置不可信
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def api_base() -> str:
    return os.environ.get("SZU_AGENT_SHUTTLE_API", DEFAULT_BASE).rstrip("/")


def _age(updated: str | None, now: datetime) -> int | None:
    if not updated:
        return None
    try:
        t = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((now - t).total_seconds()))


def parse_vehicles(payload: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """只留在线、位置不过期的车；车牌（plate）等其他字段不要。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CampusError("PAGE_CHANGED", "小巴接口返回的格式变了")
    now = now or datetime.now(timezone.utc)
    out = []
    for v in payload["data"]:
        pos = v.get("position") or {}
        if v.get("status") != "online" or pos.get("lat") is None or pos.get("lng") is None:
            continue
        age = _age(v.get("updatedAt"), now)
        if age is None or age > STALE_SECONDS:
            continue
        direction = ((v.get("stableDirection") or {}).get("direction")
                     or (v.get("directionAssignment") or {}).get("direction"))
        heading = v.get("heading") if v.get("headingKnown") and isinstance(v.get("heading"), (int, float)) else None
        out.append({"name": v.get("name") or "小巴", "lat": float(pos["lat"]), "lng": float(pos["lng"]),
                    "speedKmh": float(v.get("speed") or 0), "ageSeconds": age, "heading": heading,
                    "direction": direction if direction in ("clockwise", "counterclockwise") else None})
    return out


async def get_vehicles(campus: str, *, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    cid = CAMPUS_IDS[campus]
    if (hit := _cache.get(cid)) and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), transport=transport) as client:
            r = await client.get(f"{api_base()}/api/campuses/{cid}/vehicles", headers={"Accept": "application/json"})
            r.raise_for_status()
            data = parse_vehicles(r.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise CampusError("NETWORK_ERROR", "小巴实时位置暂时查不到", detail=type(exc).__name__) from exc
    _cache[cid] = (time.monotonic() + CACHE_TTL, data)
    return data


def mock_vehicles(campus: str, now: float | None = None) -> list[dict[str, Any]]:
    """演示数据：三辆车沿真实线路折线匀速行驶（两辆顺着站点顺序、一辆反向），位置随时间变化，
    这样演示模式里地图卡片每 10 秒刷新也能看到车在动。"""
    from ..knowledge.campusmap import BUS_M_PER_MIN, default_map

    m = default_map()
    loop, frame = m.loops[campus], m.frames[campus]
    t = time.time() if now is None else now
    reverse = {"clockwise": "counterclockwise", "counterclockwise": "clockwise"}[loop.order]
    out = []
    for i, (offset, direction) in enumerate([(0.1, loop.order), (0.55, loop.order), (0.3, reverse)], 1):
        travelled = (t * BUS_M_PER_MIN / 60) % loop.length
        s = (offset * loop.length + (travelled if direction == loop.order else -travelled)) % loop.length
        k = max(j for j, c in enumerate(loop.cum) if c <= s) if s > 0 else 0
        k = min(k, len(loop.line) - 2)
        (ax, ay), (bx, by) = loop.line[k], loop.line[k + 1]
        seg = (loop.cum[k + 1] - loop.cum[k]) or 1.0
        f = (s - loop.cum[k]) / seg
        e, n = ax + f * (bx - ax), ay + f * (by - ay)
        dx, dy = (bx - ax, by - ay) if direction == loop.order else (ax - bx, ay - by)
        heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        out.append({"name": f"{i}号车", "lat": frame.lat0 + n / 110540.0, "lng": frame.lng0 + e / frame.k,
                    "speedKmh": 15.0, "ageSeconds": 4, "heading": round(heading), "direction": direction})
    return out
