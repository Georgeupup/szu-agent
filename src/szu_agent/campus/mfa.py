"""统一认证的二次验证（多因素认证 / reAuth）。

学校对部分系统或“非常用地点”的登录要求二次验证：访问子系统时会停在
    /authserver/reAuthCheck/reAuthLoginView.do?isMultifactor=true&service=...
流程（来自学校页面 reAuth.js）：
    POST /reAuthCheck/changeReAuthType.do          切换验证方式（3=短信 4=企业微信 2=账号密码）
    POST /dynamicCode/getDynamicCodeByReauth.do    发送验证码
    POST /reAuthCheck/reAuthSubmit.do              提交验证码
    GET  /login?service=...                        拿 ticket 回到子系统

skipTmpReAuth 必须是 false（学校页面上的“仅本次登录”）。实测（2026-11-13，见 debug.log）：
传 true（“信任此设备”）时学校同样返回 reAuth_success，但随后 /login 仍被拦回二次验证页——
“信任设备”依赖浏览器端的设备识别，HTTP 客户端拿不到，所以这个选项对本程序无效。

挂在 WebVPN 上的系统（事务中心）会停在 authserver-443.webvpn.szu.edu.cn 的验证页：同一个认证服务器经 WebVPN 转发。
所有请求都发到“验证页实际所在的主机”（self.auth），不写死 authserver.szu.edu.cn；进入前先把 CASTGC 复制过去。

验证码和密码一样只在 Web 后端和学校之间传递，不进入 LLM。学号（reAuthUserId）只在服务端使用，不返回给前端。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from urllib.parse import urlparse

from .cas import AUTH, BROWSER_LIKE_LIMITS, encrypt_password
from .debuglog import log, where
from .errors import CampusError
from .http import UA, decode
from .route import campus_transport
from .session import SessionStore

CODE_TYPE_NAMES = {"3": "reAuthDynamicCodeType", "4": "reAuthWChatDynamicCodeType"}
SUPPORTED = {"2": "账号密码", "3": "短信验证码", "4": "企业微信验证码"}


def is_mfa_page(url: str) -> bool:
    return "/authserver/reAuthCheck/" in url


def parse_mfa_page(html: str) -> dict[str, Any]:
    m = re.search(r"reAuthParams\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not m:
        raise CampusError("PAGE_CHANGED", "二次验证页面结构变化：找不到 reAuthParams")
    try:
        params = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise CampusError("PAGE_CHANGED", "二次验证页面参数无法解析") from exc
    methods = dict(re.findall(r'data-type="(\d+)"\s+data-name="([^"]+)"', html))
    methods.update(dict(re.findall(r'class="reauth-tab-more-item"[^>]*id="(\d+)"[^>]*data-name="([^"]+)"', html)))
    reason = BeautifulSoup(params.get("reAuthDec1" if params.get("ipAnomaly") == "1" else "reAuthDec0", ""), "html.parser").get_text()
    return {
        "service": params.get("service") or "",
        "userId": params.get("reAuthUserId") or "",
        "salt": params.get("pwdEncryptSalt") or "",
        "current": str(params.get("reAuthType") or ""),
        # 原样照抄页面上的值，空就发空（jQuery 把 undefined/null 也序列化成空串）。
        # 2026-11-15 浏览器抓包：用户网络下页面给的是空值，浏览器提交 isMultifactor= 通过；
        # 我们曾经“没有就默认 true”，于是切换方式 code=0、提交一律“认证失败”（服务器按多因素规则核对）。
        # 容器（陌生 IP）里页面给的大概本来就是 true，所以默认值碰巧对上、一直没发现。
        "multifactor": "" if params.get("isMultifactor") is None else str(params.get("isMultifactor")),
        # reAuth.js：只有 isSleepAccount === '0' 时浏览器才弹“信任此设备”并带 skipTmpReAuth，否则根本不发这个参数
        "sleepAccount": str(params.get("isSleepAccount") if params.get("isSleepAccount") is not None else ""),
        "ipAnomaly": str(params.get("ipAnomaly") or ""),
        "methods": [{"type": t, "name": n} for t, n in methods.items() if t in SUPPORTED],
        "reason": reason.strip("，, "),
    }


def device_id(path: "Path | None" = None) -> str:
    """本机这份爱小荔的设备 ID：随机生成一次后固定不变（相当于浏览器 localStorage 里的 browser_fingerprint）。

    学校的 common-header.js 在登录页 / 验证页加载时算一个浏览器指纹（UA、语言、canvas 的 MD5，32 位大写十六进制），
    再 GET /authserver/bfp/info?bfp=指纹 登记，服务器据此种 MULTIFACTOR_BROWSER_FINGERPRINT。
    2026-11-15 对比用户浏览器抓包：浏览器提交验证码时带着这个 Cookie 并通过；我们没登记，经 WebVPN 的多因素验证一直“认证失败”。
    这里如实给出“这台设备”的 ID，不模仿任何浏览器；它只是设备标识，验证码仍要用户本人收取和填写。"""
    import os
    import secrets

    from .session import agent_home

    path = path or agent_home() / "device-id"
    try:
        value = path.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9A-F]{32}", value):
            return value
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    value = secrets.token_hex(16).upper()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(value)
    return value


class MfaClient:
    def __init__(self, store: SessionStore, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True,
                                        timeout=httpx.Timeout(30, connect=20), limits=BROWSER_LIKE_LIMITS,
                                        transport=campus_transport(transport, BROWSER_LIKE_LIMITS))
        self.client.cookies = store.cookies()
        self.page: dict[str, Any] | None = None
        self.auth = AUTH  # 验证页所在的认证服务器，start() 里按实际落地的主机改

    async def aclose(self) -> None:
        await self.client.aclose()

    def _cookie_layout(self) -> list[str]:
        """会发给验证页主机的 Cookie：名字@域名+路径（不含值）。用来排查 WebVPN 改写后出现的同名 Cookie。"""
        host = urlparse(self.auth).hostname or ""
        out = []
        for c in self.client.cookies.jar:
            d = (c.domain or "").lstrip(".")
            if host == d or host.endswith("." + d):
                out.append(f"{c.name}@{c.domain}{c.path}")
        return sorted(out)

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            # 浏览器从验证页发出的 XHR 一定带 Origin 和 Referer。2026-11-15 对比：调试脚本带 Origin 时 WebVPN 上的
            # 验证一次通过（reAuth_success），产品代码没带 Origin 时同样的流程“认证失败”，其他请求内容都相同
            u = urlparse(self.auth)
            headers = {"X-Requested-With": "XMLHttpRequest", "Origin": f"{u.scheme}://{u.netloc}"}
            if self.page and self.page.get("viewUrl"):
                headers["Referer"] = self.page["viewUrl"]
            resp = await self.client.post(f"{self.auth}{path}", data=data, headers=headers)
        except httpx.HTTPError as exc:
            log().info("mfa POST %s 网络错误 %s", path, type(exc).__name__)
            raise CampusError("NETWORK_ERROR", f"二次验证请求失败：{path}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            log().info("mfa POST %s -> %s 非 JSON，停在 %s", path, resp.status_code, where(resp.url))
            raise CampusError("MFA_FAILED", f"学校返回了非预期的页面（{path}）") from exc
        # 只记录结果码类字段，不记录 mobile、data 里的个人信息
        log().info("mfa POST %s -> %s %s | 新 Cookie %s | 请求带的 Cookie %s", path, resp.status_code,
                   {k: body.get(k) for k in ("code", "res", "msg", "message", "returnMessage") if k in body},
                   sorted(resp.cookies.keys()), self._cookie_layout())
        return body

    async def start(self, target: str) -> dict[str, Any]:
        """访问触发二次验证的地址，解析出可用的验证方式。返回给前端的信息不含学号。"""
        try:
            resp = await self.client.get(target)
        except httpx.HTTPError as exc:
            raise CampusError("NETWORK_ERROR", f"无法访问 {target}") from exc
        log().info("mfa start %s -> %s | 跳转 %s", where(target), where(resp.url), [where(r.url) for r in resp.history])
        return await self._enter(resp, target)

    @classmethod
    async def adopt(cls, store: SessionStore, client: httpx.AsyncClient, resp: httpx.Response) -> tuple["MfaClient", dict[str, Any]]:
        """接手登录流程停下来的那个会话（CasClient 的 webvpn 登录停在二次验证页）。
        WebVPN 的二次验证必须和登录在同一个会话里完成（见 cas.CasClient 的说明），所以不能另开一个客户端。"""
        self = cls.__new__(cls)
        self.store, self.client, self.page, self.auth = store, client, None, AUTH
        log().info("mfa 接手登录会话 -> %s", where(resp.url))
        return self, await self._enter(resp, str(resp.url))

    async def _enter(self, resp: httpx.Response, target: str) -> dict[str, Any]:
        if not is_mfa_page(str(resp.url)):
            self.store.save(self.client.cookies)
            return {"needed": False}
        self.page = parse_mfa_page(decode(resp))
        u = urlparse(str(resp.url))
        self.auth = f"{u.scheme}://{u.netloc}/authserver"
        await self._register_device()
        self.page["target"] = target
        self.page["viewUrl"] = str(resp.url)
        log().info("mfa page service=%s current=%s multifactor=%r sleepAccount=%s ipAnomaly=%s methods=%s cookies=%s",
                   where(self.page["service"]), self.page["current"], self.page["multifactor"], self.page["sleepAccount"],
                   self.page["ipAnomaly"], [m["type"] for m in self.page["methods"]], sorted({c.name for c in self.client.cookies.jar}))
        if not self.page["methods"]:
            raise CampusError("MFA_UNSUPPORTED", "该账号没有可用的二次验证方式（仅支持短信、企业微信验证码、账号密码）")
        return {"needed": True, "reason": self.page["reason"], "methods": self.page["methods"], "current": self.page["current"]}

    async def _register_device(self) -> None:
        """和验证页上的 common-header.js 一样登记设备指纹（setBFP → GET /bfp/info?bfp=）。失败不影响后续，只记日志。"""
        import time as _time

        try:
            resp = await self.client.get(f"{self.auth}/bfp/info", params={"bfp": device_id(), "_": int(_time.time() * 1000)},
                                         headers={"X-Requested-With": "XMLHttpRequest", "Referer": (self.page or {}).get("viewUrl", "")})
            log().info("mfa 登记设备指纹 -> %s | 新 Cookie %s", resp.status_code, sorted(resp.cookies.keys()))
        except httpx.HTTPError as exc:
            log().info("mfa 登记设备指纹失败 %s", type(exc).__name__)

    def _need_page(self) -> dict[str, Any]:
        if not self.page:
            raise CampusError("MFA_FAILED", "请重新开始二次验证")
        return self.page

    async def _switch(self, method: str) -> None:
        """切换验证方式。和 reAuth.js 一致：当前已经是这种方式就不切；学校返回 code==1 才算切换成功。
        （2026-11-14 日志：在已是企业微信时再切到企业微信，学校回 code=0，之后提交一直“认证失败”。）"""
        page = self._need_page()
        if method == page["current"]:
            return
        body = await self._post("/reAuthCheck/changeReAuthType.do",
                                {"isMultifactor": page["multifactor"], "reAuthType": method, "service": page["service"]})
        if str(body.get("code")) != "1":
            raise CampusError("MFA_FAILED", str(body.get("message") or "切换验证方式失败"))
        page["current"] = str((body.get("data") or {}).get("reAuthType") or method)

    async def send_code(self, method: str) -> str:
        page = self._need_page()
        if method not in CODE_TYPE_NAMES:
            raise CampusError("MFA_FAILED", "这种方式不需要发送验证码")
        await self._switch(method)
        body = await self._post("/dynamicCode/getDynamicCodeByReauth.do",
                                {"userName": page["userId"], "authCodeTypeName": CODE_TYPE_NAMES[method]})
        msg = str(body.get("returnMessage") or "")
        if body.get("res") in ("success", "wechat_success", "other_success"):
            return msg + str(body.get("mobile") or "") if body.get("mobile") else (msg or "验证码已发送")
        raise CampusError("MFA_FAILED", msg or "验证码发送失败")

    async def submit(self, method: str, secret: str) -> dict[str, Any]:
        """secret：验证码（3/4）或密码（2）。

        返回 {"needed": False}：验证完成，升级过的会话已写回 SessionStore。
        返回 {"needed": True, ...}：学校还要再验证一步（2026-11-14 用户反馈：经 WebVPN 访问事务中心时，
        浏览器里企业微信验证码通过后，又要求手机号验证一次），带着新一步可用的验证方式，界面接着验证。"""
        page = self._need_page()
        await self._switch(method)
        data = {
            "service": page["service"], "reAuthType": method, "isMultifactor": page["multifactor"],
            "password": encrypt_password(secret, page["salt"]) if method == "2" else "",
            "dynamicCode": secret if method in CODE_TYPE_NAMES else "",
            "uuid": "", "answer1": "", "answer2": "", "otpCode": "",
        }
        if page["sleepAccount"] == "0":
            data["skipTmpReAuth"] = "false"  # “仅本次登录”；“信任此设备”对非浏览器无效（见模块说明）
        body = await self._post("/reAuthCheck/reAuthSubmit.do", data)
        if body.get("code") in ("reAuth_failed", "reAuth_unauthorized"):
            raise CampusError("MFA_FAILED", str(body.get("msg") or "验证失败"))
        try:
            resp = await self.client.get(f"{self.auth}/login", params={"service": page["service"]})
        except httpx.HTTPError as exc:
            raise CampusError("NETWORK_ERROR", "二次验证后跳转失败") from exc
        log().info("mfa 回跳 -> %s | 跳转 %s | submit code=%s", where(resp.url), [where(r.url) for r in resp.history], body.get("code"))
        if is_mfa_page(str(resp.url)):
            self.steps = getattr(self, "steps", 1) + 1
            if self.steps > 3:
                raise CampusError("MFA_FAILED", f"学校确认了验证码（{body.get('code')}），但连续 3 次回到原系统时仍被要求验证")
            html = decode(resp)
            nxt = parse_mfa_page(html)
            log().info("mfa 第 %s 步 current=%s methods=%s 页面上的全部方式=%s", self.steps, nxt["current"],
                       [m["type"] for m in nxt["methods"]], re.findall(r'data-type="(\d+)"', html))
            if not nxt["methods"]:
                raise CampusError("MFA_UNSUPPORTED", "学校还要求一步验证，但给出的方式不在支持范围（短信、企业微信验证码、账号密码）")
            self.page = {**nxt, "target": page["target"], "viewUrl": str(resp.url)}
            return {"needed": True, "step": self.steps, "reason": nxt["reason"], "methods": nxt["methods"], "current": nxt["current"]}
        self.store.save(self.client.cookies)
        log().info("mfa 完成，会话已保存")
        return {"needed": False}
