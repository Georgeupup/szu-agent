"""图书馆预借登记的业务流程。真实模式和演示模式共用，只有“怎么取页面、怎么提交”不同（PrebookBackend）。

预借 = 馆员从书库把在架的书取出来，送到指定的取书点，读者收到通知后 3 天内去取。
和“我要预约”（书全部借出时排队）不是一回事，这里只做预借。

预借流程（和网页上一致）：
  1. 打开 addfb.aspx：拿到 ASP.NET 表单（隐藏字段）、取书点选项、预填的邮箱
  2. 预检 addfbcheckAjax.aspx：返回 0 才能继续；1=已预借过 2=没有可预借的册 3=无权限 4/5/6=数量已满 7=超时
     （网页在弹窗打开时自动调用这个预检，这里同样先查）
  3. 提交前读一次“我的预借”（myfb.aspx），记下这本书有几条记录
  4. 提交表单（模拟点击 Button1）
  5. 再读一次“我的预借”：这本书多出一条记录才算成功

成功与否以第 5 步为准。最初用的是“提交后再预检返回 1（已预借过）”，真实预借时（2026-11-14）
提交成功了、页面也提示“预借请求提交成功”，再预检却不是 1，被误报为“未确认”——所以改成核对记录列表，
它不依赖任何状态文字或返回码的含义。

取书地点：预览时可以不定（pickup=None），预览返回全部选项，由用户在确认卡片上点选；
图在人工确认节点把用户点的选项写进 confirm 调用（见 graph.human_review），提交时必须已经选定。

取消：myfb.aspx 里的 deletefb(fid, status)，只有 status==0 能取消；deletefbAjax.aspx?fid= 返回 1=操作成功、0=操作失败。
取消后同样再读一次列表，确认这条记录已经不能再取消。
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from .contact import ContactStore, mask_email, mask_phone
from .errors import CampusError
from .parsers import PREBOOK_CHECK, match_pickup, page_alerts, parse_my_prebooks, parse_prebook_form

EMAIL_RE = re.compile(r"^[\w.\-]+@([\w\-]+\.)+[A-Za-z]{2,4}$")  # 和页面 checkuemail() 一致
PREBOOK_RULES = [
    "收到“预借图书到书通知”后，3 天内到取书地点取书，否则视为违约",
    "违约三次将被取消预借资格 30 天",
]
MY_PREBOOK_URL = "https://www.lib.szu.edu.cn/opac/user/myfb.aspx"  # 我的图书馆 → 我的预借
MAX_LIST_PAGES = 3  # 找某条记录时最多翻几页（新记录在第一页）


class PrebookBackend(Protocol):
    async def prebook_form(self, ctrlno: str) -> tuple[str, str]: ...          # (html, 页面 URL)
    async def prebook_check(self, ctrlno: str, volume: str) -> str: ...        # 预检返回的数字
    async def prebook_submit(self, page_url: str, html: str, values: dict[str, str]) -> str: ...
    async def prebook_list_page(self, page: int) -> str: ...                    # myfb.aspx 的 HTML
    async def prebook_delete(self, fid: str) -> str: ...                        # deletefbAjax 的返回


def check_error(code: str) -> CampusError:
    if code in PREBOOK_CHECK:
        err, message = PREBOOK_CHECK[code]
        return CampusError(err, message)
    return CampusError("PAGE_CHANGED", f"图书馆预检返回了未知结果：{code[:20]!r}")


async def prebook(backend: PrebookBackend, contact: ContactStore, ctrlno: str, pickup: str | None, *,
                  confirm: bool) -> dict[str, Any]:
    if not re.fullmatch(r"\d{1,12}", ctrlno or ""):
        raise CampusError("NOT_FOUND", "书目编号（ctrlno）应该是数字，来自 library_search 结果里的 id")
    html, page_url = await backend.prebook_form(ctrlno)
    form = parse_prebook_form(html)
    if not form["hasForm"]:
        raise CampusError("PAGE_CHANGED", "预借登记页面上找不到表单")
    volume = "1" if form["hasVolume"] else ""  # 与页面 checkfb() 一致：有卷期下拉框时传 1

    code = (await backend.prebook_check(ctrlno, volume)).strip()
    if code != "0":
        raise check_error(code)

    choice = match_pickup(form["pickups"], pickup)
    if choice is None and confirm:
        raise CampusError("LIBRARY_PICKUP_REQUIRED", "提交前需要选定取书地点", options=[p["name"] for p in form["pickups"]])
    phone = contact.phone()
    if not phone:
        raise CampusError("CONTACT_REQUIRED", "图书馆预借需要手机号（用于到书通知）")
    email = form["prefilledEmail"]
    if not EMAIL_RE.match(email):
        raise CampusError("LIBRARY_EMAIL_REQUIRED", "图书馆账户里没有可用的邮箱", url=page_url)

    preview: dict[str, Any] = {
        "ctrlno": ctrlno, "book": form["bookInfo"],
        "pickup": choice["name"] if choice else None, "pickupId": choice["value"] if choice else None,
        "pickupOptions": form["pickups"],  # 确认卡片上的按钮
        "phone": mask_phone(phone), "email": mask_email(email), "rules": PREBOOK_RULES, "submitted": False,
    }
    if not confirm:
        return preview
    assert choice is not None

    before = _records_for(await my_prebooks(backend), ctrlno)
    result_html = await backend.prebook_submit(page_url, html, {"ddlfl": choice["value"], "txtphone": phone, "txtemail": email})
    alerts = page_alerts(result_html)
    after = _records_for(await my_prebooks(backend), ctrlno)
    if len(after) > len(before):
        return {**preview, "submitted": True, "record": after[0], "message": alerts[0] if alerts else None}
    # 列表里没有新记录：页面提示的原文带上（成功提示是“预借请求提交成功”，失败提示没有样例），不自动重试
    said = f"（图书馆页面提示：{alerts[0]}）" if alerts else ""
    raise CampusError("LIBRARY_PREBOOK_UNCONFIRMED", f"已提交，但“我的预借”里没有出现这条记录{said}", url=MY_PREBOOK_URL)


def _records_for(listing: dict[str, Any], ctrlno: str) -> list[dict[str, Any]]:
    return [r for r in listing["items"] if r["ctrlno"] == ctrlno]


async def my_prebooks(backend: PrebookBackend, page: int = 1) -> dict[str, Any]:
    data = parse_my_prebooks(await backend.prebook_list_page(page))
    return {**data, "url": MY_PREBOOK_URL}


async def find_record(backend: PrebookBackend, fid: str) -> dict[str, Any] | None:
    page, pages = 1, 1
    while page <= min(pages, MAX_LIST_PAGES):
        listing = await my_prebooks(backend, page)
        for r in listing["items"]:
            if r["fid"] == fid:
                return r
        pages, page = listing["pages"], page + 1
    return None


async def cancel_prebook(backend: PrebookBackend, fid: str, *, confirm: bool) -> dict[str, Any]:
    if not re.fullmatch(r"\d{1,12}", fid or ""):
        raise CampusError("NOT_FOUND", "预借记录号（fid）应该是数字，来自 library_my_prebooks 结果")
    record = await find_record(backend, fid)
    if record is None:
        raise CampusError("NOT_FOUND", "“我的预借”里没有这条可取消的记录（已取消、已处理的记录不能取消）")
    if not record["cancellable"]:
        raise CampusError("LIBRARY_NOT_CANCELLABLE", "当前状态不能取消预借")  # 原文来自 myfb.aspx 的 deletefb()
    if not confirm:
        return {**record, "cancelled": False}

    result = (await backend.prebook_delete(fid)).strip()
    if result == "0":
        raise CampusError("LIBRARY_CANCEL_FAILED", "图书馆返回“操作失败”")
    if result != "1":
        raise CampusError("PAGE_CHANGED", f"取消接口返回了未知结果：{result[:20]!r}")
    after = await find_record(backend, fid)
    if after is not None and after["cancellable"]:
        raise CampusError("LIBRARY_CANCEL_FAILED", "图书馆返回“操作成功”，但这条预借仍然可以取消，请到网页上确认", url=MY_PREBOOK_URL)
    return {**record, "cancelled": True, "cancellable": False}
