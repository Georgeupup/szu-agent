"""校园天气：按校区坐标查询，不需要登录，不发送任何个人信息。

数据源：Open-Meteo（免费、无需 key）优先，访问不了时退回 wttr.in；结果缓存 15 分钟。
两家的天气代码不同（WMO / WWO），统一映射成 kind + 中文描述，界面和背景动画只认 kind。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from .errors import CampusError

CAMPUSES = {
    "粤海校区": (22.5333, 113.9364),
    "丽湖校区": (22.6025, 113.9946),
}
CACHE_TTL = 15 * 60
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# kind: clear / partly / cloudy / overcast / fog / drizzle / rain / storm / snow
WMO = {0: ("clear", "晴"), 1: ("partly", "晴间多云"), 2: ("cloudy", "多云"), 3: ("overcast", "阴"),
       45: ("fog", "雾"), 48: ("fog", "雾"), 51: ("drizzle", "毛毛雨"), 53: ("drizzle", "毛毛雨"), 55: ("drizzle", "毛毛雨"),
       56: ("drizzle", "冻毛毛雨"), 57: ("drizzle", "冻毛毛雨"), 61: ("rain", "小雨"), 63: ("rain", "中雨"), 65: ("rain", "大雨"),
       66: ("rain", "冻雨"), 67: ("rain", "冻雨"), 71: ("snow", "小雪"), 73: ("snow", "中雪"), 75: ("snow", "大雪"), 77: ("snow", "雪粒"),
       80: ("rain", "阵雨"), 81: ("rain", "阵雨"), 82: ("rain", "强阵雨"), 85: ("snow", "阵雪"), 86: ("snow", "阵雪"),
       95: ("storm", "雷阵雨"), 96: ("storm", "雷阵雨伴冰雹"), 99: ("storm", "雷阵雨伴冰雹")}
WWO = {113: ("clear", "晴"), 116: ("partly", "多云"), 119: ("cloudy", "多云"), 122: ("overcast", "阴"),
       143: ("fog", "薄雾"), 248: ("fog", "雾"), 260: ("fog", "雾"),
       176: ("drizzle", "零星小雨"), 263: ("drizzle", "毛毛雨"), 266: ("drizzle", "毛毛雨"), 293: ("drizzle", "小雨"),
       296: ("rain", "小雨"), 299: ("rain", "中雨"), 302: ("rain", "中雨"), 305: ("rain", "大雨"), 308: ("rain", "暴雨"),
       353: ("drizzle", "小阵雨"), 356: ("rain", "阵雨"), 359: ("rain", "强阵雨"),
       200: ("storm", "雷阵雨"), 386: ("storm", "雷阵雨"), 389: ("storm", "强雷阵雨")}
RAINY = {"drizzle", "rain", "storm"}


def campus_for(hint: str | None) -> str:
    """从用户画像里的校区/宿舍信息推断查哪个校区，默认粤海。"""
    return "丽湖校区" if hint and any(k in hint for k in ("丽湖", "西丽")) else "粤海校区"


def _rain_soon(hours: list[tuple[str, float]]) -> str | None:
    """接下来 12 小时里，第一个降雨概率 ≥ 50% 的时刻。"""
    for hh, p in hours:
        if p >= 50:
            return hh
    return None


def parse_open_meteo(d: dict[str, Any]) -> dict[str, Any]:
    c, day = d["current"], d["daily"]
    kind, desc = WMO.get(int(c["weather_code"]), ("cloudy", "多云"))
    now = c.get("time", "")[:13]
    hourly = [(t[11:16], p or 0) for t, p in zip(d["hourly"]["time"], d["hourly"]["precipitation_probability"]) if t[:13] >= now][:12]
    return {
        "source": "open-meteo", "kind": kind, "desc": desc,
        "tempC": round(c["temperature_2m"]), "feelsC": round(c["apparent_temperature"]),
        "humidity": round(c["relative_humidity_2m"]), "cloud": (c.get("cloud_cover") or 0) / 100,
        "raining": kind in RAINY, "todayMax": round(day["temperature_2m_max"][0]), "todayMin": round(day["temperature_2m_min"][0]),
        "rainChance": max([p for _, p in hourly] or [0]), "rainAt": _rain_soon(hourly),
    }


def parse_wttr(d: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    c, day = d["current_condition"][0], d["weather"][0]
    kind, desc = WWO.get(int(c["weatherCode"]), ("cloudy", "多云"))
    now = now or datetime.now()
    # wttr 每天 8 档（0、300、600…2100，3 小时一档）：取今天当前这一档及之后，再接明天的
    today = [(int(h["time"]) // 100, float(h.get("chanceofrain", 0))) for h in d["weather"][0]["hourly"]]
    hourly = [(f"{hh:02d}:00", p) for hh, p in today if hh + 3 > now.hour]
    if len(d["weather"]) > 1:
        hourly += [(f"明天{int(h['time']) // 100:02d}:00", float(h.get("chanceofrain", 0))) for h in d["weather"][1]["hourly"]]
    hourly = hourly[:4]  # wttr 是 3 小时一档，4 档 ≈ 12 小时
    return {
        "source": "wttr.in", "kind": kind, "desc": desc,
        "tempC": int(c["temp_C"]), "feelsC": int(c["FeelsLikeC"]), "humidity": int(c["humidity"]),
        "cloud": int(c.get("cloudcover", 0)) / 100, "raining": kind in RAINY,
        "todayMax": int(day["maxtempC"]), "todayMin": int(day["mintempC"]),
        "rainChance": max([p for _, p in hourly] or [0]), "rainAt": _rain_soon(hourly),
    }


async def get_weather(campus: str = "粤海校区", *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    campus = campus if campus in CAMPUSES else campus_for(campus)
    if (hit := _cache.get(campus)) and hit[0] > time.monotonic():
        return hit[1]
    lat, lon = CAMPUSES[campus]
    errors = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), transport=transport) as client:
        try:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "timezone": "Asia/Shanghai", "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,cloud_cover",
                "daily": "temperature_2m_max,temperature_2m_min", "hourly": "precipitation_probability"})
            data = parse_open_meteo(r.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(f"open-meteo: {type(exc).__name__}")
            try:
                r = await client.get(f"https://wttr.in/{lat},{lon}", params={"format": "j1"})
                data = parse_wttr(r.json())
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc2:
                errors.append(f"wttr.in: {type(exc2).__name__}")
                raise CampusError("NETWORK_ERROR", "天气服务暂时连不上", sources=errors) from exc2
    data["campus"] = campus
    data["updatedAt"] = datetime.now().strftime("%H:%M")
    _cache[campus] = (time.monotonic() + CACHE_TTL, data)
    return data


def mock_weather(campus: str = "粤海校区") -> dict[str, Any]:
    return {"source": "mock", "kind": "partly", "desc": "晴间多云", "tempC": 24, "feelsC": 25, "humidity": 68, "cloud": 0.3,
            "raining": False, "todayMax": 27, "todayMin": 20, "rainChance": 20, "rainAt": None,
            "campus": campus_for(campus), "updatedAt": datetime.now().strftime("%H:%M")}
