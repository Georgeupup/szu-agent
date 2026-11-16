"""图书馆：书目详情解析、预借登记（预检 → 表单提交 → 再预检核实），以及手机号不进入模型上下文。

样例页面来自用户保存的真实页面（已脱敏，见 mock_fixtures/library-*.html 开头的注释）。
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from szu_agent.agent.profile import ProfileStore
from szu_agent.campus import CampusError, SessionStore
from szu_agent.campus.contact import ContactStore, mask_email, mask_phone
from szu_agent.campus.http import CampusHttp
from szu_agent.campus.mock import MockCampus
from szu_agent.campus.parsers import match_pickup, page_alerts, parse_library_book, parse_my_prebooks, parse_prebook_form
from szu_agent.campus.service import RealCampus
from szu_agent.web.app import create_app

from .conftest import ScriptedChatModel
from .test_mcp_server import mcp_client

FIX = __import__("pathlib").Path(__file__).parent.parent / "src/szu_agent/mock_fixtures"
BOOK = (FIX / "library-book.html").read_text(encoding="utf-8")
HOLDINGS = (FIX / "library-book-holdings.html").read_text(encoding="utf-8")
MYFB = (FIX / "library-myfb.html").read_text(encoding="utf-8")
FORM = (FIX / "library-addfb.html").read_text(encoding="utf-8")
PHONE = "13800138000"


def test_parse_book_detail():
    # 真实情况：bookinfo.aspx 本身没有馆藏表（用户自检时解析出 0 册），馆藏来自 bookinfowithresvAjax.aspx
    assert parse_library_book(BOOK)["copies"] == []
    d = parse_library_book(BOOK, HOLDINGS)
    assert d["ctrlno"] == "3351727" and d["title"] == "想念地坛" and d["isbn"] == "978-7-5455-6651-2"
    first, _, lent = d["copies"]
    assert first["location"] == "北馆1楼文学书库I" and first["shelf"] == "汇典楼一楼15排5架5层"
    assert first["callNumber"] == "I217.62/S57a13" and first["onShelf"] is True
    assert lent["onShelf"] is False and lent["dueDate"] == "2026-12-01" and lent["reserveQueue"] == 1
    assert (d["onShelf"], d["lent"]) == (2, 1)


def test_parse_prebook_form_hides_prefilled_email():
    f = parse_prebook_form(FORM)
    assert f["hasForm"] and f["hasVolume"] and f["bookInfo"].startswith("想念地坛")
    assert [p["value"] for p in f["pickups"]] == ["4", "6", "3"]  # “请选择”和快递（5）不在里面


@pytest.mark.parametrize("wanted,value", [
    ("北馆", "3"), ("汇典楼", "3"), ("丽湖", "4"), ("中央图书馆", "4"), ("南区取书柜", "6"), ("6", "6"),
    ("粤海", None),  # 粤海有两个取书点：有歧义就让用户选，不替用户猜
    ("南山", None), (None, None),
])
def test_match_pickup(wanted, value):
    hit = match_pickup(parse_prebook_form(FORM)["pickups"], wanted)
    assert (hit["value"] if hit else None) == value


def test_alerts_and_masks():
    assert page_alerts("<script>alert('预借成功');window.close();</script>") == ["预借成功"]
    assert mask_phone(PHONE) == "138****8000"
    assert mask_email("2500000000@mails.szu.edu.cn") == "***@mails.szu.edu.cn"  # 学校邮箱用户名就是学号


# ------------------------------ 真实适配器：模拟图书馆服务器 ------------------------------ #

NEW_ROW = ('<tr><td>1</td><td><span class="title"><a href="https://www.lib.szu.edu.cn/opac/bookinfo.aspx?ctrlno=3351727">'
           '想念地坛</a></span></td><td></td><td>粤海校区南区致理楼C栋1层信息中心服务大厅取书柜</td><td>2026-11-14</td>'
           '<td>未处理</td><td><a href="javascript:deletefb(175020,0)">[取消]</a></td></tr>')


def library_server(check_codes: list[str], *, accept: bool = True, delete_result: str = "1"):
    """模拟图书馆：addfbcheckAjax 依次返回 check_codes；accept=True 时提交后“我的预借”多一条记录；
    deletefbAjax 返回 delete_result（1 时该记录变成不可取消）。"""
    seen = {"posts": [], "checks": [], "deletes": []}
    state = {"myfb": MYFB}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        html = {"Content-Type": "text/html; charset=utf-8"}
        if path.endswith("/bookinfowithresvAjax.aspx"):
            return httpx.Response(200, text=HOLDINGS, headers=html)
        if path.endswith("/bookinfo.aspx"):
            return httpx.Response(200, text=BOOK, headers=html)
        if path.endswith("/addfb.aspx") and req.method == "GET":
            return httpx.Response(200, text=FORM, headers=html)
        if path.endswith("/addfbcheckAjax.aspx"):
            seen["checks"].append(dict(req.url.params))
            assert req.headers["Referer"].endswith("addfb.aspx?ctrlno=3351727")
            return httpx.Response(200, text=check_codes.pop(0))
        if path.endswith("/addfb.aspx") and req.method == "POST":
            seen["posts"].append({k: v[0] for k, v in parse_qs(req.content.decode(), keep_blank_values=True).items()})
            if accept:
                state["myfb"] = state["myfb"].replace("<tbody>", "<tbody>" + NEW_ROW, 1)
            return httpx.Response(200, text="<html><script>alert('预借请求提交成功');</script></html>")
        if path.endswith("/user/myfb.aspx"):
            return httpx.Response(200, text=state["myfb"], headers=html)
        if path.endswith("/user/deletefbAjax.aspx"):
            seen["deletes"].append({**dict(req.url.params), "xhr": req.headers.get("X-Requested-With"),
                                    "referer": req.headers.get("Referer")})
            if delete_result == "1":
                fid = req.url.params["fid"]
                state["myfb"] = state["myfb"].replace(f'<a href="javascript:deletefb({fid},0)">[取消]</a>', "")
            return httpx.Response(200, text=delete_result)
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


def real(tmp_path, codes=("0", "0"), phone=PHONE, **server):
    transport, seen = library_server(list(codes), **server)
    contact = ContactStore(tmp_path / "contact.json")
    if phone:
        contact.set_phone(phone)
    return RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=transport), contact), seen


def test_parse_my_prebooks():
    d = parse_my_prebooks(MYFB)
    active, cancelled = d["items"]
    assert (d["page"], d["pages"]) == (1, 1)
    assert active == {"fid": "175017", "ctrlno": "1901562", "title": "三体", "volume": None,
                      "pickup": "粤海校区南区致理楼C栋1层信息中心服务大厅取书柜", "submittedAt": "2026-11-14",
                      "status": "未处理", "cancellable": True}
    assert cancelled["fid"] is None and cancelled["status"] == "读者取消" and cancelled["cancellable"] is False


async def test_prebook_preview_then_submit(tmp_path):
    svc, seen = real(tmp_path)
    preview = await svc.library_prebook("3351727", "南区取书柜", confirm=False)
    assert preview["pickup"].endswith("取书柜") and preview["submitted"] is False
    assert PHONE not in json.dumps(preview) and "student@" not in json.dumps(preview, ensure_ascii=False)
    assert seen["posts"] == []  # 预览绝不提交

    done = await svc.library_prebook("3351727", preview["pickupId"], confirm=True)
    assert done["submitted"] is True and done["message"] == "预借请求提交成功"
    assert done["record"]["fid"] == "175020" and done["record"]["status"] == "未处理"
    [post] = seen["posts"]
    assert post["Button1"] == "提交"  # 模拟点击“提交”按钮，否则 ASP.NET 不触发提交事件
    assert post["__VIEWSTATE"] == "VIEWSTATE-PLACEHOLDER" and post["__EVENTVALIDATION"] == "EVENTVALIDATION-PLACEHOLDER"
    assert (post["ddlfl"], post["txtphone"], post["txtemail"]) == ("6", PHONE, "student@mails.szu.edu.cn")
    assert seen["checks"][0] == {"ctrlno": "3351727", "volume": "1"}  # 有卷期下拉框时传 1，与页面 checkfb() 一致


async def test_success_does_not_depend_on_precheck_returning_1(tmp_path):
    """2026-11-14 真实预借：提交成功了，但之后预检并不返回 1。成功与否只看“我的预借”里有没有多出记录。
    列表里原本就有这本书的一条“读者取消”记录，也不能把它当成这次的结果。"""
    svc, seen = real(tmp_path)
    assert (await svc.library_prebook("3351727", "北馆", confirm=True))["submitted"] is True
    assert len(seen["checks"]) == 1  # 提交后不再预检


@pytest.mark.parametrize("code,error", [
    ("1", "LIBRARY_ALREADY_PREBOOKED"), ("2", "LIBRARY_NOT_PREBOOKABLE"), ("4", "LIBRARY_PREBOOK_QUOTA_FULL"),
    ("7", "NETWORK_ERROR"), ("<html>", "PAGE_CHANGED"),
])
async def test_precheck_failures_stop_before_submit(tmp_path, code, error):
    svc, seen = real(tmp_path, [code])
    with pytest.raises(CampusError) as e:
        await svc.library_prebook("3351727", "北馆", confirm=True)
    assert e.value.code == error and seen["posts"] == []


async def test_pickup_is_chosen_on_card_but_required_to_submit(tmp_path):
    svc, seen = real(tmp_path, ["0", "0"])
    # 没说或有歧义（“粤海”有两个）：预览照样成功，带上全部选项，由用户在确认卡片上点选
    preview = await svc.library_prebook("3351727", "粤海", confirm=False)
    assert preview["pickup"] is None and [o["value"] for o in preview["pickupOptions"]] == ["4", "6", "3"]
    with pytest.raises(CampusError) as e:  # 提交时必须已经选定
        await svc.library_prebook("3351727", None, confirm=True)
    assert e.value.code == "LIBRARY_PICKUP_REQUIRED" and seen["posts"] == []


async def test_phone_required(tmp_path):

    svc, _ = real(tmp_path, phone=None)
    with pytest.raises(CampusError) as e:
        await svc.library_prebook("3351727", "北馆", confirm=False)
    assert e.value.code == "CONTACT_REQUIRED"


async def test_submit_without_new_record_is_not_reported_as_success(tmp_path):
    svc, seen = real(tmp_path, accept=False)
    with pytest.raises(CampusError) as e:
        await svc.library_prebook("3351727", "北馆", confirm=True)
    # 页面提示了，但列表里没有新记录：不能报成功，也不能自动重试（可能重复提交）；把页面原文带给用户
    assert e.value.code == "LIBRARY_PREBOOK_UNCONFIRMED" and "提交成功" in e.value.message and len(seen["posts"]) == 1
    assert e.value.details["url"].endswith("/user/myfb.aspx")


async def test_cancel_prebook(tmp_path):
    svc, seen = real(tmp_path)
    preview = await svc.library_prebook_cancel("175017", confirm=False)
    assert preview["title"] == "三体" and preview["cancelled"] is False and seen["deletes"] == []

    done = await svc.library_prebook_cancel("175017", confirm=True)
    assert done["cancelled"] is True
    # 与页面 JS 一致：XHR、带 _= 空参数、Referer 是“我的预借”页
    assert seen["deletes"] == [{"fid": "175017", "_": "", "xhr": "XMLHttpRequest",
                                "referer": "https://www.lib.szu.edu.cn/opac/user/myfb.aspx"}]

    with pytest.raises(CampusError) as e:  # 已取消的记录没有取消链接
        await svc.library_prebook_cancel("175017", confirm=False)
    assert e.value.code == "NOT_FOUND"


async def test_cancel_failures(tmp_path):
    svc, seen = real(tmp_path, delete_result="0")
    with pytest.raises(CampusError) as e:
        await svc.library_prebook_cancel("175017", confirm=True)
    assert e.value.code == "LIBRARY_CANCEL_FAILED"

    svc, seen = real(tmp_path / "x", delete_result="1")
    svc.prebook_list_page = lambda page: _async(MYFB.replace("deletefb(175017,0)", "deletefb(175017,1)"))
    with pytest.raises(CampusError) as e:  # status!=0：页面 JS 会提示“当前状态不能取消预借”，不发请求
        await svc.library_prebook_cancel("175017", confirm=True)
    assert e.value.code == "LIBRARY_NOT_CANCELLABLE" and seen["deletes"] == []


async def _async(value):
    return value


# ------------------------------ MCP 工具层 ------------------------------ #

async def test_prebook_tools_token_flow(logged_in, home):
    async with mcp_client(logged_in) as call:
        tools = {t.name: t for t in (await call.session.list_tools()).tools}
        assert tools["library_prebook_confirm"].annotations.destructiveHint is True
        # 手机号不能是任何工具的参数：参数由 LLM 生成，会进入上下文和历史
        assert not any("phone" in json.dumps(t.inputSchema) for t in tools.values())

        res = await call("library_prebook_preview", ctrlno="3351727", pickup="北馆")
        assert res["error"]["code"] == "CONTACT_REQUIRED" and "卡片" in res["error"]["agentHint"]

        ContactStore().set_phone(PHONE)
        res = await call("library_prebook_preview", ctrlno="3351727", pickup="北馆")
        assert res["ok"] and res["data"]["preview"]["phone"] == "138****8000"
        token = res["data"]["confirmationToken"]
        assert (await call("library_prebook_confirm", confirmation_token=token))["data"]["submitted"] is True
        assert (await call("library_prebook_confirm", confirmation_token=token))["error"]["code"] == "TOKEN_INVALID"
        again = await call("library_prebook_preview", ctrlno="3351727", pickup="北馆")
        assert again["error"]["code"] == "LIBRARY_ALREADY_PREBOOKED"

        # 取书地点在卡片上选：预览不带 pickup，确认时由图填入 pickup_id；只能是页面上真实存在的选项
        res = await call("library_prebook_preview", ctrlno="1901562")
        assert res["data"]["preview"]["pickup"] is None
        bad = await call("library_prebook_confirm", confirmation_token=res["data"]["confirmationToken"], pickup_id="5")
        assert bad["error"]["code"] == "LIBRARY_PICKUP_REQUIRED"  # 5 = 快递，不在选项里
        res = await call("library_prebook_preview", ctrlno="1901562")
        ok = await call("library_prebook_confirm", confirmation_token=res["data"]["confirmationToken"], pickup_id="6")
        assert ok["data"]["submitted"] is True and ok["data"]["pickup"].endswith("取书柜")

        mine = (await call("library_my_prebooks"))["data"]["items"]
        fid = next(r["fid"] for r in mine if r["cancellable"])
        res = await call("library_prebook_cancel_preview", fid=fid)
        assert res["data"]["preview"]["title"] == "想念地坛"
        done = await call("library_prebook_cancel_confirm", confirmation_token=res["data"]["confirmationToken"])
        assert done["data"]["cancelled"] is True
        assert tools["library_prebook_cancel_confirm"].annotations.destructiveHint is True

        # 不变量：每个会真实改动数据的工具都必须由图强制人工确认，新增写操作时漏加会在这里失败
        from szu_agent.agent.graph import SENSITIVE_TOOLS

        destructive = {n for n, t in tools.items() if t.annotations and t.annotations.destructiveHint}
        assert destructive == set(SENSITIVE_TOOLS)
        assert set(SENSITIVE_TOOLS.values()) <= set(tools)


async def test_book_detail_tool(logged_in):
    async with mcp_client(logged_in) as call:
        res = await call("library_book", ctrlno="3351727")
        assert res["data"]["onShelf"] == 2


def test_mock_and_real_have_same_library_methods():
    for name in ("library_book", "library_prebook", "library_my_prebooks", "library_prebook_cancel",
                 "prebook_form", "prebook_check", "prebook_submit", "prebook_list_page", "prebook_delete"):
        assert hasattr(MockCampus, name) and hasattr(RealCampus, name)


# ------------------------------ Web：手机号只走后端接口 ------------------------------ #

def test_contact_api_never_returns_full_phone(home):
    with TestClient(create_app(mock=True, llm=ScriptedChatModel(script=[]), profile=ProfileStore(home / "p.json"))) as client:
        assert client.get("/api/contact").json() == {"phone": None}
        assert client.put("/api/contact", json={"phone": "12345678901"}).status_code == 400
        assert client.put("/api/contact", json={"phone": PHONE}).json() == {"ok": True, "phone": "138****8000"}
        assert client.get("/api/contact").json() == {"phone": "138****8000"}
        assert ContactStore().phone() == PHONE
        assert client.delete("/api/contact").json() == {"ok": True}
        assert ContactStore().phone() is None


async def test_selfcheck_is_read_only_and_hides_personal_fields(monkeypatch, home):
    from szu_agent import selfcheck

    transport, seen = library_server(["0"])
    monkeypatch.setattr(selfcheck, "CampusHttp", lambda store, **kw: CampusHttp(store, transport=transport, **kw))
    lines: list[str] = []
    await selfcheck.check_library("3351727", out=lines.append)
    text = "\n".join(lines)
    assert "3 册（在架 2）" in text and "返回 '0'：可以预借" in text and "2 条记录" in text and "可取消 1" in text and "Button1" in text and "6:粤海校区南区" in text
    assert "student@" not in text and seen["posts"] == []
