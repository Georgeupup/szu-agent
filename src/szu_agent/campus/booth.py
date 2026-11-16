"""网络面试间（静音舱）预约：事务中心「办事百宝」里的场地系统（venue-api）。

接口来源：学校前端脚本（2026-11-14 用户从 swzx.webvpn.szu.edu.cn 导出的 assets/*.js）：
  index-*.js                      请求封装 px()、接口路径、响应解码 cC()、登录地址 nC
  pages-booth-szu-booth-list      GET  /booth/list?current=1&pageSize=9999          → {list: [...]}
  pages-booth-szu-booth-rule      GET  /booth/info/{id}                             → 名称、校区、社区、类型、描述
  pages-booth-szu-booth-detail    GET  /boothType/info/{typeId}                     → 开放时段掩码、每日上限、提前天数
                                  GET  /booth/{id}/available-time?startDate&endDate → [{date, times[48], timeOffsetMinutes}]
                                  GET  /boothReservation/eligibility?boothId&date   → 能不能约、可约日期范围、禁约原因
                                  POST /boothReservation/add（stage 2：要上传面试邀请凭证，还没接）
  （我的预约）                    GET  /boothReservation/my?current=1&pageSize=10

几个从脚本里读出来的约定：
  - 地址：前端用当前页面的域名，学校部署在 swzx.webvpn.szu.edu.cn——挂在学校 WebVPN 上的站点，
    校内校外都要先登录 WebVPN（没有会话时网关报“site not found”并跳 WebVPN 登录）。登录见 service.SwzxApi。
  - 响应：{data, encoding, message}；encoding == 1 时 data 是异或“加密”的 JSON 字符串，
    密钥是前端里一段 TS 源码文本本身（_XOR_KEY），按 UTF-16 码元逐个异或。
  - 时段：一天 48 个半小时格，第 i 格从 00:00 + 30*i 分钟 + timeOffsetMinutes 开始
    （2026-11 起新预约整体提前 10 分钟，offset = -10，所以 09:00 那格显示为 08:50-09:20）。
    times[i]：1 = 空闲，0 = 已被预约，-1 = 锁定，其他 = 使用中。
  - 登录：WebVPN 之后，场地系统未登录时接口返回 401；前端跳到 /venue-api/login?front=页面地址，由后端走统一认证。
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .errors import CampusError

SWZX = "https://swzx.webvpn.szu.edu.cn"
VENUE_API = f"{SWZX}/venue-api"
BOOTH_PAGE = f"{SWZX}/#/pages/booth/szu-booth-list"
LOGIN_URL = f"{VENUE_API}/login"

# 前端 lC 常量（一段源码文本，换行是真换行），cC() 用它异或解码 encoding==1 的 data
_XOR_KEY = (
    "export function encryptXor (str: string) {\n"
    "  const key = '学生事务服务中心26535653';\n"
    "  let res = '';\n"
    "  for (let i = 0; i < str.length; i++) {\n"
    "    res += String.fromCharCode(str.charCodeAt(i) ^ key.charCodeAt(i % key.length));\n"
    "  }\n"
    "  return res;\n"
    "}"
)

SLOT_STATE = {1: "free", 0: "booked", -1: "locked"}
CST = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """学校的“今天”“提前 2 小时”都按北京时间算；不依赖运行机器的时区
    （2026-11-15 在 UTC 的容器里调试时，按本机日期查资格把“今天”算成了昨天，接口回 eligible=false）。"""
    return datetime.now(CST).replace(tzinfo=None)

# 使用须知（rule 页 M 数组 + detail 页“预约说明”），预览和资料库里给模型看
BOOTH_RULES = [
    "每人每日最多预约 4 个时段，每个时段半小时；同一时段只能约一个静音舱",
    "只开放今天、明天、后天，每次只能约同一天，须按时段实际开始时间至少提前 2 小时",
    "提交时须上传面试邀请凭证（邮件、短信、微信邀约截图，或 PDF/Word，1-5 份，单份不超过 10MB），提交后立即生效",
    "后台核查发现材料不合规会取消预约；一个自然月内两次材料不合规，30 天内禁止预约",
    "请在预约时段内入场；预约结束仍未入场按爽约处理，一个自然月内三次爽约，30 天内禁止预约",
    "使用期间禁止吃零食、外卖或携带含糖饮料，用完带走个人物品",
]


def xor_decode(text: str, key: str = _XOR_KEY) -> str:
    """等价于前端 cC() 里的异或：JS 的 charCodeAt 按 UTF-16 码元算，这里也按码元算（emoji 等不会错位）。"""
    # surrogatepass：异或后的字符串里可能出现落单的代理码元（JSON 里是 \udXXX），普通编码会报错
    data = text.encode("utf-16-le", errors="surrogatepass")
    k = key.encode("utf-16-le")
    units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
    kunits = [int.from_bytes(k[i:i + 2], "little") for i in range(0, len(k), 2)]
    out = bytearray()
    for i, u in enumerate(units):
        out += (u ^ kunits[i % len(kunits)]).to_bytes(2, "little")
    return out.decode("utf-16-le", errors="surrogatepass")


def unwrap(body: Any) -> Any:
    """{data, encoding, message} → data。"""
    if not isinstance(body, dict) or "data" not in body:
        raise CampusError("PAGE_CHANGED", "事务中心接口返回格式和预期不一致")
    data = body["data"]
    if body.get("encoding") == 1 and isinstance(data, str):
        try:
            data = json.loads(xor_decode(data))
        except json.JSONDecodeError as exc:
            raise CampusError("PAGE_CHANGED", "事务中心接口的加密数据解不开（密钥可能换了）") from exc
    return data


def slot_range(day: str, index: int, offset: int) -> tuple[str, str]:
    start = datetime.fromisoformat(day[:10]) + timedelta(minutes=30 * index + offset)
    end = start + timedelta(minutes=30)
    return start.strftime("%H:%M"), end.strftime("%H:%M")


def mask_bits(mask: Any) -> set[int]:
    """availableTimePeriod：48 位掩码（前端用 BigInt 处理，可能是字符串），第 i 位 = 第 i 格开放。"""
    try:
        value = int(str(mask))
    except (TypeError, ValueError):
        return set(range(48))
    return {i for i in range(48) if value >> i & 1}


class BoothBackend(Protocol):
    async def api(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                  json_body: Any = None) -> Any: ...  # 返回解码后的 data


def _booth_brief(b: dict[str, Any]) -> dict[str, Any]:
    out = {k: b.get(k) for k in ("id", "name", "campus", "community", "description") if b.get(k) not in (None, "")}
    t = b.get("type")
    if isinstance(t, dict) and t.get("name"):
        out["type"] = t["name"]
    if b.get("status") is False:
        out["outOfService"] = True  # 列表页：status === false 时提示“该静音舱故障，暂时停止使用”
    return out


async def list_booths(api: BoothBackend, campus: str | None = None, kind: str | None = None) -> dict[str, Any]:
    data = await api.api("GET", "/booth/list", params={"current": "1", "pageSize": "9999"})
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise CampusError("PAGE_CHANGED", "静音舱列表返回格式异常")
    booths = [_booth_brief(b) for b in rows if isinstance(b, dict) and b.get("campus") and b.get("community")]
    if campus:
        key = campus.replace("校区", "")
        booths = [b for b in booths if key in str(b.get("campus"))]
    if kind:
        booths = [b for b in booths if kind in str(b.get("type", "")) or kind in str(b.get("name", ""))]
    return {"booths": booths, "count": len(booths), "url": BOOTH_PAGE}


async def booth_slots(api: BoothBackend, booth_id: int, day: Date | None = None, *,
                      now: datetime | None = None) -> dict[str, Any]:
    now = now or beijing_now()
    booth = await api.api("GET", f"/booth/info/{booth_id}")
    if not isinstance(booth, dict) or not booth.get("id"):
        raise CampusError("NOT_FOUND", f"没有编号为 {booth_id} 的空间，先用 booth_list 查")
    btype = booth.get("type") if isinstance(booth.get("type"), dict) else {}
    if booth.get("typeId"):
        btype = {**btype, **(await api.api("GET", f"/boothType/info/{booth['typeId']}") or {})}

    target = (day or now.date()).isoformat()
    elig = await api.api("GET", "/boothReservation/eligibility",
                         params={"boothId": booth_id, "date": target, "_t": int(now.timestamp() * 1000)}) or {}
    # szu-booth-detail 的 ve：资格接口说要审材料（面试邀请凭证），或者接口没返回时类型名里有“面试”
    evidence = bool(elig["materialReviewRequired"]) if "materialReviewRequired" in elig else "面试" in str(btype.get("name") or "")
    if evidence:  # 面试间：今天到后天、至少提前 2 小时（接口给了就以接口为准）
        min_date = str(elig.get("minDate") or now.date().isoformat())[:10]
        max_date = str(elig.get("maxDate") or (now.date() + timedelta(days=2)).isoformat())[:10]
        min_adv = 120 if elig.get("minAdvanceMinutes") is None else int(elig["minAdvanceMinutes"])
    else:  # 琴房 / 会议室 / 心灵小屋：前端按类型的 lastReservationDayBeforeAppointment 算可约天数；只给还没开始的格
        days = int(btype.get("lastReservationDayBeforeAppointment") or 3)
        min_date = str(elig.get("minDate") or now.date().isoformat())[:10]
        max_date = str(elig.get("maxDate") or (now.date() + timedelta(days=days - 1)).isoformat())[:10]
        min_adv = int(elig.get("minAdvanceMinutes") or 0)

    rows = await api.api("GET", f"/booth/{booth_id}/available-time", params={"startDate": target, "endDate": target})
    if not isinstance(rows, list):
        raise CampusError("PAGE_CHANGED", "静音舱时段返回格式异常")
    row = next((r for r in rows if str(r.get("date", ""))[:10] == target), None)
    open_bits = mask_bits(btype.get("availableTimePeriod")) if btype.get("availableTimePeriod") is not None else set(range(48))
    offset = (row or {}).get("timeOffsetMinutes")
    if offset is None:
        offset = btype.get("timeOffsetMinutes") or 0

    free: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    in_range = min_date <= target <= max_date
    for i, v in enumerate((row or {}).get("times") or []):
        if i not in open_bits:
            continue
        state = SLOT_STATE.get(v, "inUse")
        counts[state] = counts.get(state, 0) + 1
        start, end = slot_range(target, i, offset)
        starts_at = datetime.fromisoformat(target) + timedelta(minutes=30 * i + offset)
        if state == "free" and in_range and starts_at >= now + timedelta(minutes=min_adv):
            free.append({"index": i, "start": start, "end": end})

    result: dict[str, Any] = {
        "booth": _booth_brief(booth),
        "date": target,
        "bookableDates": f"{min_date} 至 {max_date}",
        "minAdvanceHours": round(min_adv / 60, 1),
        "maxSlotsPerDay": btype.get("samePersonMaxReservationPerDay") or 4,
        "needsInterviewProof": evidence,
        "freeSlots": free,
        "slotCounts": counts,
        "url": BOOTH_PAGE,
    }
    if elig.get("eligible") is False:
        result["eligible"] = False
        result["reasons"] = [str(r) for r in elig.get("reasons") or []][:5]
    if not in_range:
        result["note"] = f"{target} 不在可预约范围（{min_date} 至 {max_date}）"
    if isinstance(elig.get("currentMonthViolationCount"), int):
        result["materialViolationsThisMonth"] = elig["currentMonthViolationCount"]
    return result


# 预约记录里只保留这些字段：真实返回里可能有姓名、学号、手机号，一律不往外给
_RES_KEYS = ("id", "boothId", "boothName", "date", "startTime", "endTime", "status", "statusName", "createTime",
             "campus", "community")


async def my_reservations(api: BoothBackend, limit: int = 10) -> dict[str, Any]:
    data = await api.api("GET", "/boothReservation/my", params={"current": "1", "pageSize": str(limit)})
    rows = data.get("list") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise CampusError("PAGE_CHANGED", "我的静音舱预约返回格式异常")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        item = {k: r[k] for k in _RES_KEYS if r.get(k) not in (None, "")}
        booth = r.get("booth")
        if isinstance(booth, dict):
            item.setdefault("boothName", booth.get("name"))
            item.setdefault("campus", booth.get("campus"))
            item.setdefault("community", booth.get("community"))
        if isinstance(r.get("timeList"), list) and r.get("date"):
            offset = r.get("timeOffsetMinutes") or 0
            item["slots"] = [" - ".join(slot_range(str(r["date"]), int(i), offset)) for i in r["timeList"]]
        out.append(item)
    return {"reservations": out, "count": len(out), "url": BOOTH_PAGE}


# ------------------------------------------------------------------ 预约 / 取消
# 来源：pages-booth-szu-booth-detail 的 je()——非面试类型逐日调用
#   addBoothReservation({boothId, phone, grade, date, timeList})     timeList = 选中的格号（0-47）
# 面试类型还要 evidenceIds（先 POST /boothReservation/evidence/upload）和 clientRequestId，凭证文件含个人信息，
# 要走网页卡片上传、不进 LLM，还没接——这里直接报 NOT_SUPPORTED_YET 并给网页地址。
# 前端对输入的校验：phone 匹配 /^1\d{10}$/；grade 是 1000-3000 之间的整数（入学年份，如 2024）。
# 取消：cancelBoothReservation({id}) → POST /boothReservation/cancel/{id}

GRADE_MIN, GRADE_MAX = 1000, 3000


def _pick_slots(free: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    """用户说的 [start, end) 必须正好由连续的空闲格拼成，否则返回空。"""
    chosen = [s for s in free if start <= s["start"] and s["end"] <= end and s["start"] < s["end"]]
    if not chosen or chosen[0]["start"] != start or chosen[-1]["end"] != end:
        return []
    if any(b["index"] != a["index"] + 1 for a, b in zip(chosen, chosen[1:])):
        return []
    return chosen


def _ranges(free: list[dict[str, Any]]) -> list[str]:
    out: list[list[Any]] = []
    for s in free:
        if out and out[-1][2] == s["index"] - 1:
            out[-1][1], out[-1][2] = s["end"], s["index"]
        else:
            out.append([s["start"], s["end"], s["index"]])
    return [f"{a}-{b}" for a, b, _ in out]


def _check_contiguous(chosen: list[dict[str, Any]]) -> bool:
    return bool(chosen) and all(b["index"] == a["index"] + 1 for a, b in zip(chosen, chosen[1:]))


async def reserve(api: BoothBackend, contact: Any, booth_id: int, day: Date, start: str | None, end: str | None,
                  grade: str | None, *, confirm: bool, slots: list[int] | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """预约（非面试类型）。时段有两种给法：
      - start/end：用户在聊天里说了时间（“14:20 到 15:20”）
      - slots：用户在确认卡片上点选的格号（由图写进 confirm 调用，见 graph.apply_user_choices）
    预览时两者都可以没有：预览返回这一天全部可约的格子，卡片上让用户点选；提交时必须已经选定。"""
    from .contact import mask_phone

    info = await booth_slots(api, booth_id, day, now=now)
    booth = info["booth"]
    if info["needsInterviewProof"]:
        raise CampusError("NOT_SUPPORTED_YET", "网络面试间预约要上传面试邀请凭证，助手还不能代传，请在网页上提交", url=BOOTH_PAGE)
    if booth.get("outOfService"):
        raise CampusError("BOOTH_SLOT_UNAVAILABLE", "这个空间故障停用中")
    if info.get("eligible") is False:
        raise CampusError("BOOTH_NOT_ELIGIBLE", "；".join(info.get("reasons") or []) or "当前不能预约")
    free = info["freeSlots"]
    if not free:
        raise CampusError("BOOTH_SLOT_UNAVAILABLE", f"{info['date']} 已经没有可约的时段", freeRanges=[])

    if slots:
        by_index = {s["index"]: s for s in free}
        chosen = [by_index[i] for i in sorted(set(slots)) if i in by_index]
        if len(chosen) != len(set(slots)) or not _check_contiguous(chosen):
            raise CampusError("BOOTH_SLOT_UNAVAILABLE", "选的时段不是一段连续的空闲时段（可能刚被别人约走了）",
                              freeRanges=_ranges(free))
    elif start and end:
        chosen = _pick_slots(free, start, end)
        if not chosen:
            raise CampusError("BOOTH_SLOT_UNAVAILABLE", f"{info['date']} {start}-{end} 不是一段连续的空闲时段",
                              freeRanges=_ranges(free))
    else:
        chosen = []
    if confirm and not chosen:
        raise CampusError("BOOTH_SLOT_REQUIRED", "提交前需要选定时段")
    if len(chosen) > info["maxSlotsPerDay"]:
        raise CampusError("BOOTH_SLOT_UNAVAILABLE", f"每天最多约 {info['maxSlotsPerDay']} 个半小时时段，这次选了 {len(chosen)} 个")
    if not grade or not grade.isdigit() or not GRADE_MIN < int(grade) < GRADE_MAX:
        raise CampusError("BOOTH_GRADE_REQUIRED", "需要年级（入学年份，如 2024）")
    phone = contact.phone()
    if not phone:
        raise CampusError("CONTACT_REQUIRED", "预约需要联系手机号")

    preview: dict[str, Any] = {
        "booth": booth.get("name"), "type": booth.get("type"),
        "location": " · ".join(str(booth[k]) for k in ("campus", "community") if booth.get(k)),
        "date": info["date"], "grade": grade, "phone": mask_phone(phone),
        "maxSlotsPerDay": info["maxSlotsPerDay"],
        # 卡片上可点选的格子；selectedSlots 是按用户说的时间预选的（没说就为空，由用户点）
        "freeSlots": [{"index": s["index"], "start": s["start"], "end": s["end"]} for s in free],
        "selectedSlots": [s["index"] for s in chosen],
    }
    if chosen:
        preview["time"] = f"{chosen[0]['start']}-{chosen[-1]['end']}"
        preview["slotCount"] = len(chosen)
    if not confirm:
        return preview

    before = {r.get("id") for r in (await my_reservations(api, 20))["reservations"]}
    await api.api("POST", "/boothReservation/add", json_body={
        "boothId": booth_id, "phone": phone, "grade": grade, "date": info["date"],
        "timeList": [s["index"] for s in chosen]})
    # 学校成功时不返回预约内容：和图书馆预借一样，以“我的预约”里多出新记录为准
    new = [r for r in (await my_reservations(api, 20))["reservations"] if r.get("id") not in before]
    if not new:
        raise CampusError("BOOTH_RESERVE_UNCONFIRMED", "提交了，但“我的预约”里没看到新记录", url=BOOTH_PAGE)
    out = {k: v for k, v in preview.items() if k not in ("freeSlots", "selectedSlots")}
    return {**out, "submitted": True, "reservation": new[0]}


async def cancel(api: BoothBackend, reservation_id: int, *, confirm: bool) -> dict[str, Any]:
    mine = (await my_reservations(api, 20))["reservations"]
    record = next((r for r in mine if str(r.get("id")) == str(reservation_id)), None)
    if record is None:
        raise CampusError("NOT_FOUND", f"“我的预约”里没有编号为 {reservation_id} 的记录")
    if not confirm:
        return record
    await api.api("POST", f"/boothReservation/cancel/{reservation_id}", json_body={})
    after = next((r for r in (await my_reservations(api, 20))["reservations"] if str(r.get("id")) == str(reservation_id)), None)
    return {"cancelled": True, "record": after or record}
