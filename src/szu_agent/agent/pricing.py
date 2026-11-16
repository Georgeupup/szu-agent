"""模型价格表：把一次调用的 token 用量折算成金额（人民币元）。

来源：DeepSeek 官方「模型 & 价格」页（中文版，单位 元 / 百万 tokens），2026-11-16 核对：
  https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
价格会变：改了这里就同时改 VERIFIED_AT。每条调用记录在写入时就按当时的价格算好金额，
以后调价不会改写历史花费（和账单的口径一致）。

计费结构（DeepSeek）：输入分“缓存命中 / 未命中”两档，输出一档；
北京时间工作日 9:00-12:00、14:00-18:00 是高峰时段，价格是空闲时段的两倍。
法定节假日全天算空闲——这里不知道节假日安排，工作日一律按高峰算，节假日会略微高估。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
VERIFIED_AT = "2026-11-16"
CST = timezone(timedelta(hours=8))
PEAK_HOURS = ((9, 12), (14, 18))  # 北京时间，周一到周五


@dataclass(frozen=True)
class Price:
    """元 / 百万 tokens（空闲时段）。高峰时段 ×2。"""
    cache_hit: float
    cache_miss: float
    output: float


PRICES: dict[str, Price] = {
    "deepseek-flash": Price(cache_hit=0.02, cache_miss=1.0, output=4.0),
    "deepseek-v4-pro": Price(cache_hit=0.15, cache_miss=4.5, output=13.5),
}
# 旧名字仍可调用，按 Flash 计费（价格页脚注）
ALIASES = {"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash"}


def price_for(model: str | None) -> Price | None:
    m = (model or "").lower()
    return PRICES.get(ALIASES.get(m, m))


def is_peak(at: datetime) -> bool:
    t = at.astimezone(CST) if at.tzinfo else at.replace(tzinfo=timezone.utc).astimezone(CST)
    return t.weekday() < 5 and any(a <= t.hour < b for a, b in PEAK_HOURS)


def cost_cny(model: str | None, *, cache_hit: int, cache_miss: int, output: int, at: datetime) -> float | None:
    """这次调用花了多少元；不认识的模型返回 None（界面上显示“未配置价格”，而不是瞎算一个数）。"""
    p = price_for(model)
    if p is None:
        return None
    factor = 2 if is_peak(at) else 1
    return factor * (cache_hit * p.cache_hit + cache_miss * p.cache_miss + output * p.output) / 1_000_000
