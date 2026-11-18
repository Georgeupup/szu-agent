"""模型价格表：把一次调用的 token 用量折算成金额（人民币元）。

价格会变：改了这里就同时改对应来源的核对日期。每条调用记录在写入时就按当时的价格算好金额，
以后调价不会改写历史花费（和账单的口径一致）。

DeepSeek（官方 API，2026-11-16 核对）https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
  输入分“缓存命中 / 未命中”两档，输出一档；北京时间工作日 9:00-12:00、14:00-18:00 是高峰时段，价格是空闲时段的两倍。
  法定节假日全天算空闲——这里不知道节假日安排，工作日一律按高峰算，节假日会略微高估。

阿里云百炼（北京地域，按量付费原价，2026-11-17 核对）
  https://docs.bailian.console.aliyun.com/zh/model-studio/model-pricing
  https://docs.bailian.console.aliyun.com/zh/model-studio/context-cache
  百炼上的 deepseek-v4.1-flash：峰谷价，忙时 = 北京时间每天 08:00-22:00（不分工作日），忙时是闲时的两倍
  （输入 忙 2 / 闲 1，输出 忙 8 / 闲 4）；缓存命中按“对应时段输入单价”的 2%。不分档。
  千问：按“单次请求的输入 token 数”分档计价；没有高峰价。隐式缓存自动开启，命中部分按输入单价的 20% 计费
  （qwen3.8-flash 是例外，缓存价只在控制台给，所以没收录）。qwen3.7-plus 页面标“限时8折”但没写截止日期，
  这里按原价记——额度按原价扣，宁可略微高估。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
PEAK_HOURS = ((9, 12), (14, 18))  # 北京时间，周一到周五（DeepSeek 官方）
BAILIAN_BUSY_HOURS = (8, 22)      # 北京时间，每天（百炼上的 DeepSeek）


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    verified_at: str


DEEPSEEK = Source("DeepSeek 官方价格页", "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/", "2026-11-16")
BAILIAN = Source("阿里云百炼模型价格页", "https://docs.bailian.console.aliyun.com/zh/model-studio/model-pricing", "2026-11-17")


@dataclass(frozen=True)
class Price:
    """元 / 百万 tokens，空闲时段的价格。peak 是高峰规则名（见 PEAK_RULES），高峰时段 ×2；None = 没有高峰价。"""
    cache_hit: float
    cache_miss: float
    output: float
    peak: str | None = None
    source: Source = DEEPSEEK


def _qwen(inp: float, out: float) -> Price:
    return Price(cache_hit=round(inp * 0.2, 6), cache_miss=inp, output=out, source=BAILIAN)


# 模型 → [(本档单次输入 token 上限（含）, 价格), ...]，按上限从小到大；None = 不设上限
PRICES: dict[str, list[tuple[int | None, Price]]] = {
    "deepseek-flash": [(None, Price(cache_hit=0.02, cache_miss=1.0, output=4.0, peak="deepseek"))],
    "deepseek-v4-pro": [(None, Price(cache_hit=0.15, cache_miss=4.5, output=13.5, peak="deepseek"))],
    # 百炼部署的 DeepSeek（名字和官方 API 不同，不会混）
    "deepseek-v4.1-flash": [(None, Price(cache_hit=0.02, cache_miss=1.0, output=4.0, peak="bailian", source=BAILIAN))],
    # 千问思考 / 非思考模式输出同价
    "qwen3.7-flash": [(32_000, _qwen(0.2, 0.8)), (256_000, _qwen(0.6, 2.4)), (None, _qwen(1.2, 4.8))],
    "qwen3.7-plus": [(256_000, _qwen(2.0, 8.0)), (None, _qwen(6.0, 24.0))],
}
# 旧名字 / 快照名按同一价格计费
ALIASES = {
    "deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4.1-flash-1017": "deepseek-v4.1-flash",
    "qwen3.7-flash-2026-07-15": "qwen3.7-flash", "qwen3.7-plus-2026-05-26": "qwen3.7-plus",
}


def price_for(model: str | None, input_tokens: int = 0) -> Price | None:
    m = (model or "").lower()
    tiers = PRICES.get(ALIASES.get(m, m))
    if not tiers:
        return None
    for limit, p in tiers:
        if limit is None or input_tokens <= limit:
            return p
    return tiers[-1][1]


def source_for(model: str | None) -> Source:
    p = price_for(model)
    return p.source if p else DEEPSEEK


def _cst(at: datetime) -> datetime:
    return at.astimezone(CST) if at.tzinfo else at.replace(tzinfo=timezone.utc).astimezone(CST)


def is_peak(at: datetime) -> bool:
    """DeepSeek 官方：工作日 9-12、14-18 点。"""
    t = _cst(at)
    return t.weekday() < 5 and any(a <= t.hour < b for a, b in PEAK_HOURS)


def is_bailian_busy(at: datetime) -> bool:
    """百炼峰谷价：每天 8-22 点是忙时。"""
    a, b = BAILIAN_BUSY_HOURS
    return a <= _cst(at).hour < b


PEAK_RULES = {"deepseek": is_peak, "bailian": is_bailian_busy}
PEAK_NOTES = {
    "deepseek": "工作日 9-12、14-18 点（北京时间）是高峰价，法定节假日实际按空闲价，这里会略微高估。",
    "bailian": "北京时间每天 8-22 点是忙时价（闲时的两倍）。",
}


def peak_applies(model: str | None, at: datetime) -> bool:
    """这次调用是否按高峰价算（只有有高峰价的模型才算）。"""
    p = price_for(model)
    return bool(p and p.peak and PEAK_RULES[p.peak](at))


def cost_cny(model: str | None, *, cache_hit: int, cache_miss: int, output: int, at: datetime) -> float | None:
    """这次调用花了多少元；不认识的模型返回 None（界面上显示“未配置价格”，而不是瞎算一个数）。"""
    p = price_for(model, cache_hit + cache_miss)
    if p is None:
        return None
    factor = 2 if p.peak and PEAK_RULES[p.peak](at) else 1
    return factor * (cache_hit * p.cache_hit + cache_miss * p.cache_miss + output * p.output) / 1_000_000
