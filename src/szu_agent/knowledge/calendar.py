"""校历：今天是第几教学周、在不在假期、某周是哪几天、各项教学安排、节次作息时间。

数据在 calendar.yaml（照学校官网校历图片手工转录）。
做成“代码算、模型读结果”而不是把校历塞给模型自己推：日期加减、星期几、第几周正是大模型最容易算错的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CALENDAR_FILE = Path(__file__).parent / "calendar.yaml"
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")  # 下标 = date.weekday()


@dataclass
class Term:
    id: str
    name: str
    start: date
    end: date
    week1_sunday: date
    weeks: int
    raw: dict[str, Any]

    @property
    def vacation(self) -> dict[str, Any] | None:
        return self.raw.get("vacation")

    def week_of(self, day: date) -> int | None:
        """教学周次；不在第 1 周到最后一周之间（含报到、缓考那几天）返回 None，和校历表一致。"""
        if not self.start <= day <= self.end:
            return None
        w = (day - self.week1_sunday).days // 7 + 1
        return w if 1 <= w <= self.weeks else None

    def week_days(self, week: int) -> dict[str, str]:
        """某教学周的 7 天，和校历表一样从周日排到周六。"""
        sunday = self.week1_sunday + timedelta(weeks=week - 1)
        return {WEEKDAY_NAMES[(sunday + timedelta(i)).weekday()]: (sunday + timedelta(i)).isoformat() for i in range(7)}

    def in_vacation(self, day: date) -> bool:
        v = self.vacation
        return bool(v) and v["start"] <= day <= v["end"]


class Calendar:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.source = {**doc["source"], "verifiedAt": doc["verified_at"].isoformat()}
        self.terms = sorted(
            (Term(t["id"], t["name"], t["start"], t["end"], t["week1_sunday"], t["weeks"], t) for t in doc["terms"]),
            key=lambda t: t.start)
        self.periods: dict[str, dict[str, Any]] = doc.get("periods") or {}
        for t in self.terms:
            if t.week1_sunday.weekday() != 6:
                raise ValueError(f"{t.id}: week1_sunday 必须是周日")

    @classmethod
    def load(cls, path: Path = CALENDAR_FILE) -> "Calendar":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    def term_containing(self, day: date) -> Term | None:
        return next((t for t in self.terms if t.start <= day <= t.end), None)

    def relevant_term(self, day: date) -> Term | None:
        """学期中 → 当前学期；假期里 → 下一个学期（用户多半在问“什么时候开学”），还没发布就退回假期所属学期。"""
        if t := self.term_containing(day):
            return t
        if nxt := next((t for t in self.terms if t.start > day), None):
            return nxt
        return next((t for t in reversed(self.terms) if t.in_vacation(day)), None)

    def phase(self, day: date) -> dict[str, Any]:
        """某天在校历里的位置。"""
        info: dict[str, Any] = {"date": day.isoformat(), "weekday": WEEKDAY_NAMES[day.weekday()]}
        if t := self.term_containing(day):
            info["term"] = t.name
            w = t.week_of(day)
            if w:
                info["teachingWeek"] = w
                info["phase"] = f"第 {w} 教学周"
            elif day < t.week1_sunday:
                info["phase"] = "学期初（第 1 教学周之前：报到、缓考）"
            else:
                info["phase"] = f"学期末（第 {t.weeks} 教学周之后）"
            return info
        if v := next((t.vacation for t in self.terms if t.in_vacation(day)), None):
            info["phase"] = v["name"]
            info["vacation"] = {"from": v["start"].isoformat(), "to": v["end"].isoformat()}
            return info
        info["phase"] = "不在已录入的校历范围内"
        return info

    def label(self, day: date) -> str | None:
        """给用户消息时间戳用的短标签：“第 12 教学周”“寒假”。认不出就不加。"""
        p = self.phase(day)
        if "teachingWeek" in p:
            return f"第{p['teachingWeek']}教学周"
        return p["phase"] if "vacation" in p else None


def event_status(ev: dict[str, Any], day: date) -> str | None:
    start, end = ev.get("start"), ev.get("end") or ev.get("start")
    if not end:
        return None
    if end < day:
        return "已结束"
    if start and start > day:
        return f"还有 {(start - day).days} 天"
    return "进行中" if start else f"截止还有 {(end - day).days} 天"


def term_outline(t: Term, day: date) -> dict[str, Any]:
    events = []
    for ev in t.raw.get("events", []):
        item: dict[str, Any] = {"name": ev["name"]}
        if ev.get("start"):
            item["from"] = ev["start"].isoformat()
        if ev.get("end") and ev.get("end") != ev.get("start"):
            item["to"] = ev["end"].isoformat()
        for k in ("who", "note"):
            if ev.get(k):
                item[k] = ev[k]
        if s := event_status(ev, day):
            item["status"] = s
        events.append(item)
    events.sort(key=lambda e: e.get("from") or e.get("to") or "9999")
    out: dict[str, Any] = {"name": t.name, "from": t.start.isoformat(), "to": t.end.isoformat(),
                           "teachingWeeks": t.weeks, "events": events}
    if v := t.vacation:
        out["vacation"] = {"name": v["name"], "from": v["start"].isoformat(), "to": v["end"].isoformat()}
        if s := event_status(v, day):
            out["vacation"]["status"] = s
    if t.raw.get("notes"):
        out["notes"] = t.raw["notes"]
    return out


@lru_cache(maxsize=1)
def default_calendar() -> Calendar:
    return Calendar.load()


def week_label(day: date) -> str | None:
    try:
        return default_calendar().label(day)
    except (OSError, ValueError, KeyError, yaml.YAMLError):  # 时间戳只是辅助信息，数据文件坏了也不能影响对话
        return None


__all__ = ["Calendar", "Term", "WEEKDAY_NAMES", "default_calendar", "event_status", "term_outline",
           "week_label"]
