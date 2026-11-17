"""访问学校经国内服务器转发：只转发学校域名、转发服务器挂了自动直连。"""

import httpx
import pytest

from szu_agent.campus import route
from szu_agent.campus.cas import CasClient
from szu_agent.campus.http import CAMPUS_LIMITS, CampusHttp
from szu_agent.campus.mfa import MfaClient
from szu_agent.campus.route import CampusRouteTransport, campus_transport
from szu_agent.campus.session import SessionStore


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, name: str, fail: Exception | None = None) -> None:
        self.name, self.fail, self.hosts = name, fail, []

    async def handle_async_request(self, request):
        self.hosts.append(request.url.host)
        self.connect_timeout = request.extensions["timeout"]["connect"]
        if self.fail:
            raise self.fail
        return httpx.Response(200, text=self.name)


def make(via_fail=None):
    t = CampusRouteTransport("http://127.0.0.1:9", limits=CAMPUS_LIMITS)
    t.via, t.direct = Recorder("via", via_fail), Recorder("direct")
    return t


async def get(t, url):
    async with httpx.AsyncClient(transport=t) as c:
        return (await c.get(url)).text


async def test_only_campus_hosts_go_via_proxy():
    t = make()
    assert await get(t, "https://ehall.szu.edu.cn/x") == "via"
    assert await get(t, "https://authserver-443.webvpn.szu.edu.cn/x") == "via"
    assert await get(t, "https://api.deepseek.com/x") == "direct"
    assert await get(t, "https://notszu.edu.cn/x") == "direct"


async def test_proxy_unreachable_falls_back_and_backs_off():
    t = make(httpx.ConnectError("refused"))
    assert await get(t, "https://ehall.szu.edu.cn/a") == "direct"
    assert t.down_until > 0
    assert await get(t, "https://ehall.szu.edu.cn/b") == "direct"
    assert len(t.via.hosts) == 1  # 冷却期内不再试转发


async def test_proxy_refusal_falls_back_once_without_backoff():
    t = make(httpx.ProxyError("403"))
    assert await get(t, "https://lecture.szu.edu.cn/a") == "direct"
    assert t.down_until == 0
    await get(t, "https://ehall.szu.edu.cn/b")
    assert len(t.via.hosts) == 2


def test_factory(monkeypatch):
    mock = httpx.MockTransport(lambda r: httpx.Response(200))
    monkeypatch.setenv(route.ENV, "http://10.0.0.1:18888")
    assert campus_transport(mock, CAMPUS_LIMITS) is mock  # 测试传入的 transport 优先
    assert isinstance(campus_transport(None, CAMPUS_LIMITS), CampusRouteTransport)
    monkeypatch.setenv(route.ENV, "")
    assert campus_transport(None, CAMPUS_LIMITS) is None


@pytest.mark.parametrize("make_client", [
    lambda s: CampusHttp(s)._client,
    lambda s: CasClient(s).client,
    lambda s: MfaClient(s).client,
])
async def test_school_clients_use_route_when_configured(monkeypatch, tmp_path, make_client):
    monkeypatch.setenv(route.ENV, "http://10.0.0.1:18888")
    client = make_client(SessionStore(tmp_path / "session.json"))
    assert isinstance(client._transport, CampusRouteTransport)
    await client.aclose()


async def test_proxy_connect_timeout_short_direct_keeps_original():
    t = make(httpx.ConnectTimeout("dropped"))
    async with httpx.AsyncClient(transport=t, timeout=httpx.Timeout(30, connect=20)) as c:
        await c.get("https://ehall.szu.edu.cn/a")
    assert t.via.connect_timeout == route.PROXY_CONNECT_TIMEOUT
    assert t.direct.connect_timeout == 20
