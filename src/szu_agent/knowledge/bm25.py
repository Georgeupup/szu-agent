"""BM25 打分（场地库和规章细则库共用）。分词用 knowledge.tokenize 的字二元组。"""

from __future__ import annotations

import math
from collections import Counter
from typing import Iterable


class Bm25:
    def __init__(self, docs: list[Counter], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.df: Counter = Counter()
        for d in docs:
            self.df.update(d.keys())
        self.lengths = [sum(d.values()) for d in docs]
        self.avgdl = sum(self.lengths) / max(len(docs), 1)

    def score(self, query: Iterable[str], i: int) -> float:
        n, d, dl = len(self.docs), self.docs[i], self.lengths[i]
        s = 0.0
        for t in query:
            tf = d.get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
            s += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s
