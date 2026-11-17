"""Web 界面后端：FastAPI + Server-Sent Events。

接口：
    POST /api/chat     {thread_id, message}           -> SSE
    POST /api/resume   {thread_id, approved, reason}  -> SSE（回应预约确认）
    GET  /api/history/{thread_id}                      刷新页面 / 打开历史会话时恢复对话
    GET  /api/threads   /  DELETE /api/threads/{id}    历史会话列表（SQLite 持久化，重启不丢）
    GET  /api/today                                    今日课表 + 宿舍电量
    GET  /api/weather?campus=                          校区天气（左侧“今日荔园”和背景动画用）
    GET  /api/shuttle/map?campus=                      小巴地图卡片：环线、站点、校门（静态几何）
    GET  /api/shuttle/live?campus=                     小巴地图卡片：实时车辆位置（卡片每 10 秒拉一次）
    GET  /api/profile  /  DELETE /api/profile/{key}    长期记忆
    GET / PUT / DELETE /api/contact     图书馆预借用的手机号（只走这里，不进 LLM；GET 只返回打码后的）
    GET  /api/meta                                     演示模式等信息
    GET  /api/auth                                     登录状态
    POST /api/auth/qr          -> {loginId, image}     开始扫码登录
    GET  /api/auth/qr/{loginId} -> {state}             轮询扫码状态（确认后自动保存凭证）
    POST /api/auth/password    {username, password}    账号密码登录
    POST /api/auth/logout
    GET  /api/sports/payment/{orderNo}  付款卡片轮询：订单状态、体育经费余额
    POST /api/sports/cancel/{orderNo}   付款卡片上的“取消预约”按钮（用户亲手点击，不经过 LLM）
    GET  /api/notices/{id}/attachments  某条公文通的附件列表
    GET  /api/notices/attachment?url=&name=&id=   代为下载附件（只允许 www1.szu.edu.cn/board）
    POST /api/auth/mfa                  {target}            -> {mfaId, needed, methods}  二次验证
    POST /api/auth/mfa/{id}/send        {method}            发送验证码
    POST /api/auth/mfa/{id}/submit      {method, secret}

登录接口刻意放在 Web 后端而不是做成 MCP 工具：工具的参数由 LLM 生成，
密码一旦成为工具参数就会进入模型上下文和对话历史。这里密码只在这一次 HTTP 请求里存在。

SSE 事件（data 为 JSON，按 type 区分）：
    token       模型输出的文字片段（流式）
    tool_start  开始调用工具 {id, name, args}
    tool_end    工具返回     {id, name, ok, code?}   code=LOGIN_REQUIRED 时前端弹出登录卡片
                             shuttleMap?             查小巴 / 导航建议坐小巴时，前端画小巴地图卡片
    message     一条完整的助手回复（用于校正流式拼接）
    confirm     图在 human_review 处暂停，等待用户确认 {actions}
    error       出错
    done        本轮结束
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, AsyncIterator
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from ..agent.context import context_stats
from ..agent.graph import message_text
from ..agent.profile import PROFILE_LABELS, ProfileStore
from ..agent.runtime import AgentRuntime, open_runtime
from ..campus import CampusError, SessionStore, make_campus, make_cas, make_mfa
from ..campus.contact import PHONE_RE, ContactStore, mask_phone
from ..campus.pool import CampusPool
from .. import users
from .accounts import COOKIE, SESSION_DAYS, AccountStore

STATIC = Path(__file__).parent / "static"


class ChatIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class PasswordIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True
    realm: Literal["main", "webvpn"] = "main"  # webvpn：登录学校 WebVPN（事务中心用）


class QrStartIn(BaseModel):
    realm: Literal["main", "webvpn"] = "main"

    def __repr__(self) -> str:  # 防止密码因为日志/异常打印而泄露
        return f"PasswordIn(username={self.username!r}, password=***)"

    __str__ = __repr__


class MfaStartIn(BaseModel):
    target: str = Field(default="https://www1.szu.edu.cn/board/", max_length=500)


class MfaSendIn(BaseModel):
    method: str = Field(pattern=r"^[234]$")


class MfaSubmitIn(BaseModel):
    method: str = Field(pattern=r"^[234]$")
    secret: str = Field(min_length=1, max_length=128)  # 验证码或密码

    def __repr__(self) -> str:
        return f"MfaSubmitIn(method={self.method!r}, secret=***)"

    __str__ = __repr__


class ContactIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)

    def __repr__(self) -> str:
        return f"ContactIn(phone={mask_phone(self.phone)!r})"

    __str__ = __repr__


class ResumeIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    approved: bool
    reason: str | None = Field(default=None, max_length=200)
    pickup: str | None = Field(default=None, pattern=r"^\d{1,6}$")  # 预借确认卡片上点选的取书地点
    slots: list[Annotated[int, Field(ge=0, le=47)]] | None = Field(default=None, max_length=8)  # 公共空间卡片上点选的时段格号


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _tool_result(msg: ToolMessage) -> tuple[bool, dict[str, Any]]:
    """(是否成功, 错误信息)。"""
    text = message_text(msg)
    try:
        body = json.loads(text)
        if isinstance(body, dict) and "ok" in body:
            return bool(body["ok"]), body.get("error") or {}
    except (json.JSONDecodeError, TypeError):
        pass
    return msg.status != "error" and '"ok":false' not in text.replace(" ", ""), {}


def _tool_ok(msg: ToolMessage) -> bool:
    return _tool_result(msg)[0]


def _booking_from_reserve(msg: ToolMessage) -> dict[str, Any] | None:
    """sports_reserve_confirm 成功的结果里取出付款卡片需要的信息。"""
    try:
        data = json.loads(message_text(msg)).get("data") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if not data.get("submitted") or not data.get("orderNo"):
        return None
    return {"orderNo": data["orderNo"], "campus": data.get("campus"), "project": data.get("venue"),
            "field": data.get("field"), "date": data.get("date"), "slot": (data.get("selected") or {}).get("label")}


def _shuttle_map_from(msg: ToolMessage) -> dict[str, Any] | None:
    """查小巴（shuttle_status）或导航建议坐小巴（campus_navigate）时，前端画地图卡片要的焦点信息。
    只有校区、站名、方向这些公开信息；车辆位置卡片自己去 /api/shuttle/live 拉。"""
    if msg.name not in ("shuttle_status", "campus_navigate"):
        return None
    try:
        data = json.loads(message_text(msg)).get("data") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if msg.name == "shuttle_status":
        return {"campus": data["campus"], "stop": data.get("stop")} if data.get("campus") else None
    plan = data.get("shuttle")
    if not plan or not str(data.get("advice", "")).startswith("可以坐小巴"):
        return None  # 建议走路时不画，免得看起来像在推荐坐车
    return {"campus": (data.get("from") or {}).get("campus"), "board": plan.get("board"), "alight": plan.get("alight"),
            "via": plan.get("via") or [], "line": plan.get("line") or plan.get("direction")}


async def graph_events(graph, inputs: Any, config: dict) -> AsyncIterator[dict[str, Any]]:
    """把 LangGraph 的两种流合并成前端事件：
    messages 模式给出逐字 token；updates 模式给出每个节点完成后的完整消息和中断。"""
    try:
        async for mode, chunk in graph.astream(inputs, config, stream_mode=["messages", "updates"]):
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "agent" and isinstance(msg, (AIMessageChunk, AIMessage)):
                    text = msg.content if isinstance(msg.content, str) else message_text(msg)
                    if text:
                        yield ({"type": "token", "text": text})
                continue

            for node, payload in chunk.items():
                if node == "__interrupt__":
                    yield ({**payload[0].value, "type": "confirm"})  # type 放最后，避免被中断数据里的同名字段覆盖
                    continue
                for msg in (payload or {}).get("messages", []) if isinstance(payload, dict) else []:
                    if isinstance(msg, AIMessage):
                        for tc in msg.tool_calls:
                            yield ({"type": "tool_start", "id": tc["id"], "name": tc["name"], "args": tc["args"]})
                        if not msg.tool_calls and msg.content:
                            yield ({"type": "message", "text": message_text(msg)})
                    elif isinstance(msg, ToolMessage):
                        ok, err = _tool_result(msg)
                        event = {"type": "tool_end", "id": msg.tool_call_id, "name": msg.name, "ok": ok}
                        if err.get("code"):
                            event["code"] = err["code"]
                        if err.get("code") == "MFA_REQUIRED":
                            event["target"] = err.get("target")
                        if ok and msg.name == "sports_reserve_confirm" and (booking := _booking_from_reserve(msg)):
                            event["booking"] = booking  # 前端据此弹出付款卡片
                        if ok and (sm := _shuttle_map_from(msg)):
                            event["shuttleMap"] = sm
                        yield (event)
    except Exception as exc:  # 不让一次 LLM/网络错误把连接直接断掉，前端需要知道发生了什么
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}


HEARTBEAT_SECONDS = 10


class TurnRunner:
    """让一轮对话在后台任务里跑完，不依赖浏览器连接。

    之前 SSE 生成器直接驱动 graph.astream：AI 推理或查学校系统时长时间没有输出，
    中间的代理/端口转发把“空闲”连接掐断 -> 生成器被取消 -> 这一轮对话跑到一半就没了，
    还会在历史里留下没有结果的 tool_call。现在：
      - 对话在独立的 asyncio.Task 里运行，连接断了也会跑完并写进 checkpoint
      - SSE 只是从队列里读事件，空闲时每 10 秒发一次心跳注释，避免被当成死连接
      - 同一个对话同一时间只允许一轮在跑
    """

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}

    def busy(self, thread_id: str) -> bool:
        return thread_id in self.tasks

    def start(self, thread_id: str, graph, inputs: Any, config: dict) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                async for event in graph_events(graph, inputs, config):
                    queue.put_nowait(event)
            finally:
                queue.put_nowait({"type": "done"})
                self.tasks.pop(thread_id, None)

        self.tasks[thread_id] = asyncio.create_task(run())
        return queue

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()


async def sse_from_queue(queue: asyncio.Queue, heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), heartbeat)
        except asyncio.TimeoutError:
            yield ": ping\n\n"  # SSE 注释行：浏览器会忽略，但能让中间的代理知道连接还活着
            continue
        yield _sse(event)
        if event["type"] == "done":
            return




def _stale(checkpoint_time: str | None) -> bool:
    from ..mcp_server import PendingStore

    try:
        made = datetime.fromisoformat(checkpoint_time) if checkpoint_time else None
    except ValueError:
        return False
    return made is not None and (datetime.now(made.tzinfo) - made).total_seconds() > PendingStore.TTL


def _config(thread_id: str, user_id: str = users.LOCAL) -> dict:
    # thread_id 加上用户前缀（多人模式）：别人的对话 id 在你名下根本不存在；user_id 供记忆、用量、工具调用使用
    return {"configurable": {"thread_id": users.thread_key(user_id, thread_id), "user_id": user_id}, "recursion_limit": 30}


async def _call_tool(runtime: AgentRuntime, name: str, args: dict, user_id: str = users.LOCAL) -> dict:
    """不经过模型、直接调用工具（首页面板、登录状态）。同样要说明“替谁调用”。"""
    tool = runtime.tools.get(name)
    if tool is None:
        return {"ok": False, "error": {"code": "TOOL_MISSING", "message": name}}
    token = users.current_user.set(user_id)
    try:
        raw = await tool.ainvoke(args)
        text = raw if isinstance(raw, str) else "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
        return json.loads(text)
    except Exception as exc:
        return {"ok": False, "error": {"code": "TOOL_FAILED", "message": str(exc)}}
    finally:
        users.current_user.reset(token)


def create_app(*, mock: bool | None = None, llm: BaseChatModel | None = None, profile: ProfileStore | None = None) -> FastAPI:
    load_dotenv()
    mock = mock if mock is not None else os.environ.get("SZU_AGENT_MOCK") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 整个服务共享一个 MCP 长连接；不同对话靠 thread_id 隔离，不同用户靠请求元数据里的 user_id 隔离
        async with open_runtime(mock=mock, llm=llm, profile=profile) as runtime:
            app.state.runtime = runtime
            yield
            await turns.shutdown()
            await pool.aclose()

    app = FastAPI(title="爱小荔", lifespan=lifespan)
    turns = TurnRunner()
    multi = users.multiuser()
    accounts = AccountStore() if multi else None
    # Web 进程自己的校园客户端（每个用户一个）：只用于附件这类需要把文件直接交给浏览器的场景，
    # 和 MCP 进程共享同一份登录凭证文件
    pool = CampusPool(mock=mock)
    # 进行中的扫码登录：loginId -> (CasClient, 创建时间, 多人模式下的临时凭证文件)。只在内存里
    qr_logins: dict[str, tuple[Any, float, Path | None]] = {}

    def rt() -> AgentRuntime:
        return app.state.runtime

    # ------------------------------ 当前用户 ------------------------------ #
    # 单人模式：永远是 local（不需要应用登录，和以前一样）。
    # 多人模式：只认我们签发的会话 Cookie；没有就 401，前端弹出登录卡片。

    def optional_user(request: Request) -> str | None:
        if not multi:
            return users.LOCAL
        return accounts.resolve(request.cookies.get(COOKIE))

    def current_user(request: Request) -> str:
        uid = optional_user(request)
        if uid is None:
            raise HTTPException(401, detail={"code": "APP_LOGIN_REQUIRED", "message": "请先登录爱小荔"})
        return uid

    # ------------------------------ 每日额度 ------------------------------ #
    # 每人每天能花多少元的模型费用（北京时间 0 点清零）。多人模式默认 0.5 元，单人模式默认不限；
    # SZU_AGENT_DAILY_QUOTA_CNY 可改，0 表示不限。花费按 usage.sqlite 里写入时算好的金额累计（含压缩历史）。
    # 只拦新消息、不拦“确认”：预览完正要确认时额度刚好用完，不能把用户卡在确认卡片上。
    # 一轮对话里可能连续调用好几次模型，所以实际花费会略超额度（通常几分钱以内）。
    quota_cny = float(os.environ.get("SZU_AGENT_DAILY_QUOTA_CNY") or (0.5 if multi else 0))

    def quota_of(user_id: str) -> dict[str, Any]:
        from ..agent.usage import beijing_day_start

        if quota_cny <= 0:
            return {"enabled": False}
        used = rt().usage.spent_today(user_id)
        reset = beijing_day_start() + timedelta(days=1)
        return {"enabled": True, "limitCny": quota_cny, "usedCny": round(used, 4),
                "remainingCny": round(max(quota_cny - used, 0), 4), "exhausted": used >= quota_cny,
                "resetAt": reset.isoformat(timespec="seconds")}

    def store_of(user_id: str) -> SessionStore:
        return SessionStore(users.session_path(user_id, mock=mock))

    def contacts_of(user_id: str) -> ContactStore:
        return ContactStore(users.contact_path(user_id))

    def set_session_cookie(response: Response, request: Request, token: str) -> None:
        secure = os.environ.get("SZU_AGENT_COOKIE_SECURE") == "1" or request.url.scheme == "https"
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)

    async def finish_login(temp: SessionStore, request: Request, response: Response) -> None:
        """多人模式：学校登录成功后识别是谁，把学校凭证搬进这个人的目录，签发应用会话。
        学号只在这里出现一次：算出 user_id 就丢掉，不落盘、不进日志。"""
        probe = make_campus(mock=mock, store=temp)
        try:
            student_no = await probe.whoami()
        finally:
            if (http := getattr(probe, "http", None)) is not None:
                await http.aclose()
        uid = users.user_id_for(student_no)
        del student_no
        dest = users.session_path(uid, mock=mock)
        users.private_dir(dest.parent.parent)  # users/
        users.private_dir(dest.parent)         # users/<id>/
        os.replace(temp.path, dest)
        pool.drop(uid)  # 旧客户端里可能缓存着过期会话
        set_session_cookie(response, request, accounts.login(uid))

    # 多人模式登录时“识别身份”这一步遇到学校二次验证：mfaId -> 临时凭证文件。
    # 这时还没有应用会话（正是要靠这次登录建立），所以这类验证流程不要求登录爱小荔，
    # 凭 mfaId（随机、只发给发起登录的那个浏览器）继续；验证通过后再识别身份、签发会话。
    login_mfa: dict[str, SessionStore] = {}

    async def identify(temp: SessionStore, request: Request, response: Response) -> dict[str, Any]:
        """登录成功后识别身份。服务器 IP 对学校来说是“非常用地点”，第一次读学号时学校通常要求二次验证（2026-11-16 部署后实测）。"""
        try:
            await finish_login(temp, request, response)
            return {}
        except CampusError as exc:
            if exc.code != "MFA_REQUIRED":
                raise
            target = exc.details.get("target") or "https://ehall.szu.edu.cn/new/index.html"
        client = make_mfa(mock=mock, store=temp)
        try:
            info = await client.start(target)
        except CampusError:
            await client.aclose()
            raise
        if not info.get("needed"):  # 学校这次又不要求了：直接再识别一次
            await client.aclose()
            await finish_login(temp, request, response)
            return {}
        mfa_id = secrets.token_urlsafe(16)
        mfa_flows[mfa_id] = (client, time.monotonic(), None)
        login_mfa[mfa_id] = temp
        return {"mfa": {"mfaId": mfa_id, **info}}

    def pending_store() -> SessionStore:
        """多人模式登录过程中的临时凭证文件：还不知道是谁，先放这里，识别后再搬走。"""
        return SessionStore(users.private_dir(users.agent_home() / "pending") / f"{secrets.token_hex(8)}.json")

    @app.get("/")
    async def index():
        # 给脚本和样式加上按内容计算的版本号：改了代码刷新页面就能拿到新文件，
        # 不会出现“页面是新的、脚本还是浏览器缓存里的旧版本”导致功能失效
        import hashlib

        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "scene.js", "style.css"):
            ver = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
            html = html.replace(f"/static/{name}\"", f"/static/{name}?v={ver}\"")
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            if request.query_params.get("v"):
                # 带内容哈希的地址：内容变了地址就变，可以放心长期缓存（省掉每次打开页面的跨境确认往返）
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                resp.headers["Cache-Control"] = "no-cache"  # 可以缓存，但每次先向服务器确认有没有更新
        return resp

    @app.get("/api/meta")
    async def meta():
        from ..agent.threads import retention_days

        out = {"mock": mock, "historyDays": retention_days(), "multiuser": multi}
        if mock and (real := os.environ.get("SZU_AGENT_REAL_URL")):
            out["realUrl"] = real  # 演示站上给出真实服务的地址（部署脚本写入）
        return out

    def _stream(thread_id: str, inputs: Any, user_id: str) -> StreamingResponse:
        config = _config(thread_id, user_id)
        queue = turns.start(config["configurable"]["thread_id"], rt().graph, inputs, config)
        # X-Accel-Buffering: 让 nginx 一类的反向代理不要缓冲事件流
        return StreamingResponse(sse_from_queue(queue), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/quota")
    async def quota(user: str = Depends(current_user)):
        return quota_of(user)

    @app.post("/api/chat")
    async def chat(body: ChatIn, user: str = Depends(current_user)):
        if turns.busy(users.thread_key(user, body.thread_id)):
            raise HTTPException(409, "上一条消息还在处理中")
        if (q := quota_of(user)).get("exhausted"):
            raise HTTPException(429, detail={"code": "DAILY_QUOTA_EXCEEDED",
                                             "message": f"今天的额度（¥{q['limitCny']:g}）用完了，北京时间明天 0 点恢复"})
        await rt().threads.touch(body.thread_id, body.message, user)
        return _stream(body.thread_id, {"messages": [HumanMessage(body.message)]}, user)

    @app.post("/api/resume")
    async def resume(body: ResumeIn, user: str = Depends(current_user)):
        if turns.busy(users.thread_key(user, body.thread_id)):
            raise HTTPException(409, "上一条消息还在处理中")
        state = await rt().graph.aget_state(_config(body.thread_id, user))
        if not state.interrupts:
            raise HTTPException(409, "这个对话没有等待确认的操作")
        return _stream(body.thread_id, Command(resume={"approved": body.approved, "reason": body.reason, "pickup": body.pickup, "slots": body.slots}), user)

    @app.get("/api/history/{thread_id}")
    async def history(thread_id: str, user: str = Depends(current_user)):
        state = await rt().graph.aget_state(_config(thread_id, user))
        items: list[dict[str, Any]] = []
        start_id = (state.values or {}).get("context_start_id")
        for msg in state.values.get("messages", []) if state.values else []:
            if start_id and msg.id == start_id:
                items.append({"role": "compact"})  # 这条之前的内容在模型那边已经只剩摘要
            if isinstance(msg, HumanMessage):
                items.append({"role": "user", "text": message_text(msg), "sentAt": msg.additional_kwargs.get("sent_at")})
            elif isinstance(msg, AIMessage):
                if msg.content:
                    items.append({"role": "assistant", "text": message_text(msg)})
                for tc in msg.tool_calls:
                    items.append({"role": "tool", "id": tc["id"], "name": tc["name"], "args": tc["args"]})
            elif isinstance(msg, ToolMessage):
                for item in items:
                    if item.get("id") == msg.tool_call_id:
                        item["ok"] = _tool_ok(msg)
                        if item["ok"] and msg.name == "sports_reserve_confirm" and (booking := _booking_from_reserve(msg)):
                            item["booking"] = booking
                        if item["ok"] and (sm := _shuttle_map_from(msg)):
                            item["shuttleMap"] = sm
        pending = state.interrupts[0].value if state.interrupts else None
        if pending and _stale(state.created_at):
            # 确认令牌只在 MCP Server 内存里存 10 分钟（重启就没了）。过期的确认卡片不再显示，
            # 用户发新消息时 repair_tool_pairs 会把这次操作记为“未执行”
            pending = None
        pending_tools = {a["tool"] for a in (pending or {}).get("actions", [])}
        for item in items:
            # 没有结果、也不是正在等确认的调用：说明执行被中断了，标成失败而不是让界面一直转圈
            if item.get("role") == "tool" and "ok" not in item and item["name"] not in pending_tools:
                item["ok"] = False
        values = state.values or {}
        return {"messages": items, "pending": pending, "running": turns.busy(users.thread_key(user, thread_id)),
                "context": context_stats(values.get("messages", []), values.get("context_start_id"), values.get("summary", ""))}

    @app.get("/api/threads")
    async def threads(user: str = Depends(current_user)):
        return {"items": await rt().threads.list(user)}

    @app.get("/usage")
    async def usage_page():
        return Response((STATIC / "usage.html").read_text(encoding="utf-8"), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/api/usage")
    async def usage_report(days: int = Query(7, ge=1, le=365), user: str = Depends(current_user)):
        """模型用量统计：token、缓存命中率、花费。
        多人模式只给管理员看（SZU_AGENT_ADMINS 里列出的 user_id），而且不显示对话标题——标题是用户写的内容，管理员也不该看。"""
        import asyncio

        from ..agent import pricing

        if multi and user not in {x.strip() for x in os.environ.get("SZU_AGENT_ADMINS", "").split(",") if x.strip()}:
            raise HTTPException(403, "只有管理员能查看用量统计")
        report = await asyncio.to_thread(rt().usage.report, days)
        titles = {} if multi else {t["id"]: t["title"] for t in await rt().threads.list(limit=500)}
        for t in report["threads"]:
            t["title"] = titles.get(t["thread_id"], "（对话 " + t["thread_id"][-6:] + "）" if multi else "（已删除的对话）")
        if multi:
            report["users"] = accounts.count_users()
            for u in report["byUser"]:  # 只给管理员看 id 前 8 位：够区分人，又不必把完整 id 摆出来
                u["user"] = u.pop("user_id")[:8]
        else:
            report.pop("byUser", None)
        report["quota"] = {"limitCny": quota_cny} if quota_cny > 0 else None
        model = os.environ.get("LLM_MODEL", "deepseek-chat")
        report["pricing"] = {"model": model, "priced": pricing.price_for(model) is not None,
                             "source": pricing.SOURCE_URL, "verifiedAt": pricing.VERIFIED_AT,
                             "note": "按官方价格估算；工作日 9-12、14-18 点（北京时间）是高峰价，法定节假日实际按空闲价，这里会略微高估。"
                                     "以 DeepSeek 后台账单为准。"}
        return report

    @app.delete("/api/threads/{thread_id}")
    async def delete_thread(thread_id: str, user: str = Depends(current_user)):
        if turns.busy(users.thread_key(user, thread_id)):
            raise HTTPException(409, "这个对话还在处理中")
        await rt().threads.delete(thread_id, user)
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        uid = optional_user(request)
        return {"multiuser": multi, "loggedIn": uid is not None, "userId": uid if multi else None}

    @app.delete("/api/me")
    async def delete_me(request: Request, response: Response, user: str = Depends(current_user)):
        """删除我的全部数据：对话历史、记忆、联系手机、学校登录凭证、用量记录、应用账号。不可恢复。"""
        if not multi:
            raise HTTPException(400, "单人模式的数据就在你自己电脑上，直接删除数据目录即可")
        if any(k.startswith(user + ":") for k in turns.tasks):
            raise HTTPException(409, "还有对话在处理中，稍后再试")
        await rt().threads.delete_user(user)
        rt().usage.delete_user(user)
        pool.drop(user)
        users.delete_user_files(user)
        accounts.delete_user(user)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/today")
    async def today(user: str = Depends(current_user)):
        """首页“今日荔园”面板。直接调用工具，不经过 LLM：确定性的数据没必要让模型转述。"""
        runtime = rt()
        out: dict[str, Any] = {"courses": await _call_tool(runtime, "course_today", {}, user)}
        p = runtime.profile(user).load()
        if p.get("dorm_building") and p.get("dorm_room"):
            out["dorm"] = {"building": p["dorm_building"], "room": p["dorm_room"]}
            out["electricity"] = await _call_tool(
                runtime, "electricity_query", {"building": p["dorm_building"], "room": p["dorm_room"]}, user
            )
        return out

    # ------------------------------ 登录 ------------------------------ #

    @app.get("/api/auth")
    async def auth_status(request: Request):
        uid = optional_user(request)
        if uid is None:  # 多人模式还没登录爱小荔
            return {"loggedIn": False, "appSession": False}
        res = await _call_tool(rt(), "auth_status", {}, uid)
        data = res.get("data") if res.get("ok") else {"loggedIn": False, **store_of(uid).info(), "error": res.get("error")}
        return {**data, "appSession": True}

    async def _handoff_mfa(cas, store: SessionStore) -> dict[str, Any] | None:
        """WebVPN 登录停在二次验证页：把同一个会话交给二次验证流程（换客户端会“认证失败”，见 cas.CasClient）。
        返回给前端的 mfa 信息（mfaId + 验证方式），之后走 /api/auth/mfa/{mfaId}/send|submit。"""
        resp = getattr(cas, "mfa_response", None)
        if resp is None:
            return None
        from ..campus.mfa import MfaClient

        client, info = await MfaClient.adopt(store, cas.client, resp)
        if not info.get("needed"):
            await client.aclose()
            return None
        mfa_id = secrets.token_urlsafe(8)
        mfa_flows[mfa_id] = (client, time.monotonic(), uid_of_store(store))
        return {"mfaId": mfa_id, **info}

    def login_store(realm: str, uid: str | None) -> tuple[SessionStore, bool]:
        """登录要写到哪个凭证文件。返回 (store, 是否需要登录后识别身份)。
        多人模式的主登录：还不知道是谁 → 临时文件，登录成功后 finish_login 识别并搬走。
        WebVPN 登录：是给已登录的人补登 WebVPN，必须已经有应用会话。"""
        if not multi:
            return store_of(users.LOCAL), False
        if realm == "webvpn":
            if uid is None:
                raise HTTPException(401, detail={"code": "APP_LOGIN_REQUIRED", "message": "请先登录爱小荔"})
            return store_of(uid), False
        return pending_store(), True

    def uid_of_store(store: SessionStore) -> str | None:
        parent = store.path.parent.name
        return parent if users.valid_user_id(parent) and parent != users.LOCAL else (users.LOCAL if not multi else None)

    async def _drop_qr(login_id: str) -> None:
        cas, _, temp = qr_logins.pop(login_id)
        await cas.aclose()
        if temp is not None:
            temp.unlink(missing_ok=True)

    @app.post("/api/auth/qr")
    async def qr_start(request: Request, body: QrStartIn | None = None):
        now = time.monotonic()
        for k in [k for k, (_, t, _p) in qr_logins.items() if now - t > 300]:  # 二维码 5 分钟没扫就丢掉
            await _drop_qr(k)
        realm = (body or QrStartIn()).realm
        store, identify = login_store(realm, optional_user(request))
        cas = make_cas(mock=mock, store=store, realm=realm)
        try:
            image = await cas.qr_image()
        except CampusError as exc:
            await cas.aclose()
            return {"ok": False, "error": exc.to_dict()}
        login_id = secrets.token_urlsafe(8)
        qr_logins[login_id] = (cas, now, store.path if identify else None)
        return {"ok": True, "loginId": login_id, "image": image}

    @app.get("/api/auth/qr/{login_id}")
    async def qr_poll(login_id: str, request: Request, response: Response):
        entry = qr_logins.get(login_id)
        if not entry:
            return {"ok": True, "state": "expired"}
        cas, _, temp = entry
        try:
            state = await cas.qr_poll()
        except CampusError as exc:
            await _drop_qr(login_id)
            return {"ok": False, "error": exc.to_dict()}
        if state in ("confirmed", "expired"):
            qr_logins.pop(login_id, None)
            try:
                if state == "confirmed" and temp is not None:
                    await cas.aclose()
                    extra = await identify(cas.store, request, response)
                    if extra:  # 临时凭证还要留给二次验证用，不能删
                        temp = None
                    return {"ok": True, "state": state, **extra}
                mfa = await _handoff_mfa(cas, cas.store) if state == "confirmed" else None
            except CampusError as exc:
                await cas.aclose()
                if temp is not None and not any(st.path == temp for st in login_mfa.values()):
                    temp.unlink(missing_ok=True)
                return {"ok": False, "error": exc.to_dict()}
            if mfa:  # 客户端已经交给二次验证流程，不能关
                return {"ok": True, "state": state, "mfa": mfa}
            await cas.aclose()
            if temp is not None:
                temp.unlink(missing_ok=True)
        return {"ok": True, "state": state}

    @app.post("/api/auth/password")
    async def password_login(body: PasswordIn, request: Request, response: Response):
        store, needs_identify = login_store(body.realm, optional_user(request))
        cas = make_cas(mock=mock, store=store, realm=body.realm)
        handed_off = keep_temp = False
        try:
            await cas.password_login(body.username, body.password, remember=body.remember)
            if needs_identify:
                extra = await identify(store, request, response)
                if extra:
                    keep_temp = True
                return {"ok": True, **extra}
            mfa = await _handoff_mfa(cas, store)
            handed_off = mfa is not None
            return {"ok": True, **({"mfa": mfa} if mfa else {})}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}
        finally:
            if needs_identify and not keep_temp:
                store.path.unlink(missing_ok=True)  # 成功时已经搬走；失败时清掉临时文件
            if not handed_off:
                await cas.aclose()

    # 二次验证：mfaId -> (MfaClient, 创建时间, 发起人)。只有发起人能继续这个流程
    mfa_flows: dict[str, tuple[Any, float, str | None]] = {}

    def _mfa(mfa_id: str, user: str | None):
        entry = mfa_flows.get(mfa_id)
        if not entry or (mfa_id not in login_mfa and (user is None or entry[2] != user)):
            raise HTTPException(404, "二次验证已过期，请重新开始")
        return entry[0]

    @app.post("/api/auth/mfa")
    async def mfa_start(body: MfaStartIn, user: str = Depends(current_user)):
        # 只允许学校的地址：验证流程会带着登录凭证去访问 target，不能被拿去请求任意地址
        u = urlparse(body.target)
        if u.scheme not in ("https", "http") or not (u.hostname or "").endswith(".szu.edu.cn"):
            raise HTTPException(400)
        now = time.monotonic()
        for k in [k for k, (_, t, _u) in mfa_flows.items() if now - t > 600]:
            client, _, _ = mfa_flows.pop(k)
            await client.aclose()
            if (temp := login_mfa.pop(k, None)) is not None:
                temp.path.unlink(missing_ok=True)
        client = make_mfa(mock=mock, store=store_of(user))
        try:
            info = await client.start(body.target)
        except CampusError as exc:
            await client.aclose()
            return {"ok": False, "error": exc.to_dict()}
        if not info["needed"]:
            await client.aclose()
            return {"ok": True, "needed": False}
        mfa_id = secrets.token_urlsafe(8)
        mfa_flows[mfa_id] = (client, now, user)
        return {"ok": True, "mfaId": mfa_id, **info}

    @app.post("/api/auth/mfa/{mfa_id}/send")
    async def mfa_send(mfa_id: str, body: MfaSendIn, request: Request):
        try:
            return {"ok": True, "message": await _mfa(mfa_id, optional_user(request)).send_code(body.method)}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}

    @app.post("/api/auth/mfa/{mfa_id}/submit")
    async def mfa_submit(mfa_id: str, body: MfaSubmitIn, request: Request, response: Response):
        user = optional_user(request)
        client = _mfa(mfa_id, user)
        try:
            res = await client.submit(body.method, body.secret) or {}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 —— 不让界面只剩一句“验证失败”；异常信息可能含地址，只记类型
            from ..campus.debuglog import log as campus_log

            campus_log().info("mfa submit 异常 %s", type(exc).__name__)
            return {"ok": False, "error": {"code": "MFA_ERROR", "message": f"二次验证时程序出错（{type(exc).__name__}），请运行 szu-agent log 把输出贴出来"}}
        if res.get("needed"):  # 学校还要再验证一步：同一个流程继续，卡片换成下一步的验证方式
            mfa_flows[mfa_id] = (client, time.monotonic(), user)
            return {"ok": True, **res}
        mfa_flows.pop(mfa_id, None)
        await client.aclose()
        if (temp := login_mfa.pop(mfa_id, None)) is not None:
            # 登录时的二次验证通过了：现在再识别身份、签发会话
            try:
                await finish_login(temp, request, response)
            except CampusError as exc:
                temp.path.unlink(missing_ok=True)
                return {"ok": False, "error": exc.to_dict()}
            return {"ok": True, "appLogin": True}
        return {"ok": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response, user: str = Depends(current_user)):
        """退出登录：删除这个人在服务器上的学校登录凭证；多人模式同时注销应用会话。"""
        store_of(user).clear()
        pool.drop(user)
        if multi:
            accounts.logout(request.cookies.get(COOKIE))
            response.delete_cookie(COOKIE)
        return {"ok": True}

    # ------------------------------ 体育预约付款 ------------------------------ #

    @app.get("/api/sports/payment/{order_no}")
    async def sports_payment(order_no: str, user: str = Depends(current_user)):
        """付款卡片轮询：订单状态 + 体育经费余额。付款本身只能在学校页面完成（支付页禁止嵌入，密码用随机键盘）。"""
        if not re.fullmatch(r"[A-Za-z0-9]{6,40}", order_no):
            raise HTTPException(400)
        try:
            return {"ok": True, **await pool.get(user).sports_payment_info(order_no)}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}

    @app.post("/api/sports/cancel/{order_no}")
    async def sports_cancel(order_no: str, user: str = Depends(current_user)):
        """付款卡片上的“取消预约”按钮。用户在界面上亲手点击（并二次确认）才会调用，不经过 LLM；
        和 agent 的取消一样，先核对学校是否允许取消，取消后再查询确认。"""
        if not re.fullmatch(r"[A-Za-z0-9]{6,40}", order_no):
            raise HTTPException(400)
        try:
            return {"ok": True, **await pool.get(user).sports_cancel(order_no, confirm=True)}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}

    # ------------------------------ 公文通附件 ------------------------------ #

    @app.get("/api/notices/{notice_id}/attachments")
    async def notice_attachments(notice_id: str, user: str = Depends(current_user)):
        if not notice_id.isdigit():
            raise HTTPException(400)
        try:
            d = await pool.get(user).notice_view(notice_id)
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}
        return {"ok": True, "attachments": [{"name": a["name"], "url": a["url"]} for a in d.get("attachments", [])]}

    @app.get("/api/notices/attachment")
    async def notice_attachment(url: str, name: str = "附件", id: str | None = None, user: str = Depends(current_user)):
        try:
            content, ctype = await pool.get(user).notice_attachment(url, id)
        except CampusError as exc:
            status = 403 if exc.code == "PERMISSION_DENIED" else 401 if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED") else 502
            return Response(exc.message, status_code=status, media_type="text/plain; charset=utf-8")
        safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", name).strip() or "附件"
        # filename* 让中文文件名在各浏览器里都能正确保存
        disposition = f"attachment; filename=\"attachment\"; filename*=UTF-8''{quote(safe)}"
        return Response(content, media_type=ctype, headers={"Content-Disposition": disposition})

    @app.get("/api/electricity/recharge")
    async def electricity_recharge(building: str | None = None, room: str | None = None, user: str = Depends(current_user)):
        """充值卡片：剩余电量、日均用电、充多少能用几天，以及微信充值页的二维码。
        付款只能在微信里完成（JSAPI 支付），这里不下单、不接触任何支付信息。"""
        from ..campus.recharge import PAY_STEPS, PAY_URL, qr_svg, recharge_plan

        p = rt().profile(user).load()
        building, room = building or p.get("dorm_building"), room or p.get("dorm_room")
        out: dict[str, Any] = {"ok": True, "payUrl": PAY_URL, "qrSvg": qr_svg(PAY_URL), "paySteps": PAY_STEPS,
                               "building": building, "room": room}
        remaining, records = None, []
        if building and room:
            try:
                d = await pool.get(user).electricity_query(building, room, p.get("dorm_campus"))
                remaining, records = d.get("remainingKwh"), d.get("records") or []
            except CampusError as exc:
                out["error"] = exc.to_dict()  # 查不到电量（比如不在校园网）也照样给二维码
        out.update(recharge_plan(remaining, records))
        return out

    @app.get("/api/weather")
    async def weather(request: Request, campus: str | None = None):
        """左侧“今日荔园”：校区天气。公开数据，不需要登录。"""
        from ..campus.weather import campus_for, get_weather, mock_weather

        uid = optional_user(request)  # 天气是公开数据：没登录也能看，只是不知道你常去哪个校区
        p = rt().profile(uid).load() if uid else {}
        name = campus or campus_for(" ".join(p.get(k, "") for k in ("dorm_campus", "preferred_sports_campus")))
        if mock:
            return {"ok": True, **mock_weather(name)}
        try:
            return {"ok": True, **await get_weather(name)}
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}

    @app.get("/api/shuttle/map")
    async def shuttle_map(campus: str):
        """小巴地图卡片的静态几何。公开数据，不需要登录（和天气一样）。"""
        from ..knowledge.campusmap import default_map

        if campus not in ("粤海", "丽湖"):
            raise HTTPException(400, "campus 只能是 粤海 或 丽湖")
        return {"ok": True, **default_map().map_view(campus)}

    @app.get("/api/shuttle/live")
    async def shuttle_live(campus: str):
        """小巴实时位置。公开数据；上游有 10 秒缓存，所以不管多少人开着卡片，每个校区每 10 秒最多请求一次上游。"""
        from ..campus.shuttle import get_vehicles, mock_vehicles
        from ..knowledge.campusmap import default_map

        if campus not in ("粤海", "丽湖"):
            raise HTTPException(400, "campus 只能是 粤海 或 丽湖")
        try:
            vehicles = mock_vehicles(campus) if mock else await get_vehicles(campus)
        except CampusError as exc:
            return {"ok": False, "error": exc.to_dict()}
        return {"ok": True, "vehicles": default_map().vehicle_points(campus, vehicles)}

    @app.get("/api/contact")
    async def get_contact(user: str = Depends(current_user)):
        return {"phone": mask_phone(contacts_of(user).phone())}

    @app.put("/api/contact")
    async def put_contact(body: ContactIn, user: str = Depends(current_user)):
        if not PHONE_RE.match(body.phone):
            raise HTTPException(400, "手机号格式不对")
        contacts_of(user).set_phone(body.phone)
        return {"ok": True, "phone": mask_phone(body.phone)}

    @app.delete("/api/contact")
    async def delete_contact(user: str = Depends(current_user)):
        contacts_of(user).set_phone(None)
        return {"ok": True}

    @app.get("/api/profile")
    async def get_profile(user: str = Depends(current_user)):
        data = rt().profile(user).load()
        return {"items": [{"key": k, "label": PROFILE_LABELS[k], "value": v} for k, v in data.items()]}

    @app.delete("/api/profile/{key}")
    async def delete_profile(key: str, user: str = Depends(current_user)):
        if key not in PROFILE_LABELS:
            raise HTTPException(404)
        rt().profile(user).set(key, None)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    import argparse

    import uvicorn

    load_dotenv()
    parser = argparse.ArgumentParser(description="爱小荔 Web 界面")
    parser.add_argument("--mock", action="store_true", help="使用 mock 数据")
    parser.add_argument("--host", default=default_host(),
                        help="监听地址。默认 127.0.0.1；在 Docker 容器里默认 0.0.0.0（否则端口映射进不来）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SZU_AGENT_PORT", "8000")))
    args = parser.parse_args()

    if not port_available(args.host, args.port):
        print(f"端口 {args.port} 已被其他程序占用（可能是之前开的爱小荔还没关）。", file=sys.stderr)
        print(f"  换个端口：szu-agent-web --port {args.port + 1}", file=sys.stderr)
        if sys.platform == "win32":
            print(f"  查看占用者：Get-Process -Id (Get-NetTCPConnection -LocalPort {args.port}).OwningProcess", file=sys.stderr)
        else:
            print(f"  查看占用者：ss -ltnp | grep :{args.port}", file=sys.stderr)
        raise SystemExit(1)

    # 0.0.0.0 是“监听所有网卡”，不是能在浏览器里打开的地址
    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"爱小荔已启动：http://{shown}:{args.port}", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  正在监听 {args.host}：同一网络里的其他设备也能访问这个页面（它能使用你的校园登录凭证）。", flush=True)
        if in_container():
            print("  Docker 建议这样映射端口，只让本机访问：docker run -p 127.0.0.1:8000:8000 ...", flush=True)
    uvicorn.run(create_app(mock=args.mock), host=args.host, port=args.port, log_level="warning")


def port_available(host: str, port: int) -> bool:
    """启动前先试绑一次端口，避免先打印“已启动”再被 uvicorn 报 Errno 10048/98。"""
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        # 与 uvicorn 一致：Linux/macOS 设 SO_REUSEADDR（忽略 TIME_WAIT）；
        # Windows 上这个选项会允许抢占已占用端口，所以不能设
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def default_host() -> str:
    """容器里的 127.0.0.1 只对容器自己可见，Docker 端口映射转发进来的连接会被拒绝，所以容器里默认监听 0.0.0.0。"""
    if host := os.environ.get("SZU_AGENT_HOST"):
        return host
    return "0.0.0.0" if in_container() else "127.0.0.1"


if __name__ == "__main__":
    main()
