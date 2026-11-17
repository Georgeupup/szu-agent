"""访问学校时经国内服务器转发（服务器在境外时用；设 SZU_AGENT_CAMPUS_PROXY=http://广州IP:端口）。

为什么：2026-11-17 实测，首尔服务器到学校一次往返约 200ms，广州到学校约 7ms，首尔到广州 84ms。
经广州转发后约 91ms，一次工具调用要访问学校好几次，省下的时间能叠加起来。
只转发 *.szu.edu.cn；DeepSeek、天气照常直连（首尔到 DeepSeek 本来就快）。
转发用 HTTP CONNECT 隧道：首尔到学校的 HTTPS 是端到端加密的，中转服务器看不到 Cookie 和页面内容。

转发连不上时自动直连（只是变慢，不会不能用），60 秒后再试转发。
"""

from __future__ import annotations

import os
import time

import httpx

from .debuglog import log

ENV = "SZU_AGENT_CAMPUS_PROXY"
RETRY_AFTER = 60.0


def is_campus_host(host: str) -> bool:
    return host == "szu.edu.cn" or host.endswith(".szu.edu.cn")


class CampusRouteTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy: str, *, limits: httpx.Limits) -> None:
        self.via = httpx.AsyncHTTPTransport(proxy=proxy, limits=limits)
        self.direct = httpx.AsyncHTTPTransport(limits=limits)
        self.down_until = 0.0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if is_campus_host(request.url.host) and time.monotonic() >= self.down_until:
            try:
                return await self.via.handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # 连不上转发服务器本身：这段时间都直连。请求还没发出去，改直连重发是安全的
                self.down_until = time.monotonic() + RETRY_AFTER
                log().info("转发服务器连不上（%s），%d 秒内直连", type(exc).__name__, RETRY_AFTER)
            except httpx.ProxyError as exc:
                # 转发服务器在，但拒绝或连不到这个地址（比如只在内网的系统）：这一次直连，不影响其他请求
                log().info("转发拒绝 %s（%s），本次直连", request.url.host, exc)
        return await self.direct.handle_async_request(request)

    async def aclose(self) -> None:
        await self.via.aclose()
        await self.direct.aclose()


def campus_transport(transport: httpx.AsyncBaseTransport | None, limits: httpx.Limits) -> httpx.AsyncBaseTransport | None:
    """给访问学校的 httpx 客户端用：测试传入的 transport 原样返回；没配转发返回 None（httpx 默认直连）。

    注意 httpx 传了 transport 就不再用客户端的 limits，所以连接保活参数要在这里传给内部的 transport。
    """
    if transport is not None:
        return transport
    proxy = os.environ.get(ENV, "").strip()
    return CampusRouteTransport(proxy, limits=limits) if proxy else None
