"""自检：在你自己的电脑上，用本机登录凭证对学校接口做只读检查，输出可以直接贴给别人排查。

    uv run szu-agent check sports

只调用查询类接口（项目目录、开放日期、时段、场地），绝不提交预约；
输出只有字段名、项目/场地名称和计数，不含学号、姓名、Cookie 等个人信息。
"""

from __future__ import annotations

import json
from typing import Any

from .campus import CampusError, SessionStore
from .campus.http import CampusHttp
from .campus.parsers import normalize_field, normalize_slot, parse_sports_catalog
from .campus.service import SPORTS_APP, RealCampus

XHR = {"X-Requested-With": "XMLHttpRequest"}


def _keys(rows: list[Any]) -> list[str]:
    return sorted({k for r in rows[:5] if isinstance(r, dict) for k in r})


async def _post(http: CampusHttp, path: str, data: dict[str, str] | None = None) -> Any:
    resp = await http.request("POST", f"{SPORTS_APP}/{path}", data=data or {}, headers=XHR)
    return resp.json()


async def check_sports(out=print) -> None:
    store = SessionStore()
    info = store.info()
    out(f"登录凭证: {'有' if info['hasSession'] else '没有'}（方式 {info['method']}）")
    http = CampusHttp(store, cache_ttl=0)
    svc = RealCampus(http)
    try:
        await svc._sports_user()  # 进入应用；只确认能拿到，不输出
        out("进入体育预约应用: ✓")

        raw = await _post(http, "sportVenue/getSportVenueData.do")
        out(f"\n[getSportVenueData.do] 顶层字段: {sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}")
        for key in ("campusList", "xmList", "dismissalVenueList", "packageVenueList"):
            rows = raw.get(key) or []
            out(f"  {key}: {len(rows)} 条, 字段 {_keys(rows)}")
        catalog = parse_sports_catalog(raw)
        out("  解析出的校区: " + ", ".join(f"{c['name']}({c['code']})" for c in catalog["campuses"]))
        for p in catalog["projects"]:
            out(f"  项目: {p['name']:<10} 代码 {p['code']:<6} 校区 {','.join(p['campusCodes']):<6} 方式 {'/'.join(p['modes']) or '-'}")

        dates = await svc.sports_dates()
        out(f"\n[getRqList.do] 开放日期: {dates}")

        # 挑第一个有空闲时段的项目，看看时段和场地长什么样
        for p in catalog["projects"]:
            for mode_code in ("2.0", "1.0"):
                if not p["modes"] or {"2.0": "散场", "1.0": "包场"}[mode_code] not in p["modes"]:
                    continue
                campus = p["campusCodes"][0] if p["campusCodes"] else ""
                for day in dates[:2]:
                    slots_raw = await _post(http, "sportVenue/getTimeList.do",
                                            {"XQ": campus, "YYRQ": day, "YYLX": mode_code, "XMDM": p["code"]})
                    slots = [normalize_slot(r) for r in slots_raw if isinstance(r, dict)] if isinstance(slots_raw, list) else []
                    free = [s for s in slots if s["selectable"]]
                    if not free:
                        continue
                    out(f"\n[getTimeList.do] {p['name']} {day} {'散场' if mode_code == '2.0' else '包场'}: "
                        f"{len(slots)} 个时段, 字段 {_keys(slots_raw)}, 例如 {free[0]}")
                    start, _, end = free[0]["label"].partition("-")
                    room = await _post(http, "modules/sportVenue/getOpeningRoom.do",
                                       {"XMDM": p["code"], "YYRQ": day, "YYLX": mode_code, "KSSJ": start, "JSSJ": end, "XQDM": campus})
                    rows = ((room.get("datas") or {}).get("getOpeningRoom") or {}).get("rows") or []
                    out(f"[getOpeningRoom.do] code={room.get('code')} {len(rows)} 个场地, 字段 {_keys(rows)}")
                    for r in rows[:4]:
                        f = normalize_field(r)
                        out(f"  场地: {f['name']} | 场馆 {f['venue']} | 可约 {f['available']} | 状态 {f['state']}")
                    out("\n自检完成（全程只读，没有提交任何预约）")
                    return
        out("\n没有找到任何有空闲时段的项目，无法检查场地接口（可能今天的都约满了）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
    except (ValueError, json.JSONDecodeError) as exc:
        out(f"\n✗ 学校返回的不是 JSON：{exc}")
    finally:
        await http.aclose()


async def check_library(ctrlno: str = "3351727", out=print) -> None:
    """图书馆：书目详情 + 预借登记页 + 预检 + 我的预借。只读：打开预借页面不会提交，预检只是查询。

    输出只有字段名、取书点名称、预检返回码、记录数和状态；预填的邮箱、姓名、借了哪些书都不输出。
    """
    from urllib.parse import urlparse

    from .campus.http import parse_form
    from .campus.parsers import PREBOOK_CHECK, parse_library_book, parse_prebook_form

    store = SessionStore()
    out(f"登录凭证: {'有' if store.info()['hasSession'] else '没有'}")
    http = CampusHttp(store, cache_ttl=0)
    svc = RealCampus(http)
    try:
        book = await svc.library_book(ctrlno)
        out(f"[bookinfo.aspx + bookinfowithresvAjax.aspx] 书名 {book['title']!r}，{len(book['copies'])} 册（在架 {book['onShelf']}），"
            f"状态 {sorted({c['status'][:12] for c in book['copies']})}")

        html, url = await svc.prebook_form(ctrlno)
        out(f"[addfb.aspx] 最终地址 {urlparse(url).path}")
        form = parse_prebook_form(html)
        _, action, fields = parse_form(html, url, contains="ddlfl", submit="Button1")
        out(f"  表单字段 {sorted(fields)}，提交到 {urlparse(action).path}")
        out(f"  有卷期下拉框 {form['hasVolume']}，预填邮箱 {'有' if form['prefilledEmail'] else '没有'}")
        out(f"  取书点 {[p['value'] + ':' + p['name'] for p in form['pickups']]}")

        code = (await svc.prebook_check(ctrlno, "1" if form["hasVolume"] else "")).strip()
        meaning = "可以预借" if code == "0" else PREBOOK_CHECK.get(code, ("", "未知返回"))[1]
        out(f"[addfbcheckAjax.aspx] 返回 {code[:20]!r}：{meaning}")

        mine = await svc.library_my_prebooks()
        items = mine["items"]
        out(f"[myfb.aspx] 第 {mine['page']}/{mine['pages']} 页，{len(items)} 条记录，可取消 {sum(r['cancellable'] for r in items)}，"
            f"状态 {sorted({r['status'] for r in items if r['status']})}，书目编号齐全 {all(r['ctrlno'] for r in items)}")
        out("\n自检完成（全程只读，没有提交预借）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
    finally:
        await http.aclose()


async def check_courses(out=print) -> None:
    """研究生课表（wdkbapp）：身份判断、开放学期、每个学期的排课记录长什么样、课程列表分页。

    不输出课程名、老师、教室、学号姓名；只输出字段名、计数、周次 / 星期 / 节次这类取值。
    """
    from collections import Counter

    from .campus.debuglog import where
    from .campus.parsers import build_gs_courses, emap_rows
    from .campus.service import GS_COURSE_PAGE

    store = SessionStore()
    out(f"登录凭证: {'有' if store.info()['hasSession'] else '没有'}")
    http = CampusHttp(store, cache_ttl=0)
    svc = RealCampus(http)
    try:
        resp = await http.request("GET", GS_COURSE_PAGE)
        out(f"[index.do] {resp.status_code} 落在 {where(resp.url)}")
        info = await svc._gs_json("GET", "wdkcb/initXsxx.do", "学生信息", params={"XH": ""})
        data = info.get("data") if isinstance(info, dict) else None
        out(f"[initXsxx.do] 顶层 {sorted(info) if isinstance(info, dict) else type(info).__name__}，"
            f"data {len(data) if isinstance(data, list) else data!r} 条，字段 {_keys(data) if isinstance(data, list) else '-'}")
        out(f"  判断为研究生: {await svc._is_graduate()}")
        terms = await svc._gs_terms()
        out(f"[kfdxnxqcx.do] 开放学期: {terms}")
        for code, name in terms[:3]:
            body = await svc._gs_json("POST", "modules/xskcb/xspkjgcx.do", "课表", data={"XNXQDM": code, "XH": ""})
            rows = emap_rows(body, "xspkjgcx", "研究生课表")
            items = build_gs_courses(rows)
            out(f"\n[xspkjgcx.do {name}] {len(rows)} 条记录 → 合并成 {len(items)} 段，字段 {_keys(rows)}")
            for k in ("ZCMC", "XQ", "KSJCDM", "JSJCDM", "KSSJ", "JSSJ"):
                out(f"  {k} 取值: {dict(Counter(str(r.get(k)) for r in rows).most_common(10))}")
            out("  合并后: " + "; ".join(f"周{x['weekday']} 第{x['startSection']}-{x['endSection']}节 {x['time']} {x['weeksText']}" for x in items))
            lst = await svc._gs_json("POST", "modules/xskcb/xsjxrwcx.do", "课程列表",
                                     data={"XNXQDM": code, "XH": "", "pageSize": "100", "pageNumber": "1"})
            lrows = emap_rows(lst, "xsjxrwcx", "研究生课程列表")
            total = ((lst.get("datas") or {}).get("xsjxrwcx") or {}).get("totalSize")
            out(f"[xsjxrwcx.do {name}] totalSize={total}，本页 {len(lrows)} 行，没排课的 {sum(1 for r in lrows if not r.get('PKSJDD'))} 门")
        out("\n自检完成（只读；上面没有课程名、老师、教室）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
    finally:
        await http.aclose()


async def check_grades(out=print) -> None:
    """研究生成绩（szdxwdcjapp）：进应用落在哪、两种请求体的返回、行里有哪些字段、各字段有值的条数。

    不输出课程名、成绩、学分这些值；只输出学期代码 / 学期名 / 考试性质这类和个人无关的取值。
    """
    from collections import Counter

    from .campus.debuglog import where
    from .campus.parsers import build_grade_report, parse_grade_page
    from .campus.service import GRADE_LIST, GRADE_PAGE

    store = SessionStore()
    out(f"登录凭证: {'有' if store.info()['hasSession'] else '没有'}")
    http = CampusHttp(store, cache_ttl=0)
    try:
        resp = await http.request("GET", GRADE_PAGE)
        out(f"[index.do] {resp.status_code} 落在 {where(resp.url)}，跳转 {[where(r.url) for r in resp.history]}")
        out(f"  /gsapp/ 下的 Cookie: {sorted(c.name for c in http.cookies.jar if (c.path or '').startswith('/gsapp'))}")
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": GRADE_PAGE}
        rows: list[dict[str, Any]] = []
        for label, body in (("浏览器默认", {"pageSize": "10", "pageNumber": "1"}),
                            ("爱小荔", {"pageSize": "100", "pageNumber": "1"})):
            r = await http.request("POST", GRADE_LIST, data=body, headers=headers)
            ctype = r.headers.get("content-type", "")
            try:
                raw = r.json()
            except ValueError:
                out(f"[xscjcx.do {label}] {r.status_code} {ctype} 不是 JSON，落在 {where(r.url)}")
                continue
            datas = raw.get("datas") if isinstance(raw, dict) else None
            out(f"[xscjcx.do {label}] {r.status_code} code={raw.get('code') if isinstance(raw, dict) else '-'} "
                f"顶层 {sorted(raw) if isinstance(raw, dict) else type(raw).__name__} datas {sorted(datas) if isinstance(datas, dict) else '-'}")
            try:
                page_rows, total = parse_grade_page(raw)
            except CampusError as exc:
                out(f"  ✗ {exc.message}")
                continue
            out(f"  totalSize={total}，本页 {len(page_rows)} 行")
            rows = page_rows or rows
        if rows:
            keys = sorted({k for r in rows for k in r})
            out(f"\n字段（{len(keys)} 个）: {keys}")
            filled = {k: sum(1 for r in rows if r.get(k) not in (None, "")) for k in
                      ("KCMC", "XF", "CJ", "DYBFZCJ", "JDZ", "XNXQDM", "XNXQDM_DISPLAY", "KSXZDM_DISPLAY", "KCLBMC")}
            out(f"有值的行数（共 {len(rows)} 行）: {filled}")
            for k in ("XNXQDM", "XNXQDM_DISPLAY", "KSXZDM_DISPLAY", "KCLBMC", "SFJG_DISPLAY", "CJFZDM_DISPLAY"):
                out(f"  {k} 取值: {dict(Counter(str(r.get(k)) for r in rows).most_common(8))}")
            out(f"  CJ 是不是都是数字: {all(str(r.get('CJ') or '0').replace('.', '', 1).isdigit() for r in rows)}")
            rep = build_grade_report(rows)
            out(f"解析结果: {len(rep['terms'])} 个学期，overall 有学分 {rep['overall']['credits'] > 0}，"
                f"有加权平均 {rep['overall']['weightedScore'] is not None}，缺绩点 {rep['overall']['gradePointMissing']} 门")
        out("\n自检完成（只读；上面没有课程名和成绩）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
    finally:
        await http.aclose()


async def check_booth(out=print) -> None:
    """网络面试间（事务中心 venue-api）：登录跳转链 + 列表 + 时段 + 资格 + 我的预约。全程只读，不提交预约。

    输出只有主机/路径、字段名、计数和静音舱名称；当前用户信息（姓名、卡号）、预约记录里的值都不输出。
    """
    from collections import Counter

    from .campus.booth import BOOTH_PAGE, LOGIN_URL, VENUE_API, mask_bits
    from .campus.debuglog import where
    from .campus.service import SwzxApi

    store = SessionStore()
    out(f"登录凭证: {'有' if store.info()['hasSession'] else '没有'}")
    http = CampusHttp(store, cache_ttl=0)
    api = SwzxApi(http)
    svc = RealCampus(http)

    async def raw(path: str, params: dict | None = None) -> Any:
        resp = await http.request("GET", f"{VENUE_API}{path}", params=params, headers={"Accept": "application/json"})
        body = resp.json()
        out(f"[{path}] HTTP {resp.status_code}，顶层字段 {sorted(body) if isinstance(body, dict) else type(body).__name__}，"
            f"encoding={body.get('encoding') if isinstance(body, dict) else '-'}")
        return body

    async def probe(label: str, url: str, params: dict | None = None) -> Any:
        """不抛异常的请求：把跳转链、状态码、内容类型、网页标题都打出来（不输出正文），返回 JSON 或 None。"""
        import re

        try:
            resp = await http.request("GET", url, params=params, headers={"Accept": "application/json"})
        except CampusError as exc:
            out(f"[{label}] ✗ {exc.code} {exc.details.get('status', '')} {exc.message}")
            return None
        chain = " → ".join(where(r.url) for r in [*resp.history, resp])
        ctype = resp.headers.get("content-type", "-").split(";")[0]
        out(f"[{label}] HTTP {resp.status_code} {ctype} {len(resp.content)} 字节 | 跳转链 {chain}")
        try:
            body = resp.json()
        except ValueError:
            title = re.search(r"<title[^>]*>([^<]{0,60})", resp.text, re.I)
            out(f"  不是 JSON；网页标题 {title.group(1).strip() if title else '-'!r}")
            return None
        out(f"  JSON 顶层字段 {sorted(body) if isinstance(body, dict) else type(body).__name__}，"
            f"encoding={body.get('encoding') if isinstance(body, dict) else '-'}，status={body.get('status') if isinstance(body, dict) else '-'}")
        return body

    def swzx_cookies() -> list[str]:
        return sorted({f"{c.name}@{c.domain}{c.path}" for c in http.cookies.jar if "webvpn" in (c.domain or "")})

    try:
        out("--- 1. 未登录时直接调接口 ---")
        await probe("/venue-api/user/current", f"{VENUE_API}/user/current")
        await probe("/venue-api/booth/list", f"{VENUE_API}/booth/list", {"current": "1", "pageSize": "1"})
        out("--- 2. 走场地系统登录（需要已经在界面上登录过 WebVPN） ---")
        tgc = [f"{c.domain}{c.path}" for c in http.cookies.jar if c.name == "CASTGC"]
        out(f"  本机 CASTGC 所在: {tgc}（值不输出）")
        await probe("/venue-api/login", LOGIN_URL, {"front": BOOTH_PAGE})
        out(f"  WebVPN / 事务中心 Cookie 名: {swzx_cookies()}")
        out("--- 3. 登录后再调接口 ---")
        user = await probe("/venue-api/user/current", f"{VENUE_API}/user/current")
        if user is None:
            out("\n接口还是拿不到 JSON：先在界面上问一句“有哪些琴房”，按卡片登录 WebVPN，再运行一次")
            return
        user = await api.api("GET", "/user/current")
        out(f"  登录后用户字段 {sorted(user) if isinstance(user, dict) else type(user).__name__}（值不输出）")

        listing = await api.api("GET", "/booth/list", params={"current": "1", "pageSize": "9999"})
        rows = listing.get("list") or []
        out(f"[/booth/list] {len(rows)} 个，字段 {_keys(rows)}")
        for b in rows[:12]:
            out(f"  {b.get('id')}: {b.get('campus')}·{b.get('community')} {b.get('name')} status={b.get('status')} typeId={b.get('typeId')}")
        booth = next((b for b in rows if b.get("status") is not False), None)
        if not booth:
            out("没有可用的静音舱，后面的检查跳过")
            return
        bid = booth["id"]
        info = await api.api("GET", f"/booth/info/{bid}")
        out(f"[/booth/info/{bid}] 字段 {sorted(info)}，type 字段 {sorted(info.get('type') or {})}")
        if info.get("typeId"):
            t = await api.api("GET", f"/boothType/info/{info['typeId']}")
            bits = sorted(mask_bits(t.get("availableTimePeriod")))
            out(f"[/boothType/info] 字段 {sorted(t)}；开放格 {bits[:1]}…{bits[-1:]}（共 {len(bits)} 格），"
                f"每日上限 {t.get('samePersonMaxReservationPerDay')}，提前天数 {t.get('lastReservationDayBeforeAppointment')}，"
                f"offset {t.get('timeOffsetMinutes')}")
        from datetime import date, timedelta

        for d in (date.today(), date.today() + timedelta(days=1)):
            body = await raw(f"/booth/{bid}/available-time", {"startDate": d.isoformat(), "endDate": d.isoformat()})
            from .campus.booth import unwrap

            days = unwrap(body)
            for r in days if isinstance(days, list) else []:
                out(f"  {r.get('date')}: 字段 {sorted(r)}，{len(r.get('times') or [])} 格，取值计数 {dict(Counter(r.get('times') or []))}，"
                    f"offset {r.get('timeOffsetMinutes')}")
        elig = await api.api("GET", "/boothReservation/eligibility", params={"boothId": bid, "date": date.today().isoformat(), "_t": 0})
        out(f"[/boothReservation/eligibility] 字段 {sorted(elig)}，eligible={elig.get('eligible')}，"
            f"{elig.get('minDate')} 至 {elig.get('maxDate')}，提前 {elig.get('minAdvanceMinutes')} 分钟，reasons {len(elig.get('reasons') or [])} 条")
        slots = await svc.booth_slots(bid, date.today() + timedelta(days=1))
        out(f"[解析结果] 明天可约 {len(slots['freeSlots'])} 格，例如 {slots['freeSlots'][:2]}")

        body = await raw("/boothReservation/my", {"current": "1", "pageSize": "10"})
        mine = unwrap(body)
        items = mine.get("list") if isinstance(mine, dict) else mine
        out(f"  {len(items or [])} 条，记录字段 {_keys(items or [])}（值不输出）")
        out("\n自检完成（全程只读，没有提交任何预约）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
        if exc.code == "NETWORK_REQUIRED":
            out("  这个系统只能在校园网里访问，连上校园网再运行一次")
    except (ValueError, json.JSONDecodeError) as exc:
        out(f"\n✗ 返回的不是 JSON：{exc}")
    finally:
        await http.aclose()


async def check_webvpn(out=print) -> None:
    """交互式诊断：在终端里完成 WebVPN 登录 + 二次验证（和界面同一套代码：CasClient(realm="webvpn") → MfaClient.adopt）。

    密码用 getpass 输入，不显示、不记录；验证码也只在本机终端里输入。输出只有步骤结果、学校返回的结果码和页面参数，
    不含学号、Cookie 值。提交失败时，用同一个会话、同一个验证码补上 skipTmpReAuth=false 再试一次
    （2026-11-15：容器里成功那次页面 isSleepAccount=0、带了这个参数；用户电脑上是 1、按浏览器规矩不带，一直“认证失败”）。
    成功后登录状态会保存到本机，界面和工具可以直接用。
    """
    import asyncio
    import time
    from getpass import getpass

    from .campus.cas import CasClient
    from .campus.mfa import MfaClient

    async def ask(prompt: str) -> str:
        return (await asyncio.to_thread(input, prompt)).strip()

    username = await ask("学号 / 工号：")
    password = await asyncio.to_thread(getpass, "统一认证密码（输入时不显示）：")
    store = SessionStore()
    cas = CasClient(store, realm="webvpn")
    mfa = None
    try:
        t = time.monotonic()
        await cas.password_login(username, password)
        password = ""
        out(f"[1] WebVPN 登录：✓（{time.monotonic() - t:.1f}s）")
        if cas.mfa_response is None:
            out("    学校没有要求二次验证")
        else:
            mfa, info = await MfaClient.adopt(store, cas.client, cas.mfa_response)
            p = mfa.page
            out(f"[2] 二次验证页：可用方式 {[m['type'] + '=' + m['name'] for m in info['methods']]}，当前 {p['current']}，"
                f"multifactor={p['multifactor']!r} isSleepAccount={p['sleepAccount'] or '(无)'} ipAnomaly={p['ipAnomaly'] or '(无)'}")
            method = await ask(f"    用哪种方式（4=企业微信 3=短信，直接回车用 {p['current'] or '4'}）：") or p["current"] or "4"
            sent = time.monotonic()
            out(f"[3] 发送：{await mfa.send_code(method)}")
            code = await ask("    验证码：")
            out(f"    距发送 {time.monotonic() - sent:.0f} 秒")
            try:
                res = await mfa.submit(method, code)
                out("[4] 提交（和浏览器一致）：✓ 认证成功")
                if res.get("needed"):
                    out(f"    学校还要求再验证一步：{[m['name'] for m in res['methods']]}。把以上输出贴给 Claude")
                    return
            except CampusError as exc:
                out(f"[4] 提交（和浏览器一致）：✗ {exc.message}")
                if p["sleepAccount"] != "0":
                    mfa.page["sleepAccount"] = "0"  # 让 submit 带上 skipTmpReAuth=false
                    try:
                        await mfa.submit(method, code)
                        out("[4b] 同一个验证码 + skipTmpReAuth=false：✓ 认证成功 —— 原因找到了，把这行结果发给 Claude")
                    except CampusError as exc2:
                        out(f"[4b] 同一个验证码 + skipTmpReAuth=false：✗ {exc2.message}")
                        out("\n两种提交都失败。把以上输出整段贴给 Claude（不含个人信息）")
                        return
                else:
                    out("\n提交失败。把以上输出整段贴给 Claude（不含个人信息）")
                    return
        svc = RealCampus(CampusHttp(store, cache_ttl=0))
        try:
            rooms = await svc.booth_list("粤海", "琴房")
            out(f"[5] 查琴房：✓ {[b['name'] for b in rooms['booths']]}")
        finally:
            await svc.http.aclose()
        out("\nWebVPN 登录状态已保存，界面上可以直接查询 / 预约公共空间了")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
    finally:
        await (mfa.aclose() if mfa else cas.aclose())


async def check_lms(out=print) -> None:
    """畅课：单点登录跳转链（lms → identity → authserver）、my-courses 不带学期编号时返回哪些学期、作业列表字段。

    不输出课程名、作业名、分数；只输出学期名、计数和字段名。
    """
    from collections import Counter

    from .campus import lms
    from .campus.debuglog import where

    store = SessionStore()
    out(f"登录凭证: {'有' if store.info()['hasSession'] else '没有'}")
    http = CampusHttp(store, cache_ttl=0)
    try:
        resp = await http.request("GET", lms.LMS_HOME)
        out(f"[进入畅课] {resp.status_code} 落在 {where(resp.url)}")
        out(f"  跳转 {[where(r.url) for r in resp.history]}")
        out(f"  lms 的 Cookie: {sorted(c.name for c in http.cookies.jar if 'lms.szu.edu.cn' in (c.domain or ''))}")
        if resp.url.host != "lms.szu.edu.cn":
            out("  ✗ 没有跳回畅课，后面的检查跳过")
            return
        variants = (("爱小荔（不带学期编号）", lms.COURSE_CONDITIONS),
                    ("学期编号为空数组", {**lms.COURSE_CONDITIONS, "academic_year_id": [], "semester_id": []}),
                    ("不带 classify_type", {k: v for k, v in lms.COURSE_CONDITIONS.items() if k != "classify_type"}))
        courses: list[dict[str, Any]] = []
        for label, cond in variants:
            try:
                body = await lms.api(http, "POST", "/api/my-courses", json={
                    "page": 1, "page_size": lms.COURSE_PAGE_SIZE, "fields": lms.COURSE_FIELDS, "conditions": cond})
            except CampusError as exc:
                out(f"[my-courses {label}] ✗ {exc.code}: {exc.message}")
                continue
            rows = body.get("courses") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                out(f"[my-courses {label}] 结构不对：顶层 {sorted(body) if isinstance(body, dict) else type(body).__name__}")
                continue
            sems = Counter(str((r.get("semester") or {}).get("name")) for r in rows if isinstance(r, dict))
            out(f"[my-courses {label}] total={body.get('total')} pages={body.get('pages')} 本页 {len(rows)} 门；学期: {dict(sems)}")
            out(f"  is_instructor: {dict(Counter(bool(r.get('is_instructor')) for r in rows))}")
            if not courses and rows:
                courses = rows
                out(f"  字段: {_keys(rows)}")
        report = await lms.courses_report(lambda: lms.fetch_courses(http), None, lms.datetime.now(lms.CST).date())
        out(f"\n当前学期判断: {report['term']}（{len(report['courses'])} 门），可选学期 {report['availableTerms']}")
        student = [c for c in report["courses"] if c["role"] == "student"] or [lms.normalize_course(c) for c in courses][:1]
        if student:
            rows = await lms.fetch_homework(http, student[0]["id"])
            out(f"[homework-activities 第 1 门学生身份的课] {len(rows)} 条作业")
            if rows:
                out(f"  字段: {_keys(rows)}")
                out(f"  submitted: {dict(Counter(bool(r.get('submitted')) for r in rows))}，score 有值: "
                    f"{sum(1 for r in rows if r.get('score') not in (None, ''))}，有 deadline: {sum(1 for r in rows if r.get('deadline'))}")
                now = lms.datetime.now(lms.CST).replace(tzinfo=None)
                out(f"  解析后的状态: {dict(Counter(lms.normalize_homework(r, now)['status'] for r in rows))}")
        out("\n自检完成（只读；上面没有课程名、作业名和分数）")
    except CampusError as exc:
        out(f"\n✗ {exc.code}: {exc.message}")
        if exc.code in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
            out("  先在爱小荔界面上登录 / 完成二次验证，再运行一次")
    finally:
        await http.aclose()
