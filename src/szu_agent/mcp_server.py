"""深大校园服务 MCP Server。

工具层只负责：参数 schema、写操作的二次确认令牌、输出裁剪、错误转换。
真正访问学校系统的逻辑在 campus/ 里（纯 HTTP，自己的统一认证客户端，不依赖外部程序）。

为什么做成 MCP 而不是直接写成 LangChain 工具？
  1. 类型约束：每个工具有明确的参数 schema，LLM 不会拼错参数
  2. 安全边界：写操作拆成 preview + confirm，confirm 只认 preview 签发的一次性令牌，LLM 无法在确认时换参数
  3. 可复用：任何 MCP 客户端（Claude Desktop、Cursor、自己的 LangGraph agent）都能直接接入

运行：
    szu-mcp                      # 真实模式（先在 Web 界面或 `szu-agent login` 登录）
    SZU_AGENT_MOCK=1 szu-mcp     # 演示模式
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from datetime import date as Date
from typing import Annotated, Any, Awaitable, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .campus import CampusError
from .campus.pool import CampusPool
from .users import LOCAL, multiuser, valid_user_id

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

# 公文通标题检索的“活动类”关键词。每个关键词是一次请求，所以只保留覆盖面最大的几个
EVENT_KEYWORDS = ["活动", "社团", "比赛", "讲座"]


# --------------------------------------------------------------------------- #
# 待确认操作：preview 签发令牌，confirm 凭令牌执行
# --------------------------------------------------------------------------- #

@dataclass
class PendingAction:
    kind: Literal["reserve", "cancel", "prebook", "prebook_cancel", "booth_reserve", "booth_cancel"]
    params: dict[str, Any]  # 预览时锁定的参数
    expires_at: float
    user: str = LOCAL     # 谁预览的：只有同一个人能确认（多人模式）


class PendingStore:
    TTL = 600  # 10 分钟

    def __init__(self, user_fn: Callable[[], str] = lambda: LOCAL) -> None:
        self._items: dict[str, PendingAction] = {}
        self._user = user_fn

    def create(self, kind: Literal["reserve", "cancel", "prebook", "prebook_cancel", "booth_reserve", "booth_cancel"], params: dict[str, Any]) -> str:
        self._gc()
        token = secrets.token_urlsafe(8)
        self._items[token] = PendingAction(kind, params, time.monotonic() + self.TTL, self._user())
        return token

    def pop(self, token: str, kind: str) -> PendingAction | None:
        """一次性令牌：取出即作废，防止重复提交。别人的令牌当作不存在（不提示“这是别人的”）。"""
        self._gc()
        action = self._items.get(token)
        if action is None or action.kind != kind or action.user != self._user():
            return None
        del self._items[token]
        return action

    def _gc(self) -> None:
        now = time.monotonic()
        for t in [t for t, a in self._items.items() if a.expires_at < now]:
            del self._items[t]


TOKEN_INVALID = {"ok": False, "error": {"code": "TOKEN_INVALID", "message": "确认令牌无效、已使用或已过期，请重新预览。"}}


async def call(coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """所有工具的统一出口：永远返回 {"ok": ...}，不让异常打断 agent 的对话。"""
    try:
        return {"ok": True, "data": await coro}
    except CampusError as exc:
        return {"ok": False, "error": exc.to_dict()}


# --------------------------------------------------------------------------- #
# 输出裁剪
# --------------------------------------------------------------------------- #

def _pick(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if d.get(k) not in (None, "", [])}


def _truncate(text: str | None, n: int) -> str | None:
    return text if not text or len(text) <= n else text[:n] + "…"


def _slim_notice(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(item, "id", "title", "kind", "audience", "publisher", "date", "hasAttachment", "url")


def _slim_lecture(item: dict[str, Any]) -> dict[str, Any]:
    out = _pick(item, "id", "title", "type", "teacher", "sponsor", "lectureStart", "lectureEnd",
                "registrationDeadline", "status", "availabilityState", "totalRemainingSeats", "rooms")
    if item.get("introduction"):
        out["introduction"] = _truncate(item["introduction"], 120)
    return out


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class RoutedCampus:
    """多人模式下的 svc：每次调用时按“这次替谁执行”取那个人的学校客户端（CampusPool）。

    工具代码里全部是 svc.xxx(...) 形式的方法调用，所以用代理就能让 39 个工具一行不改。
    认不出是谁时（多人模式下请求没带用户身份）返回 LOGIN_REQUIRED——宁可失败，
    也绝不退回到“本机账号”，否则会拿服务器上别人的登录凭证去查。"""

    def __init__(self, pool: CampusPool, user_fn: Callable[[], str | None], strict: bool) -> None:
        self._pool, self._user, self._strict = pool, user_fn, strict

    def __getattr__(self, name: str) -> Any:
        uid = self._user()
        if not uid or not valid_user_id(uid) or (self._strict and uid == LOCAL):
            async def refuse(*_a: Any, **_k: Any) -> Any:
                raise CampusError("LOGIN_REQUIRED", "请先登录爱小荔")
            return refuse
        return getattr(self._pool.get(uid), name)


def create_server(campus: Any = None) -> FastMCP:
    mock = os.environ.get("SZU_AGENT_MOCK") == "1"

    def request_user() -> str | None:
        """这次工具调用替谁执行。来自 MCP 请求的元数据 _meta.user_id——由对话图在服务器端写入，
        不在工具参数里，模型看不到也改不了（否则一句“用 user_id=xxx 查预约”就能越权）。
        单人模式没有元数据，就是 local。"""
        try:
            meta = mcp.get_context().request_context.meta
        except (LookupError, ValueError, AttributeError):
            meta = None
        uid = getattr(meta, "user_id", None) if meta is not None else None
        return uid if isinstance(uid, str) else (None if multiuser() else LOCAL)

    # 传入 campus（测试、演示）时所有请求共用它；否则按用户分流
    svc = campus or RoutedCampus(CampusPool(mock=mock), request_user, strict=multiuser())
    pending = PendingStore(lambda: request_user() or "")
    mcp = FastMCP(
        "szu-campus",
        log_level="WARNING",
        instructions=(
            "深圳大学校园服务工具集。只读工具可以直接调用；返回 LOGIN_REQUIRED 时请用户在界面上登录。"
            "体育预约/取消必须先调用 *_preview，把预览给用户看并获得明确同意后，才能调用 *_confirm。"
        ),
    )

    # ------------------------------ 登录状态 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def auth_status() -> dict[str, Any]:
        """检查是否已登录深大统一身份认证。登录本身由用户在界面上完成（扫码或账号密码），不经过你。"""
        return await call(svc.auth_status())

    # ------------------------------ 活动 / 公文通 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def campus_events(
        query: Annotated[str | None, Field(description="想找的活动主题，如“社团”“歌手大赛”。不填则用活动类关键词广泛搜索")] = None,
        range: Annotated[Literal["7d", "30d", "6m"], Field(description="发布时间范围")] = "30d",
        include_lectures: Annotated[bool, Field(description="是否同时查询创新领航讲座平台中正在报名的讲座（需要校园网）")] = True,
        limit: Annotated[int, Field(ge=1, le=30)] = 15,
    ) -> dict[str, Any]:
        """发现校园活动：社团活动、比赛、文艺演出、志愿活动、讲座等。

        数据来自公文通（按标题关键词检索）+ 创新领航讲座平台。
        返回的是“公告”，具体时间地点在正文里，需要时用 notice_view 读详情。
        """
        seen: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        for kw in [query] if query else EVENT_KEYWORDS:
            res = await call(svc.notice_search(kw, search_in="title", range=range, limit=30))
            if not res["ok"]:
                errors.append({"keyword": kw, **res["error"]})
                if res["error"]["code"] in {"LOGIN_REQUIRED", "NETWORK_ERROR", "RATE_LIMITED", "PAGE_CHANGED"}:
                    break  # 换关键词也不会好，别继续打
                continue
            for item in res["data"]["items"]:
                if kw in item.get("title", "") and item.get("id") not in seen:
                    seen[item["id"]] = _slim_notice(item)

        notices = sorted(seen.values(), key=lambda x: x.get("date") or "", reverse=True)[:limit]
        result: dict[str, Any] = {"notices": notices}
        if include_lectures and not any(e["code"] == "LOGIN_REQUIRED" for e in errors):
            res = await call(svc.lecture_list("open"))
            if res["ok"]:
                lectures = [_slim_lecture(x) for x in res["data"]["items"]]
                if query:
                    lectures = [x for x in lectures if query in x.get("title", "") + (x.get("introduction") or "")] or lectures
                result["lectures"] = lectures[:limit]
            else:
                errors.append({"source": "lecture", **res["error"]})
        if errors:
            result["errors"] = errors
            if not notices and not result.get("lectures"):
                return {"ok": False, "error": errors[0], "data": result}
        return {"ok": True, "data": result}

    @mcp.tool(annotations=READ_ONLY)
    async def notice_search(
        keyword: Annotated[str, Field(description="搜索关键词，尽量短，如“奖学金”“助学金”“选课”“宿舍”")],
        search_in: Annotated[Literal["title", "body", "full"], Field(description="title=只搜标题（结果最准，默认）；full=标题+正文")] = "title",
        range: Annotated[Literal["24h", "7d", "30d", "6m"], Field(description="发布时间范围")] = "6m",
        kind: Annotated[Literal["申请/报名", "公示/结果", "活动/讲座", "安排/提醒", "通知"] | None,
                        Field(description="只要某类通知。找“能申请的”用 申请/报名；找“结果出了没”用 公示/结果")] = None,
        audience: Annotated[Literal["本科生", "研究生"] | None, Field(description="只要面向本科生或研究生的（同时保留未注明对象的）")] = None,
        publisher: Annotated[str | None, Field(description="发文单位，如“学生部”“研究生院”“教务部”")] = None,
        limit: Annotated[int, Field(ge=1, le=40)] = 15,
    ) -> dict[str, Any]:
        """搜索公文通（学校官方公告）：奖学金、助学金、考试、选课、宿舍、放假安排等。

        每条结果带自动分类 kind（申请/报名、公示/结果……）和面向对象 audience，
        并返回整体分布 stats。结果很多时先看 stats，用 kind/audience 缩小范围，再用 notice_read_many 读最相关的几篇。"""
        res = await call(svc.notice_search(keyword, search_in=search_in, range=range, limit=500, publisher=publisher))
        if not res["ok"]:
            return res
        items = res["data"]["items"]
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        if audience:
            items = [i for i in items if i.get("audience") in (audience, "不限/未注明")]
        res["data"] = {"total": res["data"]["total"], "matched": len(items), "stats": res["data"]["stats"],
                       "items": [_slim_notice(x) for x in items[:limit]]}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def notice_view(
        notice_id: Annotated[str, Field(description="公告 id（来自 campus_events / notice_search 的结果）")],
    ) -> dict[str, Any]:
        """读取一条公文通公告的正文和附件列表。"""
        res = await call(svc.notice_view(notice_id))
        if res["ok"]:
            d = res["data"]
            res["data"] = {**_pick(d, "id", "title", "publisher", "publishedAt", "url"),
                           "content": _truncate(d.get("contentText"), 3000), "attachments": d.get("attachments", [])}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def notice_read_many(
        notice_ids: Annotated[list[str], Field(min_length=1, max_length=5, description="最多 5 个公告 id，挑最相关、最新的")],
    ) -> dict[str, Any]:
        """一次读取多条公告正文（每条截取前 1500 字），用于整理截止时间、申请条件、所需材料。比逐条调用 notice_view 快。"""
        out = []
        for nid in dict.fromkeys(notice_ids):
            res = await call(svc.notice_view(nid))
            if res["ok"]:
                d = res["data"]
                out.append({**_pick(d, "id", "title", "publisher", "publishedAt", "url"),
                            "content": _truncate(d.get("contentText"), 1500),
                            "attachments": [a["name"] for a in d.get("attachments", [])]})
            else:
                out.append({"id": nid, "error": res["error"]})
                if res["error"]["code"] in ("LOGIN_REQUIRED", "MFA_REQUIRED", "RATE_LIMITED"):
                    return {"ok": False, "error": res["error"], "data": {"notices": out}}
        return {"ok": True, "data": {"notices": out}}

    # ------------------------------ 讲座 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def lecture_list(
        availability: Annotated[Literal["available", "open", "all"],
                                Field(description="available=还有余位；open=报名窗口内（含已满）；all=含历史")] = "available",
    ) -> dict[str, Any]:
        """查询创新领航讲座平台的讲座（只读，不支持代报名；需要校园网）。"""
        res = await call(svc.lecture_list(availability))
        if res["ok"]:
            res["data"] = {"items": [_slim_lecture(x) for x in res["data"]["items"]]}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def lecture_progress() -> dict[str, Any]:
        """查询本人创新领航讲座的学习进度（线上/线下已完成次数和要求次数）。"""
        return await call(svc.lecture_progress())

    # ------------------------------ 体育场馆预约 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def sports_campuses() -> dict[str, Any]:
        """列出体育场馆预约系统中的校区。"""
        return await call(svc.sports_campuses())

    @mcp.tool(annotations=READ_ONLY)
    async def sports_projects(
        campus: Annotated[str, Field(description="校区名，如“粤海校区”“丽湖校区”")],
    ) -> dict[str, Any]:
        """列出某校区可预约的运动项目（羽毛球、足球、篮球、网球、游泳、健身……）及支持的预约方式（包场/散场）。
        数据实时来自学校。用户说“健身房”“打球”等口语时先调用它，再从返回的项目名里匹配；多个都像时让用户选。"""
        return await call(svc.sports_projects(campus))

    @mcp.tool(annotations=READ_ONLY)
    async def sports_slots(
        campus: str,
        project: Annotated[str, Field(description="sports_projects 返回的项目名，必须完全一致")],
        date: Annotated[str | None, Field(description="YYYY-MM-DD；不填为最近的开放日期")] = None,
        mode: Annotated[Literal["包场", "散场"] | None,
                        Field(description="预约方式：散场=个人使用一个位置；包场=整片场地。不填时按项目默认（优先散场）")] = None,
    ) -> dict[str, Any]:
        """查询某项目在某天的可预约时段及状态。返回里的 openDates 是学校当前开放预约的日期。"""
        return await call(svc.sports_slots(campus, project, date, mode))

    @mcp.tool(annotations=READ_ONLY)
    async def sports_fields(
        campus: str, project: str,
        date: Annotated[str, Field(description="YYYY-MM-DD")],
        slot: Annotated[str, Field(description="时段，如“20:00-21:00”")],
        mode: Annotated[Literal["包场", "散场"] | None,
                        Field(description="预约方式：散场=个人使用一个位置；包场=整片场地。不填时按项目默认（优先散场）")] = None,
    ) -> dict[str, Any]:
        """查询某项目某时段下具体可选的场地。多个可用时需让用户选择。"""
        return await call(svc.sports_fields(campus, project, date, slot, mode))

    @mcp.tool(annotations=READ_ONLY)
    async def sports_bookings(limit: Annotated[int, Field(ge=1, le=20)] = 5) -> dict[str, Any]:
        """查看我的体育场馆预约记录：时间、场地、是否已支付（awaitingPayment=待支付）、能否取消、实付金额。
        用于“我约了哪些”“上次那个付了没有”。未支付的预约会被学校自动取消，发现待支付的要提醒用户尽快付款。"""
        res = await call(svc.sports_bookings(limit))
        if res["ok"]:
            keep = ("orderNo", "timeRange", "project", "venue", "field", "status", "payState", "paid",
                    "awaitingPayment", "cancellable", "amountCny")
            res["data"] = {"total": res["data"].get("total"), "url": res["data"].get("url"),
                           "items": [{k: b.get(k) for k in keep} for b in res["data"]["items"]]}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def sports_balance() -> dict[str, Any]:
        """查询体育专项经费余额（元）。体育预约付款时可以选“体育经费”或“校园卡”；余额不够时提醒用户选校园卡。"""
        return await call(svc.sports_balance())

    @mcp.tool(annotations=READ_ONLY)
    async def sports_reserve_preview(
        campus: str, project: str,
        date: Annotated[str, Field(description="YYYY-MM-DD")],
        slot: Annotated[str, Field(description="时段，如“20:00-21:00”")],
        field: Annotated[str | None, Field(description="具体场地名；只有一个可用场地时可不填")] = None,
        mode: Annotated[Literal["包场", "散场"] | None,
                        Field(description="预约方式：散场=个人使用一个位置；包场=整片场地。不填时按项目默认（优先散场）")] = None,
    ) -> dict[str, Any]:
        """预览一次体育场馆预约（不会提交）。成功时返回预约详情和 confirmationToken。
        预览成功后，紧接着用这个 token 调用 sports_reserve_confirm：界面会弹出确认卡片，由用户点“确认”或“再想想”，
        不需要也不要在聊天里再问一遍“确定吗”。"""
        params = {"campus": campus, "project": project, "day": date, "slot": slot, "field": field, "mode": mode}
        res = await call(svc.sports_reserve(**params, confirm=False))
        if not res["ok"]:
            return res
        params["field"] = res["data"]["field"]  # 锁定实际选中的场地和预约方式
        params["mode"] = res["data"].get("mode") or mode
        token = pending.create("reserve", params)
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL,
                                     "note": "尚未提交。预约成功后需在学校系统中手动支付。"}}

    @mcp.tool(annotations=WRITE)
    async def sports_reserve_confirm(
        confirmation_token: Annotated[str, Field(description="sports_reserve_preview 返回的 confirmationToken")],
    ) -> dict[str, Any]:
        """【会真实提交】按预览时锁定的参数提交体育场馆预约。令牌只能用一次。
        调用后执行会暂停，界面弹出确认卡片；只有用户点了“确认”才会真正提交。"""
        action = pending.pop(confirmation_token, "reserve")
        if action is None:
            return TOKEN_INVALID
        return await call(svc.sports_reserve(**action.params, confirm=True))

    @mcp.tool(annotations=READ_ONLY)
    async def sports_cancel_preview(
        order_no: Annotated[str, Field(description="订单号，来自 sports_bookings")],
    ) -> dict[str, Any]:
        """预览取消一条体育预约（不会取消）：核对订单存在且学校允许取消。返回预约详情和 confirmationToken。
        预览成功后紧接着调用 sports_cancel_confirm，界面会弹出确认卡片，不要在聊天里再问一遍。
        订单号来自 sports_bookings；用户说“取消明天的健身房”这类话时先用 sports_bookings 找到对应订单。"""
        res = await call(svc.sports_cancel(order_no, confirm=False))
        if not res["ok"]:
            return res
        token = pending.create("cancel", {"order_no": order_no})
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL}}

    @mcp.tool(annotations=WRITE)
    async def sports_cancel_confirm(
        confirmation_token: Annotated[str, Field(description="sports_cancel_preview 返回的 confirmationToken")],
    ) -> dict[str, Any]:
        """【会真实取消】取消体育预约。调用后执行会暂停，只有用户在确认卡片上点了“确认”才会真正取消。"""
        action = pending.pop(confirmation_token, "cancel")
        if action is None:
            return TOKEN_INVALID
        return await call(svc.sports_cancel(action.params["order_no"], confirm=True))

    # ------------------------------ 宿舍电费 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def electricity_buildings() -> dict[str, Any]:
        """列出电费系统中的校区和宿舍楼栋（需要校园网）。楼栋名不确定时先调用它。"""
        return await call(svc.electricity_buildings())

    @mcp.tool(annotations=READ_ONLY)
    async def electricity_query(
        building: Annotated[str, Field(description="宿舍楼栋名，如“红豆斋”，支持唯一的部分匹配")],
        room: Annotated[str, Field(description="房间号，如“838”")],
        campus: Annotated[str | None, Field(description="校区/斋区名，楼栋能唯一匹配时可不填")] = None,
        from_date: Annotated[str | None, Field(description="YYYY-MM-DD，默认最近 7 天")] = None,
        to_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """查询宿舍剩余电量（度）和最近的用电记录（需要校园网）。"""
        res = await call(svc.electricity_query(building, room, campus, from_date, to_date))
        if res["ok"]:
            d = res["data"]
            res["data"] = {**_pick(d, "campus", "building", "room", "from", "to", "remainingKwh"),
                           "latestRecordAt": (d.get("latestRecord") or {}).get("recordedAt"),
                           "recentRecords": [_pick(r, "recordedAt", "remainingKwh") for r in (d.get("records") or [])[-10:]]}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def electricity_purchases(
        building: Annotated[str, Field(description="宿舍楼栋名，如“红豆斋”，支持唯一的部分匹配")],
        room: Annotated[str, Field(description="房间号，如“838”")],
        campus: Annotated[str | None, Field(description="校区，楼栋能唯一匹配时可不填")] = None,
        from_date: Annotated[str | None, Field(description="YYYY-MM-DD，默认最近 90 天")] = None,
        to_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """查询宿舍的电费充值记录（充了多少度、多少钱、什么时候、什么方式）。用于“这个月充了多少电费”“上次什么时候充的”。需要校园网。"""
        res = await call(svc.electricity_purchases(building, room, campus, from_date, to_date))
        if res["ok"]:
            d = res["data"]
            res["data"] = {**_pick(d, "campus", "building", "room", "from", "to", "totalKwh", "totalCny"),
                           "records": [_pick(r, "purchasedAt", "kwh", "amountCny", "purchaseType") for r in d["records"][-20:]]}
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def weather_today(
        campus: Annotated[Literal["粤海校区", "丽湖校区"], Field(description="校区")] = "粤海校区",
    ) -> dict[str, Any]:
        """校区今天的天气：当前天气和温度、体感、湿度、今天最高/最低温、接下来 12 小时降雨概率和可能下雨的时刻。
        用于“今天会下雨吗”“要不要带伞”“适合去打球吗”，也可以在建议户外运动或活动时顺带参考。"""
        from .campus.weather import get_weather, mock_weather

        if os.environ.get("SZU_AGENT_MOCK") == "1":
            return {"ok": True, "data": mock_weather(campus)}
        return await call(get_weather(campus))

    # ------------------------------ 网络面试间（静音舱） ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def booth_list(
        campus: Annotated[Literal["粤海", "丽湖"] | None, Field(description="只看某个校区")] = None,
        kind: Annotated[str | None, Field(description="空间类型关键词：面试 / 琴房 / 会议室 / 心灵小屋，不填列出全部")] = None,
    ) -> dict[str, Any]:
        """列出事务中心「一站式学生社区」的可预约公共空间：网络面试间（静音舱/静音仓）、共享琴房、共享会议室、心灵小屋，
        按校区、社区分，type 是空间类型。返回的 id 用于 booth_slots 和 booth_reserve_preview。
        用于“哪里有静音舱”“想找个琴房练琴”“约个小会议室”。outOfService=true 的故障停用中，不要推荐。"""
        return await call(svc.booth_list(campus, kind))

    @mcp.tool(annotations=READ_ONLY)
    async def booth_slots(
        booth_id: Annotated[int, Field(description="静音舱 id，来自 booth_list")],
        date: Annotated[str | None, Field(description="YYYY-MM-DD，不填为今天。只开放今天、明天、后天")] = None,
    ) -> dict[str, Any]:
        """查某个公共空间某一天还能约的时段（已经按可约日期范围、提前量过滤，只返回现在真能约的），
        以及用户当前能不能预约（eligible=false 时 reasons 里是禁约原因）。时段是半小时一格，时间已按学校规则提前 10 分钟。
        needsInterviewProof=true（网络面试间）的要上传面试邀请凭证，只能在网页上提交，给出 url；其他类型用 booth_reserve_preview。"""
        try:
            day = Date.fromisoformat(date) if date else None
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_DATE", "message": "日期格式应为 YYYY-MM-DD"}}
        res = await call(svc.booth_slots(booth_id, day))
        if res["ok"]:
            d = res["data"]
            # 半小时一格太碎：合并成连续区间给模型看，index 留给以后的预约工具
            spans: list[dict[str, Any]] = []
            for s in d["freeSlots"]:
                if spans and spans[-1]["lastIndex"] == s["index"] - 1:
                    spans[-1].update(end=s["end"], lastIndex=s["index"])
                else:
                    spans.append({"start": s["start"], "end": s["end"], "firstIndex": s["index"], "lastIndex": s["index"]})
            d["freeRanges"] = [f"{x['start']}-{x['end']}" for x in spans]
            d["freeSlotCount"] = len(d.pop("freeSlots"))
            d.pop("slotCounts", None)
        return res

    @mcp.tool(annotations=READ_ONLY)
    async def booth_my_reservations(limit: Annotated[int, Field(ge=1, le=20)] = 10) -> dict[str, Any]:
        """查看我在事务中心公共空间（静音舱、琴房、会议室、心灵小屋）的预约记录。id 用于 booth_cancel_preview。"""
        return await call(svc.booth_my_reservations(limit))

    @mcp.tool(annotations=READ_ONLY)
    async def booth_reserve_preview(
        booth_id: Annotated[int, Field(description="空间 id，来自 booth_list")],
        date: Annotated[str, Field(description="YYYY-MM-DD")],
        start: Annotated[str | None, Field(pattern=r"^\d{2}:\d{2}$", description="用户明确说了时间才填：开始时间 HH:MM，如 14:20")] = None,
        end: Annotated[str | None, Field(pattern=r"^\d{2}:\d{2}$", description="用户明确说了时间才填：结束时间 HH:MM，如 15:20")] = None,
        grade: Annotated[str | None, Field(pattern=r"^\d{4}$", description="年级 = 入学年份，如 2024；记忆里有就直接用，没有就问用户")] = None,
    ) -> dict[str, Any]:
        """预览预约共享琴房、共享会议室、心灵小屋（不会提交）：核对没被禁约、年级和联系手机号齐全。
        用户没说具体时间就不传 start/end——确认卡片上会列出这一天所有空闲的半小时格子让用户点选，
        **不要在聊天里列出时段让用户回复**；用户说了时间就传，卡片上会预选好（用户仍可改）。
        成功时返回预览和 confirmationToken，紧接着调用 booth_reserve_confirm，不要在聊天里再问一遍。
        网络面试间要上传面试邀请凭证，这里会返回 NOT_SUPPORTED_YET。手机号由界面卡片填写，不要在聊天里要。"""
        try:
            day = Date.fromisoformat(date)
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_DATE", "message": "日期格式应为 YYYY-MM-DD"}}
        res = await call(svc.booth_reserve(booth_id, day, start, end, grade, confirm=False))
        if not res["ok"]:
            return res
        token = pending.create("booth_reserve", {"booth_id": booth_id, "day": day, "start": start, "end": end, "grade": grade})
        # 预览里的 freeSlots 是给确认卡片画格子用的（图从这条结果里取预览），模型不要拿它在聊天里列时段
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL}}

    @mcp.tool(annotations=WRITE)
    async def booth_reserve_confirm(
        confirmation_token: Annotated[str, Field(description="booth_reserve_preview 返回的 confirmationToken")],
        slots: Annotated[list[int] | None, Field(description="不要填：由界面写入用户在卡片上点选的时段")] = None,
    ) -> dict[str, Any]:
        """【会真实预约】提交公共空间预约。调用后执行会暂停，只有用户在确认卡片上选好时段并点了“确认”才会真正提交。"""
        action = pending.pop(confirmation_token, "booth_reserve")
        if action is None:
            return TOKEN_INVALID
        p = action.params
        return await call(svc.booth_reserve(p["booth_id"], p["day"], p["start"], p["end"], p["grade"], confirm=True,
                                            slots=slots or None))

    @mcp.tool(annotations=READ_ONLY)
    async def booth_cancel_preview(
        reservation_id: Annotated[int, Field(description="预约记录 id，来自 booth_my_reservations")],
    ) -> dict[str, Any]:
        """预览取消一条公共空间预约（不会取消）。成功时返回记录和 confirmationToken，紧接着调用 booth_cancel_confirm，
        界面会弹出确认卡片。用户说“取消明天的琴房”时，先用 booth_my_reservations 找到对应记录的 id。"""
        res = await call(svc.booth_cancel(reservation_id, confirm=False))
        if not res["ok"]:
            return res
        token = pending.create("booth_cancel", {"reservation_id": reservation_id})
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL}}

    @mcp.tool(annotations=WRITE)
    async def booth_cancel_confirm(
        confirmation_token: Annotated[str, Field(description="booth_cancel_preview 返回的 confirmationToken")],
    ) -> dict[str, Any]:
        """【会真实取消】取消一条公共空间预约。调用后执行会暂停，只有用户在确认卡片上点了“确认”才会真正取消。"""
        action = pending.pop(confirmation_token, "booth_cancel")
        if action is None:
            return TOKEN_INVALID
        return await call(svc.booth_cancel(action.params["reservation_id"], confirm=True))

    # ------------------------------ 校园资料库 ------------------------------ #

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def academic_calendar(
        date: Annotated[str | None, Field(description="YYYY-MM-DD，不填为今天（北京时间）")] = None,
        week: Annotated[int | None, Field(ge=1, le=25, description="想知道“第 N 周是哪几天”时传周次，返回那一周每天的日期")] = None,
        with_periods: Annotated[bool, Field(description="需要节次作息时间（第几节几点上课）时传 true")] = False,
        campus: Annotated[Literal["粤海", "丽湖"], Field(description="作息时间按哪个校区")] = "粤海",
    ) -> dict[str, Any]:
        """查校历：某天是第几教学周、是不是假期；本学期的开学、上课、缓考、期末考试、毕业典礼、寒暑假日期
        （每项带 status：已结束 / 进行中 / 还有 N 天）；第 N 周对应的日期；第几节课几点上（with_periods）。
        用于“现在第几周”“第 15 周周三是几号”“什么时候考试/放寒假”“第 3 节几点上课”。
        日期和周次直接用返回结果，不要自己推算。国庆、中秋等法定假日调课另行通知，这里没有。"""
        from .campus.booth import beijing_now
        from .knowledge.calendar import default_calendar, term_outline

        try:
            day = Date.fromisoformat(date) if date else beijing_now().date()
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_ARGUMENT", "message": "date 要写成 YYYY-MM-DD"}}
        cal = default_calendar()
        data: dict[str, Any] = {"day": cal.phase(day), "source": cal.source}
        term = cal.relevant_term(day)
        if term:
            data["term"] = term_outline(term, day)
            if week is not None:
                if week > term.weeks:
                    data["weekHint"] = f"{term.name}只有 {term.weeks} 个教学周"
                else:
                    data["week"] = {"week": week, "days": term.week_days(week)}
        else:
            data["hint"] = ("这个日期的校历还没录入（学校一般提前半年发布）。如实告诉用户，"
                            f"请他到官网校历页确认：{cal.source['url']}")
        if with_periods:
            p = cal.periods.get(campus)
            if p:
                data["periods"] = {"campus": campus, "source": p["source"],
                                   "times": [{"period": i, "from": a, "to": b} for i, (a, b) in enumerate(p["times"], 1)]}
            else:
                data["periodsHint"] = f"还没有{campus}校区的作息时间表，如实告诉用户"
        return {"ok": True, "data": data}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def policy_search(
        query: Annotated[str, Field(description="关键词，空格分隔，如“国家励志奖学金 申请条件”“卓越班 一等奖 金额”“挂科”。"
                                                "不填时列出该学院有哪些细则")] = "",
        college: Annotated[str | None, Field(description="学院全称或常用简称（如“计算机与软件学院”“计软”“政管”）。"
                                                         "各学院规则不同，知道用户学院时一定要传")] = None,
        level: Annotated[Literal["本科生", "研究生", "博士生"] | None, Field(description="学生类别")] = None,
        limit: Annotated[int, Field(ge=1, le=8)] = 5,
    ) -> dict[str, Any]:
        """查奖学金、助学金评选细则原文（各学院 2026 年秋季发布的细则 + 学校的学业奖学金管理办法等），
        返回最相关的几段条文：申请条件、金额和名额、加分标准、综合成绩算法、材料和截止时间。
        用于“我们学院国家励志奖学金要什么条件”“卓越班一等奖多少钱”“挂科还能评吗”“论文怎么加分”。
        回答只依据返回的条文，注明出自哪份细则；查不到就说没查到，不要凭常识补。"""
        from .knowledge.policy import default_policy

        kb = default_policy("scholarship")
        if college:
            matched, docs = kb.filter_docs(college, level)
            if not matched:
                return {"ok": True, "data": {"chunks": [], "colleges": sorted(kb.publishers - {"深圳大学"}),
                                             "hint": f"没有找到“{college}”的细则。看看 colleges 里有没有用户的学院（可能叫法不同），"
                                                     "对上了就用全称重查；确实没有就如实告诉用户。"}}
        else:
            matched, docs = set(), kb.filter_docs(None, level)[1]
        data: dict[str, Any] = {}
        if matched:
            data["colleges"] = sorted(matched)
        if not query.strip():
            data["documents"] = [{"title": d.title, "kind": d.meta.get("kind"), "level": d.meta.get("level"),
                                  "published": d.meta.get("published")} for d in docs[:40]]
            return {"ok": True, "data": data}
        hits = kb.search(query, college=college, level=level, limit=limit)
        chunks, sources = [], {}
        for c, _ in hits:
            text = c.text if len(c.text) <= 1800 else c.text[:1800] + "…（后略）"
            chunks.append({"document": c.doc.title, "publisher": c.doc.publisher, "section": c.heading, "text": text})
            sources[c.doc.id] = {"title": c.doc.title, "published": c.doc.meta.get("published"), "url": c.doc.meta.get("source")}
        data.update({"chunks": chunks, "sources": list(sources.values()),
                     "note": "细则每学年秋季（9-10 月）重新发布；这里是 2026 年秋季（2025-2026 学年评选）的版本，"
                             "申请时间多半已过，具体以学院最新通知为准。"})
        if not college:
            data["hint"] = "没有指定学院，结果可能来自多个学院、规则不同。不知道用户学院就先问一句并用 remember_user_info 记下。"
        if not chunks:
            data["hint"] = "细则里没有相关内容。可以换个说法（用公文里的词，如“不及格”“家庭经济困难”）再查一次，还没有就如实说。"
        return {"ok": True, "data": data}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def campus_guide(
        query: Annotated[str, Field(description="名词关键词，空格分隔，如“会议室”“绿幕 拍摄”“静音舱”“派发传单”。"
                                                "用户说法口语化时先改写成场地名或用途（“开会的地方”→“会议”）")] = "",
        campus: Annotated[Literal["粤海", "丽湖"] | None, Field(description="只看某个校区")] = None,
        min_capacity: Annotated[int | None, Field(ge=1, description="至少能容纳多少人（只返回写明了人数的场地）")] = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> dict[str, Any]:
        """查校园资料库：学校系统查不到的静态信息——学生活动场地（会议室、研讨室、活动室、舞蹈室、报告厅、
        户外摆摊/传单场地）在哪、几点开、能坐多少人、能做什么、怎么申请；网络面试间（静音舱）、心灵小屋、
        事务中心「办事百宝」里各项服务的入口。
        用于“丽湖哪里能开 30 人的会”“有带绿幕的拍摄场地吗”“静音舱在哪约”“摆摊发传单要去哪申请”。
        不是实时数据：查不到空闲情况。体育场馆用 sports_* 工具，不用这个。"""
        from .knowledge import default_kb

        kb = default_kb()
        hits = kb.search(query, campus=campus, min_capacity=min_capacity, limit=limit)
        if not hits:
            return {"ok": True, "data": {"entries": [], "hint": "资料库里没有相关条目。可以换个说法再查一次；"
                                         "还是没有就如实告诉用户不知道，不要凭印象回答。"}}
        entries, used = [], {}
        for e, _ in hits:
            item = _pick(e.data, "name", "campus", "kind", "location", "hours", "capacity", "rules", "note", "booking")
            item["source"] = e.source
            if kb.is_stale(e):
                item["stale"] = True
            entries.append(item)
            used[e.source] = kb.sources[e.source]
        data: dict[str, Any] = {"entries": entries, "sources": list(used.values())}
        if any(x.get("stale") for x in entries):
            data["hint"] = "带 stale 的条目超过半年没核实，回答时提醒用户以申请页面为准。"
        return {"ok": True, "data": data}

    # ------------------------------ 课表 / 图书馆 ------------------------------ #

    @mcp.tool(annotations=READ_ONLY)
    async def course_today(
        date: Annotated[str | None, Field(description="YYYY-MM-DD，不填为今天")] = None,
    ) -> dict[str, Any]:
        """查询某一天的课程（节次、地点、老师）。"""
        try:
            day = Date.fromisoformat(date) if date else None
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_DATE", "message": "日期格式应为 YYYY-MM-DD"}}
        return await call(svc.course_today(day))

    @mcp.tool(annotations=READ_ONLY)
    async def library_search(
        keyword: Annotated[str, Field(description="书名、作者或主题关键词")],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        """搜索图书馆馆藏：索书号、馆藏数量、可借数量。结果里的 id 就是 library_book / 预借要用的书目编号。"""
        return await call(svc.library_search(keyword, limit))

    @mcp.tool(annotations=READ_ONLY)
    async def library_book(
        ctrlno: Annotated[str, Field(description="书目编号：library_search 结果里的 id")],
    ) -> dict[str, Any]:
        """查看一本书每一册的馆藏位置（含排架）、状态（可供出借 / 借出及应还日期）、预约排队人数。
        onShelf>0 时可以尝试预借（馆员从书库取书送到取书点）；全部借出时只能到馆预约，暂不支持代办。"""
        return await call(svc.library_book(ctrlno))

    @mcp.tool(annotations=READ_ONLY)
    async def library_prebook_preview(
        ctrlno: Annotated[str, Field(description="书目编号：library_search 结果里的 id")],
        pickup: Annotated[str | None, Field(description="取书地点：只在用户已经明确说了（如“北馆”“丽湖”“南区取书柜”）时填，会在卡片上预选；"
                                                       "没说就不填，不要在聊天里问")] = None,
    ) -> dict[str, Any]:
        """预览图书预借登记（不会提交）：先向图书馆预检能否预借，再核对联系方式。
        成功时返回预借详情和 confirmationToken，紧接着调用 library_prebook_confirm——界面会弹出确认卡片，
        用户在卡片上点选取书地点并确认。不要在聊天里列出取书地点让用户选，也不要再问“确定吗”。
        手机号由用户在界面卡片里填写，你看到的只是打码后的形式，不要询问或复述手机号。"""
        res = await call(svc.library_prebook(ctrlno, pickup, confirm=False))
        if not res["ok"]:
            return res
        options = [p["value"] for p in res["data"]["pickupOptions"]]
        token = pending.create("prebook", {"ctrlno": ctrlno, "pickup": res["data"]["pickupId"], "options": options})
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL}}

    @mcp.tool(annotations=WRITE)
    async def library_prebook_confirm(
        confirmation_token: Annotated[str, Field(description="library_prebook_preview 返回的 confirmationToken")],
        pickup_id: Annotated[str | None, Field(description="不要填：用户在确认卡片上点选取书地点后由系统自动填入")] = None,
    ) -> dict[str, Any]:
        """【会真实提交】按预览时锁定的书目提交预借登记，取书地点以用户在确认卡片上点选的为准。令牌只能用一次。
        调用后执行会暂停，界面弹出确认卡片；只有用户点了“确认”才会真正提交。"""
        action = pending.pop(confirmation_token, "prebook")
        if action is None:
            return TOKEN_INVALID
        pickup = pickup_id or action.params["pickup"]
        if pickup not in action.params["options"]:  # 只能是预览时页面上真实存在的选项
            return {"ok": False, "error": {"code": "LIBRARY_PICKUP_REQUIRED", "message": "取书地点无效，请重新预览",
                                           "agentHint": "重新调用 library_prebook_preview，让用户在卡片上选择取书地点。"}}
        return await call(svc.library_prebook(action.params["ctrlno"], pickup, confirm=True))

    @mcp.tool(annotations=READ_ONLY)
    async def library_my_prebooks(
        page: Annotated[int, Field(ge=1, le=20, description="页码，新记录在第 1 页")] = 1,
    ) -> dict[str, Any]:
        """我的预借记录：书名、取书地点、提交日期、状态（如“未处理”“读者取消”）、能否取消（cancellable）。
        fid 是预借记录号，取消时用；只有能取消的记录才有 fid。"""
        return await call(svc.library_my_prebooks(page))

    @mcp.tool(annotations=READ_ONLY)
    async def library_prebook_cancel_preview(
        fid: Annotated[str, Field(description="预借记录号，来自 library_my_prebooks 的 fid")],
    ) -> dict[str, Any]:
        """预览取消一条预借（不会取消）：核对记录存在且图书馆允许取消。成功时返回记录详情和 confirmationToken，
        紧接着调用 library_prebook_cancel_confirm，界面会弹出确认卡片，不要在聊天里再问一遍。
        用户说“取消三体的预借”时，先用 library_my_prebooks 找到对应记录的 fid。"""
        res = await call(svc.library_prebook_cancel(fid, confirm=False))
        if not res["ok"]:
            return res
        token = pending.create("prebook_cancel", {"fid": fid})
        return {"ok": True, "data": {"preview": res["data"], "confirmationToken": token, "expiresInSeconds": PendingStore.TTL}}

    @mcp.tool(annotations=WRITE)
    async def library_prebook_cancel_confirm(
        confirmation_token: Annotated[str, Field(description="library_prebook_cancel_preview 返回的 confirmationToken")],
    ) -> dict[str, Any]:
        """【会真实取消】取消一条图书预借。调用后执行会暂停，只有用户在确认卡片上点了“确认”才会真正取消。"""
        action = pending.pop(confirmation_token, "prebook_cancel")
        if action is None:
            return TOKEN_INVALID
        return await call(svc.library_prebook_cancel(action.params["fid"], confirm=True))

    return mcp


def main() -> None:
    create_server().run()  # stdio


if __name__ == "__main__":
    main()
