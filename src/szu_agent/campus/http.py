"""访问学校网站的 HTTP 客户端。

关键点：
  - 单点登录：所有子系统未登录时都会 302 到 authserver。只要 Cookie 里有有效的 CASTGC，
    authserver 会直接签发 ticket 再 302 回来——httpx 自动跟随重定向，相当于“自动登录”。
    如果最终停在了 authserver 的登录页，说明凭证过期，抛 LOGIN_REQUIRED。
  - 编码：公文通等老系统是 GBK，表单提交时中文也要按 GBK 编码。
  - 礼貌访问：并发上限 + 只读缓存，不做高频抓取。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .debuglog import log, where
from .errors import CampusError
from .session import SessionStore

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
INTRANET_NOTICE = "仅限内网访问"


INTRANET_HOSTS = {"lecture.szu.edu.cn"}

# 校外访问时改走学校 WebVPN 的系统。2026-11-16 用户在校外（5G，关闭 SecureLink）实测：
#   - lecture.szu.edu.cn 直连显示“该网站仅限内网访问”，WebVPN 门户里有“领航讲座”
#   - www1（公文通）、ehall、authserver、图书馆在公网，直连即可，不需要 WebVPN
#   - 电费 SIMS（192.168.84.3）在 WebVPN 里也打不开
# WebVPN 地址规律（用户实测 www1-443 / ehall-443，项目实测 authserver-443 / swzx）：
#   https://host/path → https://host-443.webvpn.szu.edu.cn/path；http://host/path → https://host.webvpn.szu.edu.cn/path
# 讲座经 WebVPN 理论上可行（门户里有），但 2026-11-16 决定先不做：服务器版直接说明用不了。
# 以后要做：把 lecture.szu.edu.cn 从 OFFCAMPUS_UNAVAILABLE 移到这里，并用真实账号走通一次
VIA_WEBVPN: set[str] = set()
OFFCAMPUS_UNAVAILABLE = {"lecture.szu.edu.cn"}
WEBVPN_SUFFIX = ".webvpn.szu.edu.cn"


def network_mode() -> str:
    """campus：服务器/电脑在校园网里（或开着 SecureLink），所有系统直连——单人模式默认。
    offcampus：在校外（多人模式部署在校外服务器时默认）——VIA_WEBVPN 里的系统改走 WebVPN，内网 IP 直接报不可用。
    SZU_AGENT_NETWORK=campus|offcampus 可以强制指定（比如服务器其实在校内）。"""
    mode = os.environ.get("SZU_AGENT_NETWORK", "")
    if mode in ("campus", "offcampus"):
        return mode
    return "offcampus" if os.environ.get("SZU_AGENT_MULTIUSER") == "1" else "campus"


def webvpn_url(url: str) -> str:
    u = urlparse(url)
    name = (u.hostname or "").removesuffix(".szu.edu.cn")  # lecture.szu.edu.cn → lecture
    host = f"{name}-443" if u.scheme == "https" else name
    return u._replace(scheme="https", netloc=f"{host}{WEBVPN_SUFFIX}").geturl()


def is_intranet(host: str) -> bool:
    """只能在校园网访问的系统：内网 IP 和少数仅限内网的域名。"""
    return host.startswith(("192.168.", "10.", "172.16.")) or host in INTRANET_HOSTS


def is_login_page(url: str) -> bool:
    u = urlparse(url)
    return u.hostname == "authserver.szu.edu.cn" and u.path.rstrip("/").endswith("/login")


def decode(resp: httpx.Response, default: str = "utf-8") -> str:
    """老系统经常不在 Content-Type 里声明编码，只写在 <meta> 里，甚至哪里都不写（此时用 default）。"""
    raw = resp.content
    charset = resp.charset_encoding
    if not charset:
        m = re.search(rb'charset=["\']?([a-zA-Z0-9_-]+)', raw[:2048])
        charset = m.group(1).decode() if m else default
    if charset.lower() in {"gb2312", "gbk"}:
        charset = "gb18030"  # gb2312 页面里常混着 gbk 字符
    return raw.decode(charset, errors="replace")


def encode_form(data: dict[str, str], charset: str) -> bytes:
    enc = "gb18030" if charset.lower() in {"gb2312", "gbk", "gb18030"} else charset
    return "&".join(f"{quote(k, encoding=enc)}={quote(v, encoding=enc)}" for k, v in data.items()).encode("ascii")


def parse_form(html: str, base_url: str, *, contains: str, submit: str | None = None) -> tuple[str, str, dict[str, str]]:
    """找到包含某个字段的表单，返回 (method, action, 默认字段值)。
    用页面上真实的表单而不是写死参数：学校改了隐藏字段也能继续工作。

    submit：模拟点击哪个提交按钮。浏览器会把被点击按钮的 name=value 一起提交，
    有些老系统（比如公文通的 searchb1）就靠这个字段判断“这是一次搜索”——漏了它，关键词会被忽略。"""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if not form.find(attrs={"name": contains}):
            continue
        fields: dict[str, str] = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                fields[name] = opt.get("value", opt.get_text(strip=True)) if opt else ""
            elif el.get("type") in {"checkbox", "radio"}:
                if el.has_attr("checked"):
                    fields[name] = el.get("value", "on")
            elif el.get("type") in {"submit", "image"}:
                if name == submit:
                    fields[name] = el.get("value", "")
            elif el.get("type") not in {"button", "reset"}:
                fields[name] = el.get("value", "")
        return (form.get("method") or "get").lower(), urljoin(base_url, form.get("action") or base_url), fields
    raise CampusError("PAGE_CHANGED", f"页面上找不到包含 {contains} 的表单")


def select_options(html: str, name: str) -> list[tuple[str, str]]:
    """[(value, 显示文字)]"""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", attrs={"name": name})
    if not sel:
        return []
    return [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option")]


class CampusHttp:
    def __init__(self, store: SessionStore, *, timeout: float = 30, cache_ttl: float = 300,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.cache_ttl = cache_ttl
        self._client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=20),
            # 连接空闲保留 120 秒（httpx 默认 5 秒）：两次工具调用之间隔着模型思考，经常超过 5 秒；
            # 服务器在境外时到学校一次往返约 0.2 秒，重新握手（TCP+TLS）要多花约 0.6 秒
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120),
            transport=transport,
        )
        self._loaded_mtime = -1.0
        self._sem = asyncio.Semaphore(2)
        self._cache: dict[tuple, tuple[float, Any]] = {}

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def _sync_from_disk(self) -> None:
        m = self.store.mtime()
        if m != self._loaded_mtime:
            self._client.cookies = self.store.cookies()
            self._loaded_mtime = m
            self._cache.clear()  # 换了账号/重新登录，旧缓存作废

    def _persist(self) -> None:
        self.store.save(self._client.cookies)
        self._loaded_mtime = self.store.mtime()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def request(
        self,
        method: str,
        url: str,
        *,
        require_login: bool = True,
        cache: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        self._sync_from_disk()
        key = (method, url, repr(sorted(kwargs.items(), key=lambda kv: kv[0])))
        if cache and (hit := self._cache.get(key)) and hit[0] > time.monotonic():
            return hit[1]

        host = urlparse(url).hostname or ""
        via_webvpn = False
        if network_mode() == "offcampus":
            if host in VIA_WEBVPN:
                url, via_webvpn = webvpn_url(url), True
                host = urlparse(url).hostname or ""
            elif host in OFFCAMPUS_UNAVAILABLE or host.startswith(("192.168.", "10.", "172.16.")):
                # 校外连内网 IP 必然失败，不用等超时；WebVPN 也不提供这些系统（电费 SIMS 实测打不开）
                raise CampusError("NETWORK_REQUIRED", f"这个系统（{host}）只在校园网内开放，学校 WebVPN 也不提供，"
                                  "服务器版用不了。请在校园网下（或校外登录学校 SecureLink 零信任后）使用本机版爱小荔")
        intranet = is_intranet(host)
        if intranet:
            # 内网系统连不上时通常是根本不在校园网里，不必傻等 30 秒
            kwargs.setdefault("timeout", httpx.Timeout(10, connect=5))
        async with self._sem:
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                log().info("http %s %s 失败：%s", method, where(url), type(exc).__name__)
                if intranet:
                    raise CampusError("NETWORK_REQUIRED", f"连不上 {host}：这个系统只能在校园网里访问"
                                      "（在校外可以先登录学校 SecureLink 零信任再试）") from exc
                kind = "超时" if isinstance(exc, httpx.TimeoutException) else "连接被学校服务器断开" if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)) else "无法连接"
                raise CampusError("NETWORK_ERROR", f"{kind}：{host}") from exc

        log().info("http %s %s -> %s %s | 跳转 %s | 新 Cookie %s", method, where(url), resp.status_code, where(resp.url),
                   [where(r.url) for r in resp.history], sorted({c for r in [*resp.history, resp] for c in r.cookies.keys()}))
        if require_login and is_login_page(str(resp.url)):
            raise CampusError("LOGIN_REQUIRED", "登录凭证不存在或已过期")
        if "/authserver/reAuthCheck/" in str(resp.url) and (resp.url.host or "").endswith("webvpn.szu.edu.cn"):
            # WebVPN 的二次验证必须和 WebVPN 登录在同一个会话里完成，单独开验证流程一定“认证失败”（2026-11-15 实测），
            # 所以不报 MFA_REQUIRED（那会弹出独立的验证卡片），而是让用户走“登录学校 WebVPN”卡片：登录 + 验证一起做
            raise CampusError("WEBVPN_LOGIN_REQUIRED", "需要重新登录学校 WebVPN（含二次验证）")
        if via_webvpn and resp.url.host != host:
            # 改走 WebVPN 的请求没落在目标站点上：被带去 WebVPN 门户登录（vpn_key/update、sign_in）
            # 或经 WebVPN 转发的统一认证登录页，说明这个用户还没有 / 已过期 WebVPN 会话
            raise CampusError("WEBVPN_LOGIN_REQUIRED", f"需要登录学校 WebVPN（停在 {where(resp.url)}）")
        if "/authserver/reAuthCheck/" in str(resp.url):
            # 已登录，但学校对这个系统 / 非常用地点要求二次验证。target 用来在界面上重新触发这次验证
            target = url if method == "GET" else f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            raise CampusError("MFA_REQUIRED", "学校要求二次验证", target=target)
        if resp.status_code == 429:
            raise CampusError("RATE_LIMITED", "学校系统返回 429")
        if resp.status_code in (401, 403):
            # status 给需要区分“未登录(401)”和“无权限(403)”的子系统用（事务中心场地系统未登录时返回 401）
            raise CampusError("PERMISSION_DENIED", f"学校系统返回 {resp.status_code}", status=resp.status_code)
        if "www1.szu.edu.cn/nc/view.asp" in str(resp.url) or INTRANET_NOTICE in resp.text[:3000]:
            if via_webvpn:  # 走了 WebVPN 还被拦：说明 WebVPN 的出口也不在这个系统的白名单里，需要实测后调整 VIA_WEBVPN
                raise CampusError("NETWORK_REQUIRED", f"{host} 经学校 WebVPN 访问仍提示仅限内网，服务器版暂时用不了这个功能")
            raise CampusError("NETWORK_REQUIRED", "该网站仅限校园内网访问（在校外可以先登录学校 SecureLink 零信任再试）")

        if resp.cookies or resp.history:
            self._persist()  # 跳转过程中拿到的子系统 Cookie 也存下来，下次少走一轮 SSO
        if cache and resp.status_code == 200:
            self._cache[key] = (time.monotonic() + self.cache_ttl, resp)
        return resp

    async def get_html(self, url: str, **kw: Any) -> tuple[str, httpx.Response]:
        resp = await self.request("GET", url, **kw)
        return decode(resp), resp

    async def submit_form(self, page_url: str, html: str, *, contains: str, values: dict[str, str],
                          submit: str | None = None, **kw: Any) -> tuple[str, httpx.Response]:
        method, action, fields = parse_form(html, page_url, contains=contains, submit=submit)
        fields.update(values)
        charset = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', html[:2048])
        body = encode_form(fields, charset.group(1) if charset else "utf-8")
        if method == "post":
            resp = await self.request("POST", action, content=body,
                                      headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": page_url}, **kw)
        else:
            resp = await self.request("GET", f"{action}{'&' if '?' in action else '?'}{body.decode()}", **kw)
        return decode(resp), resp

    async def aclose(self) -> None:
        await self._client.aclose()
