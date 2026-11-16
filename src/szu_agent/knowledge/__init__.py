"""校园资料库：学校系统里查不到、但学生常问的静态信息（场地在哪、几点开、怎么约、什么规则）。

数据在 data/*.yaml，一个来源一个文件（来源链接 + 核实日期 + 条目列表），手工维护。

检索 = 结构化过滤（校区、人数）+ BM25 关键词打分：
  - 中文按“字二元组”切词（"研讨室" → 研讨 / 讨室），不用 jieba：
    场地名多是专有名词（风槐斋、山楂树、青冈栎），词典分词反而容易切错；也省一个依赖。
  - 没有向量检索：条目只有几十条，查询词由模型改写（“开会的地方”→“会议室”），关键词足够准。
    要不要换方案看 tests/fixtures/knowledge_eval.yaml 上的命中率，而不是凭感觉。
  - 字段加权：名称、别名比正文重要，命中名称的排前面。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).parent / "data"
STALE_AFTER_DAYS = 180  # 超过一个学期没核实的条目，回答时提醒“可能已更新”
CAMPUSES = ("粤海", "丽湖", "罗湖")

# 查询里的“功能词”：几乎每条都沾边，留着只会把不相关的条目拉进来（“游泳馆开放时间”命中一堆写了“开放时间”的条目）
_STOP_PHRASES = ("开放时间", "在哪里", "在哪儿", "在哪", "哪里", "哪儿", "怎么", "如何", "能不能", "有没有", "可以",
                 "什么", "几点", "的地方", "地方", "预约", "申请", "使用")
_RUN = re.compile(r"[一-鿿]+|[a-z0-9]+")
# 字段权重：通过重复文本实现（BM25 里等价于提高词频）
_WEIGHTS = {"name": 3, "aliases": 3, "location": 2, "category": 1, "kind": 1, "campus": 1, "rules": 1}


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for run in _RUN.findall(text.lower()):
        if run[0].isascii() or len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


@dataclass
class Entry:
    id: str
    name: str
    source: str
    verified_at: date
    data: dict[str, Any]
    tokens: Counter = field(default_factory=Counter)
    length: int = 0

    @property
    def campus(self) -> str:
        return self.data.get("campus", "")

    @property
    def capacity(self) -> int | None:
        return self.data.get("capacity")


def _text_for(data: dict[str, Any]) -> str:
    parts = []
    for key, w in _WEIGHTS.items():
        v = data.get(key)
        if isinstance(v, list):
            v = " ".join(v)
        if v:
            parts.extend([str(v)] * w)
    return " ".join(parts)


def _canon_campus(campus: str | None) -> str | None:
    if not campus:
        return None
    for c in CAMPUSES:
        if c in campus:
            return c
    return campus


class KnowledgeBase:
    def __init__(self, entries: list[Entry], sources: dict[str, dict[str, Any]]) -> None:
        from .bm25 import Bm25

        self.entries = entries
        self.sources = sources
        self._bm25_index = Bm25([e.tokens for e in entries])
        self._pos = {id(e): i for i, e in enumerate(entries)}

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> "KnowledgeBase":
        entries: list[Entry] = []
        sources: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for path in sorted(data_dir.glob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            src = doc["source"]
            verified = doc["verified_at"]
            if not isinstance(verified, date):
                raise ValueError(f"{path.name}: verified_at 必须是 YYYY-MM-DD")
            sources[src["id"]] = {**src, "verifiedAt": verified.isoformat()}
            defaults = doc.get("defaults", {})
            for raw in doc["entries"]:
                data = {**defaults, **raw}
                if data["id"] in seen:
                    raise ValueError(f"{path.name}: 条目 id 重复：{data['id']}")
                seen.add(data["id"])
                toks = Counter(tokenize(_text_for(data)))
                entries.append(Entry(data["id"], data["name"], src["id"], verified, data, toks, sum(toks.values())))
        return cls(entries, sources)

    def _bm25(self, q: list[str], e: Entry) -> float:
        return self._bm25_index.score(q, self._pos[id(e)])

    def search(self, query: str = "", *, campus: str | None = None, min_capacity: int | None = None,
               limit: int = 5) -> list[tuple[Entry, float]]:
        """返回 [(条目, 分数)]。query 为空时只按条件过滤（分数为 0）。"""
        campus = _canon_campus(campus)
        pool = [e for e in self.entries
                if (not campus or e.campus in (campus, "全校", ""))
                and (min_capacity is None or (e.capacity or 0) >= min_capacity)]
        for p in _STOP_PHRASES:
            query = query.replace(p, " ")
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return [(e, 0.0) for e in pool[:limit]]
        scored = [(e, self._bm25(q, e)) for e in pool]
        scored = [x for x in scored if x[1] > 0]
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]

    def is_stale(self, e: Entry, today: date | None = None) -> bool:
        return ((today or date.today()) - e.verified_at).days > STALE_AFTER_DAYS


_default: KnowledgeBase | None = None


def default_kb() -> KnowledgeBase:
    global _default
    if _default is None:
        _default = KnowledgeBase.load()
    return _default
