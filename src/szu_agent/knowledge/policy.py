"""规章细则库：长篇条文（目前是各学院的奖学金/助学金评选细则），按章节切段 + BM25 检索。

数据在 docs/<类别>/*.md，由 `szu-agent kb-build` 从公文通原文生成（YAML 头 + 正文 + 规则类附件）。

和场地库（knowledge/__init__.py）的区别：
  - 场地库一条记录就是一个场地，字段齐全；这里是几千字的公文，要先切段，只把相关的几段给模型
  - 切段按公文自己的层级（“一、”“（一）”“第X条”、附件标题），每段带“文档标题 › 章 › 节”前缀：
    单看一段也知道是哪个学院、哪类奖学金、哪一节（检索时这个前缀也参与打分）
  - 表格切开时每段都重复表头，不然后半张表的数字没有列名
  - 学院必须过滤：同一类奖学金，各学院条件不一样，混着给模型最容易张冠李戴
仍然不用向量检索：先看 tests/fixtures/policy_eval.yaml 的命中率，不够再说。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import tokenize
from .bm25 import Bm25

DOCS_DIR = Path(__file__).parent / "docs"
TARGET_CHARS = 900   # 小节合并到这个长度左右
MAX_CHARS = 1600     # 超过就按行切开
SMALL_CHARS = 300    # 比这短的块跨章合并
SCHOOL = "深圳大学"   # 校级文件，查任何学院都带上

H1 = re.compile(r"^(#{1,2} |[一二三四五六七八九十]+、|第[一二三四五六七八九十百]+章)")
H2 = re.compile(r"^([（(][一二三四五六七八九十]+[）)]|第[一二三四五六七八九十百]+条)")
# 学生的说法 → 公文里的说法。只收确实对不上字面的（“挂科”在细则里写作“不及格”），
# 加一条就在评测集里加一条对应用例，确认有用再留
SYNONYMS = {
    "挂科": "不及格 课程及格 补考 重修", "绩点": "GPA 学分绩", "钱": "元 标准", "多少钱": "元 标准", "奖金": "元 标准",
    "名额": "比例 名额", "截止": "截止 前提交", "ddl": "截止 前提交", "处分": "处分 违纪", "贫困": "家庭经济困难",
    "竞赛": "竞赛 比赛", "论文": "论文 期刊", "学生干部": "学生干部 任职", "班干部": "班干部 任职",
    "体测": "体质健康", "志愿时长": "志愿服务时长", "综测": "综合测评 综合成绩",
}
STOP = ("怎么", "如何", "什么", "多少", "能不能", "可以", "有没有", "哪些", "是否", "需要", "请问", "我们", "学院")
LEVELS = {"本科生": {"本科生", "不限"}, "研究生": {"研究生", "博士生", "不限"}, "博士生": {"研究生", "博士生", "不限"}}


@dataclass
class PolicyDoc:
    id: str
    meta: dict[str, Any]
    path: Path

    @property
    def title(self) -> str:
        return self.meta["title"]

    @property
    def publisher(self) -> str:
        return self.meta["publisher"]


@dataclass
class Chunk:
    doc: PolicyDoc
    heading: str
    text: str


def parse_doc(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    if not m:
        raise ValueError(f"{path.name}: 缺少 YAML 头")
    return yaml.safe_load(m[1]), raw[m.end():]


def _split_long(lines: list[str]) -> list[list[str]]:
    """一段太长时按行切；切到表格中间时给后一段补上表头。"""
    pieces: list[list[str]] = []
    cur: list[str] = []
    header: list[str] = []
    size = 0
    for i, ln in enumerate(lines):
        if ln.startswith("|") and (i == 0 or not lines[i - 1].startswith("|")):
            header = [ln, lines[i + 1]] if i + 1 < len(lines) and lines[i + 1].startswith("| ---") else [ln]
        elif not ln.startswith("|"):
            header = []
        if cur and size + len(ln) > MAX_CHARS:
            pieces.append(cur)
            cur = list(header) if ln.startswith("|") and ln not in header else []
            size = sum(len(x) for x in cur)
        cur.append(ln)
        size += len(ln)
    if cur:
        pieces.append(cur)
    return pieces


def chunk_doc(doc: PolicyDoc, body: str) -> list[Chunk]:
    # 1. 按标题切成小节，记下所属的章、节
    sections: list[tuple[str, str, list[str]]] = []
    h1 = h2 = ""
    cur: list[str] = []

    def flush() -> None:
        if any(x.strip() for x in cur):
            sections.append((h1, h2, list(cur)))
        cur.clear()

    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("# "):  # 一级标题就是文档标题，已经在前缀里
            continue
        if H1.match(s):
            flush()
            h1, h2 = s.lstrip("# ")[:40], ""
        elif H2.match(s):
            flush()
            h2 = s[:40]
        cur.append(s)
    flush()

    # 2. 同一章下相邻小节合并到 TARGET_CHARS 左右；太长的再切
    #    前一块很短（一两行的章，如“一、评选对象”）时跨章也合并，避免几十字的碎段
    merged: list[tuple[list[str], list[str]]] = []
    for sh1, _, lines in sections:
        n = sum(len(x) for x in lines)
        if merged:
            heads, prev = merged[-1]
            size = sum(len(x) for x in prev)
            if size + n <= TARGET_CHARS and (heads[-1] == sh1 or size < SMALL_CHARS):
                prev.extend(lines)
                if heads[-1] != sh1:
                    heads.append(sh1)
                continue
        merged.append(([sh1], list(lines)))
    chunks: list[Chunk] = []
    for heads, lines in merged:
        for piece in _split_long(lines):
            chunks.append(Chunk(doc, " / ".join(h for h in heads if h), "\n".join(piece)))
    return chunks


# 学生口头的简称 → 发文单位全称里的关键片段
COLLEGE_ALIASES = {
    "计软": "计算机与软件", "计院": "计算机与软件", "电信": "电子与信息工程", "电子": "电子与信息工程",
    "机电": "机电与控制工程", "化环": "化学与环境工程", "化学": "化学与环境工程", "土木": "土木与交通工程",
    "生科": "生命与海洋科学", "物光": "物理与光电工程", "物理": "物理与光电工程", "数科": "数学科学",
    "数学": "数学科学", "政管": "政府管理", "管院": "管理学院", "经院": "经济学院", "外院": "外国语",
    "传院": "传播", "建规": "建筑与城市规划", "建筑": "建筑与城市规划", "马院": "马克思主义", "高研院": "高等研究院",
    "金科": "金融科技", "人工智能": "人工智能学院", "AI": "人工智能学院", "医学院": "医学部", "教育学院": "教育学部",
    "艺术学院": "艺术学部", "集成电路": "集成电路学院", "光明实验室": "人工智能与数字经济",
}
_SUFFIX = re.compile(r"(学院|学部|研究院)$")


def match_college(name: str, publishers: set[str]) -> set[str]:
    """把用户说的学院名对到发文单位上。按可靠程度依次试，前一步对上就不再往下：
    1. 全称（“管理学院”只对管理学院，不能顺带把“政府管理学院”也算上）
    2. 简称表（“计软”“政管”）
    3. 去掉“学院”后互相包含（“计算机” → 计算机与软件学院）；仍不唯一时宁可返回多个，让调用方提示用户
    """
    name = name.strip()
    if not name:
        return set()
    if name in publishers or name + "学院" in publishers:
        return {name if name in publishers else name + "学院"}
    for alias, key in COLLEGE_ALIASES.items():
        if name.lower() == alias.lower() or name.lower().startswith(alias.lower()) and len(name) <= len(alias) + 2:
            hit = {p for p in publishers if key in p}
            if hit:
                return hit
    q = _SUFFIX.sub("", name)
    return {p for p in publishers if q and (q in p or _SUFFIX.sub("", p) == q)}


class PolicyBase:
    def __init__(self, chunks: list[Chunk], docs: list[PolicyDoc]) -> None:
        self.chunks = chunks
        self.docs = docs
        self.publishers = {d.publisher for d in docs}
        self._index = Bm25([self._tokens(c) for c in chunks])

    @staticmethod
    def _tokens(c: Chunk) -> Counter:
        prefix = f"{c.doc.title} {c.doc.meta.get('kind', '')} {c.heading}"
        return Counter(tokenize(prefix) * 2 + tokenize(c.text))

    @classmethod
    def load(cls, category: str = "scholarship", root: Path = DOCS_DIR) -> "PolicyBase":
        docs: list[PolicyDoc] = []
        chunks: list[Chunk] = []
        for path in sorted((root / category).glob("*.md")):
            meta, body = parse_doc(path)
            doc = PolicyDoc(path.stem, meta, path)
            docs.append(doc)
            chunks.extend(chunk_doc(doc, body))
        return cls(chunks, docs)

    def filter_docs(self, college: str | None, level: str | None) -> tuple[set[str], list[PolicyDoc]]:
        pubs = self.publishers
        if college:
            pubs = match_college(college, self.publishers - {SCHOOL})
            if not pubs:
                return set(), []
            pubs = pubs | {SCHOOL}
        allowed = LEVELS.get(level or "", None)
        docs = [d for d in self.docs if d.publisher in pubs and (allowed is None or d.meta.get("level") in allowed)]
        return pubs - {SCHOOL}, docs

    def search(self, query: str, *, college: str | None = None, level: str | None = None,
               limit: int = 5) -> list[tuple[Chunk, float]]:
        _, docs = self.filter_docs(college, level)
        allowed = {id(d) for d in docs}
        query = query.lower()
        query += " " + " ".join(v for k, v in SYNONYMS.items() if k in query)
        for w in STOP:
            query = query.replace(w, " ")
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return []
        scored = [(c, self._index.score(q, i)) for i, c in enumerate(self.chunks) if id(c.doc) in allowed]
        scored = [x for x in scored if x[1] > 0]
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]

    def is_stale(self, doc: PolicyDoc, today: date | None = None) -> bool:
        v = doc.meta.get("verified_at")
        return isinstance(v, date) and ((today or date.today()) - v).days > 365


@lru_cache(maxsize=4)
def default_policy(category: str = "scholarship") -> PolicyBase:
    return PolicyBase.load(category)
