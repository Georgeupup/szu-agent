"""宿舍电费充值：算充值方案 + 生成微信充值页的二维码。

付款只能在微信里完成：充值页（cgdzchat.com 的“购买电”）下单后返回的是微信 JSAPI 支付参数
（appId / prepay_id / paySign），只能由微信内置浏览器调起支付，普通浏览器无法使用；
页面的登录会话也来自微信授权。所以这里只做两件事：
  1. 按剩余电量和日均用电量算出“充多少钱、能用几天”（确定性计算，不交给 LLM 心算）
  2. 给充值页生成二维码，用户用微信扫码后在微信里付款
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Any

# 2026-11 抓包：充 10 元得到 14.29 度
PRICE_PER_KWH = round(10 / 14.29, 4)  # ≈ 0.70 元/度
AMOUNTS = [20, 50, 100]

# 充值入口在“微信校园卡 → 服务大厅”里（weixiao.qq.com）。直接扫“购买电”页面会提示权限不足，
# 因为那个页面的登录态来自校园卡服务大厅的微信授权。这里的入口只保留学校/服务大厅的公共编号，
# 扫码后由微信重新授权；能不能直接进入取决于微信，所以卡片上同时给出手动路径。
PAY_STEPS = "微信 → 我 → 卡包 → 深圳大学校园卡 → 服务大厅 → 电费充值"
DEFAULT_PAY_URL = ("https://weixiao.qq.com/apps/public/service/index.html"
                   "?card_id=pzZRGuDpMpnANXoQz_wdv7rNq9n4&hid=bf11210a3cc169eaa981beeb476596f6&media_id=gh_a280c83dc0be")

# 微信授权后链接里会带上个人信息：openid（微信身份）、encrypt_code（校园卡号）、code（一次性授权码）……
# 不管链接从哪来，都先去掉这些参数，绝不把个人身份做进二维码
PERSONAL_PARAMS = {"openid", "unionid", "code", "encrypt_code", "state", "access_token", "token", "ticket", "sessionid", "jsessionid"}


def strip_personal(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    u = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k.lower() not in PERSONAL_PARAMS]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ""))


PAY_URL = strip_personal(os.environ.get("SZU_ELECTRIC_PAY_URL") or DEFAULT_PAY_URL)


def daily_usage(records: list[dict[str, Any]]) -> float | None:
    """用剩余电量的下降速度估算日均用电；中间有充值（剩余电量上升）的那天跳过。"""
    pts = []
    for r in records:
        try:
            pts.append((datetime.strptime(str(r["recordedAt"])[:19], "%Y-%m-%d %H:%M:%S"), float(r["remainingKwh"])))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort()
    used, days = 0.0, 0.0
    for (t0, k0), (t1, k1) in zip(pts, pts[1:]):
        dt = (t1 - t0).total_seconds() / 86400
        if dt <= 0 or k1 > k0:  # 充过值，这一段不算
            continue
        used += k0 - k1
        days += dt
    return round(used / days, 2) if days >= 0.9 and used > 0 else None


def recharge_plan(remaining_kwh: float | None, records: list[dict[str, Any]]) -> dict[str, Any]:
    daily = daily_usage(records)
    plan: dict[str, Any] = {"pricePerKwh": round(PRICE_PER_KWH, 2), "remainingKwh": remaining_kwh, "dailyKwh": daily,
                            "daysLeft": round(remaining_kwh / daily, 1) if remaining_kwh is not None and daily else None}
    plan["options"] = [{"amountCny": a, "kwh": round(a / PRICE_PER_KWH, 1),
                        "extraDays": round(a / PRICE_PER_KWH / daily, 1) if daily else None} for a in AMOUNTS]
    return plan


def qr_svg(url: str = PAY_URL) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]  # 去掉 XML 声明，方便直接嵌进页面
