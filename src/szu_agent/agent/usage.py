"""模型调用用量记录：每次调用 LLM 的 token 数、缓存命中、耗时、花费，存在本机 SQLite。

数据来源是模型接口返回的 usage（DeepSeek：prompt_tokens / prompt_cache_hit_tokens / completion_tokens …），
LangChain 把它整理成 AIMessage.usage_metadata：
  input_tokens                       = 缓存命中 + 未命中
  input_token_details.cache_read     = 缓存命中（DeepSeek 的 prompt_tokens_details.cached_tokens，和 prompt_cache_hit_tokens 相同）
  output_tokens / output_token_details.reasoning
流式输出时 usage 在最后一个分块里（runtime.make_llm 打开了 stream_usage）。

只记数字，不记对话内容：将来多人使用时，这张表可以直接给管理员看，不涉及隐私。
user_id 先固定为 "local"，多人版本接上登录后填真实用户。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from ..campus.session import agent_home
from .pricing import CST, cost_cny, peak_applies

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,                 -- UTC ISO 时间
    user_id TEXT NOT NULL DEFAULT 'local',
    thread_id TEXT,
    purpose TEXT NOT NULL,            -- chat：正常回答 / summary：压缩历史
    model TEXT,
    input_tokens INTEGER,             -- 没有 usage 时为 NULL（接口没返回）
    cache_hit_tokens INTEGER,
    cache_miss_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms INTEGER,
    peak INTEGER NOT NULL DEFAULT 0,  -- 是否按高峰价算（DeepSeek 工作日高峰 ×2）
    cost_cny REAL                     -- 写入时按当时价格算好；不认识的模型为 NULL
);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls (ts);
CREATE INDEX IF NOT EXISTS llm_calls_thread ON llm_calls (thread_id);
"""

_SUMS = """COUNT(*) AS calls,
    COALESCE(SUM(input_tokens), 0) AS input, COALESCE(SUM(cache_hit_tokens), 0) AS hit,
    COALESCE(SUM(cache_miss_tokens), 0) AS miss, COALESCE(SUM(output_tokens), 0) AS output,
    COALESCE(SUM(reasoning_tokens), 0) AS reasoning, SUM(cost_cny) AS cost,
    SUM(input_tokens IS NULL) AS no_usage, SUM(cost_cny IS NULL AND input_tokens IS NOT NULL) AS unpriced,
    CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms"""


def beijing_day_start(now: datetime | None = None) -> datetime:
    """北京时间今天 0 点（带时区）。每日限额按北京时间的自然日算。"""
    t = (now or datetime.now(timezone.utc)).astimezone(CST)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def extract_usage(msg: Any) -> dict[str, int] | None:
    u = getattr(msg, "usage_metadata", None) if isinstance(msg, AIMessage) else None
    if not u:
        return None
    inp = int(u.get("input_tokens") or 0)
    hit = int((u.get("input_token_details") or {}).get("cache_read") or 0)
    return {"input": inp, "hit": hit, "miss": max(inp - hit, 0), "output": int(u.get("output_tokens") or 0),
            "reasoning": int((u.get("output_token_details") or {}).get("reasoning") or 0)}


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["hitRate"] = round(d["hit"] / d["input"], 4) if d.get("input") else None
    if d.get("cost") is not None:
        d["cost"] = round(d["cost"], 4)
    return d


class UsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or agent_home() / "usage.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)  # 只有本人可读（Windows 上 chmod 只影响只读位，无害）
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def record(self, msg: Any, *, purpose: str, model: str | None, thread_id: str | None = None,
               latency_ms: int | None = None, user_id: str = "local", at: datetime | None = None) -> None:
        """记录一次调用。写失败只丢这一条，不能影响对话（调用方自己 try）。"""
        at = at or datetime.now(timezone.utc)
        u = extract_usage(msg)
        served = (getattr(msg, "response_metadata", None) or {}).get("model_name")
        model = model or served
        cost = cost_cny(model, cache_hit=u["hit"], cache_miss=u["miss"], output=u["output"], at=at) if u else None
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO llm_calls (ts, user_id, thread_id, purpose, model, input_tokens, cache_hit_tokens,"
                " cache_miss_tokens, output_tokens, reasoning_tokens, latency_ms, peak, cost_cny)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (at.astimezone(timezone.utc).isoformat(timespec="seconds"), user_id, thread_id, purpose, model,
                 *((u["input"], u["hit"], u["miss"], u["output"], u["reasoning"]) if u else (None,) * 5),
                 latency_ms, int(peak_applies(model, at)), cost))

    def spent_today(self, user_id: str, *, now: datetime | None = None) -> float:
        """这个用户北京时间今天已经花了多少元（回答 + 压缩历史都算）。没配价格的模型记为 0。"""
        since = beijing_day_start(now).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(cost_cny), 0) FROM llm_calls WHERE user_id = ? AND ts >= ?",
                             (user_id, since)).fetchone()
        return float(row[0])

    def delete_user(self, user_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM llm_calls WHERE user_id = ?", (user_id,))

    # ------------------------------ 统计 ------------------------------ #

    def report(self, days: int = 7, *, now: datetime | None = None, top: int = 10) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            total = _row(db.execute(f"SELECT {_SUMS} FROM llm_calls WHERE ts >= ?", (since,)).fetchone())
            by_purpose = {r["purpose"]: _row(r) for r in db.execute(
                f"SELECT purpose, {_SUMS} FROM llm_calls WHERE ts >= ? GROUP BY purpose", (since,))}
            # 按北京时间分天
            daily = [_row(r) for r in db.execute(
                f"SELECT date(ts, '+8 hours') AS day, {_SUMS} FROM llm_calls WHERE ts >= ? GROUP BY day ORDER BY day",
                (since,))]
            threads = [_row(r) for r in db.execute(
                f"SELECT thread_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, {_SUMS} FROM llm_calls"
                " WHERE ts >= ? AND thread_id IS NOT NULL GROUP BY thread_id ORDER BY cost DESC, input DESC LIMIT ?",
                (since, top))]
            by_user = [_row(r) for r in db.execute(
                f"SELECT user_id, MAX(ts) AS last_ts, {_SUMS} FROM llm_calls WHERE ts >= ?"
                " GROUP BY user_id ORDER BY cost DESC, input DESC LIMIT ?", (since, top))]
            recent = [_row(r) for r in db.execute(
                "SELECT ts, thread_id, purpose, model, input_tokens AS input, cache_hit_tokens AS hit,"
                " cache_miss_tokens AS miss, output_tokens AS output, latency_ms, peak, cost_cny AS cost"
                " FROM llm_calls ORDER BY id DESC LIMIT 20")]
        return {"days": days, "total": total, "byPurpose": by_purpose, "daily": daily, "threads": threads,
                "byUser": by_user, "recent": recent}
