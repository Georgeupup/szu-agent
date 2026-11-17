"""深圳大学统一身份认证（金智 CAS）客户端。

扫码登录（推荐，密码完全不经过本程序）：
    GET  /authserver/login?service=...      建立会话、拿到 execution
    GET  /authserver/qrCode/getToken        -> uuid
    GET  /authserver/qrCode/getCode?uuid=   -> 二维码 PNG（用企业微信扫）
    GET  /authserver/qrCode/getStatus.htl   -> 0 等待 / 2 已扫码 / 1 已确认 / 3 已失效
    POST /authserver/login  (cllt=qrLogin, uuid, execution...)  -> 颁发 CASTGC

账号密码登录：
    GET  /authserver/checkNeedCaptcha.htl?username=  -> 需要滑块验证码时放弃，引导扫码（不破解验证码）
    POST /authserver/login  password = AES-CBC(随机64字符 + 密码, key=页面盐, iv=随机16字符) 的 base64

两种方式成功后都只把 Cookie 存到 SessionStore；密码用完即丢，不落盘、不进日志、不进 LLM。
"""

from __future__ import annotations

import base64
from urllib.parse import urlparse
import secrets
import time
from dataclasses import dataclass
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import CampusError
from .http import UA, decode, is_login_page
from .route import campus_transport
from .session import SessionStore

AUTH = "https://authserver.szu.edu.cn/authserver"
# 经学校 WebVPN 转发的同一个统一认证（事务中心等挂在 WebVPN 上的系统从这里登录）
WEBVPN_AUTH_HOST = "authserver-443.webvpn.szu.edu.cn"
WEBVPN_AUTH = f"https://{WEBVPN_AUTH_HOST}/authserver"
# 登录 + 二次验证要横跨用户看验证码、输入的几十秒。httpx 默认空闲 5 秒就关连接，之后每个请求都新建连接；
# 浏览器（学校服务器只支持 HTTP/1.1）会把空闲连接留几分钟、整个验证都走同一条连接。
# 2026-11-15：用户电脑上切换验证方式、提交验证码都被当成“不是这次验证”（code=0 / 认证失败），只有发码成功；
# 怀疑出口按连接换 IP（验证页参数里有 ipAnomaly），所以让连接像浏览器一样一直复用。
BROWSER_LIKE_LIMITS = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300)


# 登录后落地到网上办事大厅；其他子系统靠 CASTGC 自动单点登录
DEFAULT_SERVICE = "https://ehall.szu.edu.cn/login?service=https://ehall.szu.edu.cn/new/index.html"
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

QrState = Literal["waiting", "scanned", "confirmed", "expired"]
QR_STATES: dict[str, QrState] = {"0": "waiting", "2": "scanned", "1": "confirmed", "3": "expired"}


def _rand(n: int) -> str:
    return "".join(secrets.choice(AES_CHARS) for _ in range(n))


def encrypt_password(password: str, salt: str) -> str:
    """复刻登录页 encrypt.js 的 encryptPassword。"""
    padder = padding.PKCS7(128).padder()
    data = padder.update((_rand(64) + password).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(salt.strip().encode()), modes.CBC(_rand(16).encode())).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


@dataclass
class LoginPage:
    execution: str
    salt: str | None


def parse_login_page(html: str, form_id: str) -> LoginPage:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id=form_id) or soup
    execution = form.find("input", attrs={"name": "execution"})
    salt = soup.find("input", id="pwdEncryptSalt")
    if not execution:
        raise CampusError("PAGE_CHANGED", "统一认证登录页结构变化：找不到 execution")
    return LoginPage(execution=execution.get("value", ""), salt=salt.get("value") if salt else None)


def login_error_message(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ("#showErrorTip", "#errorMsg", ".auth_error", "#msg"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return "登录没有成功（学校页面没有给出原因）"


class CasClient:
    """一次登录流程用一个实例。扫码登录要跨多次 HTTP 调用（取码 -> 轮询 -> 提交），所以实例要保留住。

    realm="webvpn"：登录学校 WebVPN（事务中心等系统挂在它后面）。和主登录是同一个统一认证，但要在
    WebVPN 转发的认证入口 authserver-443.webvpn 上单独登录：
      - 2026-11-15 实测：把主登录的 CASTGC 复制过去，会话能被认出，但随后的二次验证提交一律“认证失败”；
        在 authserver-443.webvpn 上直接登录、再在同一个会话里验证，一次通过（reAuth_success）。
      - 所以从事务中心入口出发（拿到带正确 service 的登录页），登录后停在二次验证页时把这个响应留在
        mfa_response 里，由 Web 后端把同一个 httpx 会话交给 MfaClient.adopt 继续。
    """

    def __init__(self, store: SessionStore, *, service: str = DEFAULT_SERVICE,
                 transport: httpx.AsyncBaseTransport | None = None, realm: str = "main") -> None:
        self.store = store
        self.service = service
        self.realm = realm
        self.auth = WEBVPN_AUTH if realm == "webvpn" else AUTH
        self.client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True,
                                        timeout=httpx.Timeout(60 if realm == "webvpn" else 30, connect=20),
                                        limits=BROWSER_LIKE_LIMITS,
                                        transport=campus_transport(transport, BROWSER_LIKE_LIMITS))
        if realm == "webvpn":
            # 保留主登录等其他 Cookie（保存时整份写回），只丢掉旧的 WebVPN Cookie，从干净的 WebVPN 状态开始
            for c in store.cookies().jar:
                if not (c.domain or "").lstrip(".").endswith("webvpn.szu.edu.cn"):
                    self.client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        self._page: LoginPage | None = None
        self.login_url: str | None = None
        self.mfa_response: httpx.Response | None = None  # webvpn：登录后停在二次验证页
        self.qr_uuid: str | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _open_login_page(self, form_id: str) -> LoginPage | None:
        try:
            if self.realm == "webvpn":
                from .booth import BOOTH_PAGE, LOGIN_URL

                resp = await self.client.get(LOGIN_URL, params={"front": BOOTH_PAGE})
            else:
                resp = await self.client.get(f"{AUTH}/login", params={"service": self.service})
        except httpx.HTTPError as exc:
            raise CampusError("NETWORK_ERROR", f"无法访问统一认证：{type(exc).__name__}") from exc
        if not self._on_login_page(resp):
            if "/reAuthCheck/" in str(resp.url):
                self.mfa_response = resp
            return None  # 已经登录（例如 Cookie 仍有效），authserver 直接放行了
        self.login_url = str(resp.url)  # 带着 service 参数，提交登录就发到这里
        self._page = parse_login_page(decode(resp), form_id)
        return self._page

    def _form_headers(self) -> dict[str, str]:
        """浏览器提交登录表单时带的 Origin / Referer（登录页本身）。"""
        u = urlparse(self.login_url or self.auth)
        return {"Origin": f"{u.scheme}://{u.netloc}", "Referer": self.login_url or self.auth}

    def _on_login_page(self, resp: httpx.Response) -> bool:
        u = urlparse(str(resp.url))
        if self.realm == "webvpn":
            return u.hostname == WEBVPN_AUTH_HOST and u.path.rstrip("/").endswith("/authserver/login")
        return is_login_page(str(resp.url))

    def _finish(self, resp: httpx.Response, method: str) -> None:
        if self.realm == "webvpn":
            if self._on_login_page(resp):
                raise CampusError("LOGIN_FAILED", login_error_message(decode(resp)))
            if "/reAuthCheck/" in str(resp.url):
                self.mfa_response = resp  # 交给 MfaClient.adopt，在同一个会话里验证
            self.store.save(self.client.cookies)  # 不改主登录的 method / loggedInAt
            return
        if is_login_page(str(resp.url)) or "CASTGC" not in {c.name for c in self.client.cookies.jar}:
            raise CampusError("LOGIN_FAILED", login_error_message(decode(resp)))
        self.store.save(self.client.cookies, method=method, loggedInAt=int(time.time()))

    # ---------------- 扫码 ----------------

    async def qr_image(self) -> str:
        """开始扫码登录，返回二维码图片（data URL，前端直接显示）。"""
        await self._open_login_page("qrLoginForm")
        token = (await self.client.get(f"{self.auth}/qrCode/getToken", params={"ts": int(time.time() * 1000)})).text.strip()
        if not token:
            raise CampusError("PAGE_CHANGED", "统一认证没有返回二维码 token")
        self.qr_uuid = token
        img = await self.client.get(f"{self.auth}/qrCode/getCode", params={"uuid": token})
        if not img.content.startswith(b"\x89PNG"):
            raise CampusError("PAGE_CHANGED", "统一认证返回的二维码不是图片")
        return "data:image/png;base64," + base64.b64encode(img.content).decode()

    async def qr_poll(self) -> QrState:
        """查一次扫码状态；确认后自动完成登录并保存凭证。"""
        if not self.qr_uuid or not self._page:
            raise CampusError("LOGIN_FAILED", "请先获取二维码")
        raw = (await self.client.get(
            f"{self.auth}/qrCode/getStatus.htl", params={"ts": int(time.time() * 1000), "uuid": self.qr_uuid}
        )).text.strip()
        state = QR_STATES.get(raw, "waiting")
        if state == "confirmed":
            resp = await self.client.post(self.login_url, headers=self._form_headers(), data={
                "lt": "", "uuid": self.qr_uuid, "cllt": "qrLogin", "dllt": "generalLogin",
                "execution": self._page.execution, "_eventId": "submit", "rmShown": "1",
            })
            self._finish(resp, "qr")
        return state

    # ---------------- 账号密码 ----------------

    async def password_login(self, username: str, password: str, *, remember: bool = True) -> None:
        page = await self._open_login_page("pwdFromId")
        if page is None:
            if self.realm == "webvpn":
                self.store.save(self.client.cookies)
            else:
                self.store.save(self.client.cookies, method="password", loggedInAt=int(time.time()))
            return
        need = await self.client.get(f"{self.auth}/checkNeedCaptcha.htl", params={"username": username, "_": int(time.time() * 1000)})
        if '"isNeed":true' in need.text.replace(" ", ""):
            raise CampusError("CAPTCHA_REQUIRED", "学校要求滑块验证码，请改用扫码登录")
        if not page.salt:
            raise CampusError("PAGE_CHANGED", "统一认证登录页结构变化：找不到加密盐")
        data = {
            "username": username,
            "password": encrypt_password(password, page.salt),
            "captcha": "", "_eventId": "submit", "cllt": "userNameLogin", "dllt": "generalLogin",
            "lt": "", "execution": page.execution,
        }
        if remember:
            data["rememberMe"] = "true"  # 学校给 7 天免登录
        resp = await self.client.post(self.login_url, data=data, headers=self._form_headers())
        self._finish(resp, "password")
