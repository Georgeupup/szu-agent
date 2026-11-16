"""终端入口。

    szu-agent                 # 对话（真实模式）
    szu-agent --mock          # 对话（演示模式）
    szu-agent login           # 扫码登录深大统一身份认证
    szu-agent login --password  # 账号密码登录（密码用 getpass 输入，不回显、不进入对话）
    szu-agent logout
    szu-agent check sports      # 只读自检：体育预约接口返回的字段和名称，输出可以直接贴出来排查
    szu-agent check library [书目编号]  # 只读自检：图书详情、预借登记页的表单字段和取书点、预检结果
    szu-agent check booth       # 只读自检：事务中心静音舱的登录跳转、列表、时段、资格
    szu-agent check webvpn      # 交互式诊断：终端里完成 WebVPN 登录 + 二次验证（失败时自动试 skipTmpReAuth=false）
    szu-agent usage [--days 7]  # 模型用量：token、缓存命中率、估算花费（网页版在 /usage）
    szu-agent log [正则]        # 打印诊断日志里最近的相关行（默认：二次验证和事务中心）
    szu-agent kb-build 奖学金   # 把 resource/kb-raw/奖学金 整理成 knowledge/docs/scholarship/*.md（需要 uv run --group kb）
    szu-agent fetch 关键词 [--include 正则] [--exclude 正则]  # 把公文通里的一类公告和附件下载到 resource/kb-raw/（整理资料库用）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import uuid
import webbrowser

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent.graph import message_text
from .agent.profile import ProfileStore
from .agent.runtime import open_runtime
from .campus import CampusError, SessionStore, agent_home, make_cas

console = Console()

HELP = "命令：/login 登录  /new 新对话  /profile 查看记忆  /exit 退出"


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items())


def _show_board(args: dict) -> None:
    """终端里的通知面板：同一份结构化数据，换一种渲染方式。"""
    from rich.table import Table

    console.print(Panel(f"[bold]{args.get('topic', '')}[/bold]\n{args.get('summary', '')}", border_style="magenta"))
    for g in args.get("groups", []):
        t = Table(title=g.get("name"), title_justify="left", show_lines=True, expand=True)
        t.add_column("类型", width=9)
        t.add_column("通知")
        t.add_column("截止", width=10)
        for c in g.get("items", []):
            body = f"[bold]{c.get('title', '')}[/bold]\n{c.get('summary', '')}"
            if c.get("action"):
                body += f"\n[magenta]下一步：{c['action']}[/magenta]"
            t.add_row(c.get("kind", ""), body, c.get("deadline") or "")
        console.print(t)


def _show_update(update: dict) -> None:
    """把图的每一步执行过程打印出来，方便看清 agent 在做什么。"""
    for node, payload in update.items():
        if not payload or node == "__interrupt__":
            continue
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        for msg in messages:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls:
                    if tc["name"] == "show_notice_board":
                        _show_board(tc["args"])
                        continue
                    console.print(f"  [dim]🔧 {tc['name']}({_fmt_args(tc['args'])})[/dim]")
                if msg.content and not msg.tool_calls:
                    console.print(Panel(Markdown(message_text(msg)), title="爱小荔", border_style="green"))
            elif isinstance(msg, ToolMessage):
                text = message_text(msg)
                ok = '"ok": false' not in text and '"ok":false' not in text
                console.print(f"  [dim]{'✓' if ok else '✗'} {msg.name}[/dim]")
                if "LOGIN_REQUIRED" in text:
                    console.print("  [yellow]需要登录：输入 /login 扫码（或在另一个终端运行 szu-agent login --password）[/yellow]")


def _format_preview(tool: str, preview: dict | None) -> str:
    if not preview:
        return "未找到对应的预览结果（令牌可能是伪造的，服务端会拒绝执行）"
    if tool == "library_prebook_confirm":
        rows = [("图书", preview.get("book")), ("通知手机", preview.get("phone")), ("通知邮箱", preview.get("email"))]
        return "\n".join(f"{k}：{v}" for k, v in rows if v)
    if tool == "library_prebook_cancel_confirm":
        rows = [("图书", preview.get("title")), ("取书地点", preview.get("pickup")), ("状态", preview.get("status"))]
        return "\n".join(f"{k}：{v}" for k, v in rows if v)
    if tool == "sports_cancel_confirm":
        rows = [("项目", preview.get("project")), ("场地", preview.get("field")), ("时间", preview.get("timeRange")),
                ("状态", preview.get("payState")), ("订单号", preview.get("orderNo"))]
        return "\n".join(f"{k}：{v}" for k, v in rows if v)
    slot = preview.get("selected") or {}
    rows = [
        ("校区", preview.get("campus")),
        ("项目", preview.get("venue")),
        ("方式", preview.get("mode")),
        ("场地", preview.get("field")),
        ("日期", preview.get("date")),
        ("时段", slot.get("label")),
        ("余量", slot.get("remaining")),
    ]
    if any(v for _, v in rows):
        return "\n".join(f"{k}：{v}" for k, v in rows if v is not None) + "\n\n提交后需在学校系统手动支付"
    return json.dumps(preview, ensure_ascii=False, indent=2)


def _ask_confirmation(payload: dict) -> dict:
    console.print()
    for action in payload.get("actions", []):
        title = {"sports_reserve_confirm": "确认预约", "library_prebook_confirm": "确认预借",
                 "library_prebook_cancel_confirm": "确认取消预借"}.get(action["tool"], "确认取消预约")
        body = _format_preview(action["tool"], action.get("preview"))
        console.print(Panel(body, title=f"⚠️  {title}", border_style="yellow", expand=False))
    choice = {}
    for action in payload.get("actions", []):
        options = (action.get("preview") or {}).get("pickupOptions") or []
        if action["tool"] == "library_prebook_confirm" and options:
            for i, o in enumerate(options, 1):
                console.print(f"  {i}. {o['name']}")
            default = next((i for i, o in enumerate(options, 1) if o["value"] == action["preview"].get("pickupId")), None)
            while True:
                n = console.input(f"[bold yellow]选择取书地点 1-{len(options)}{f'（回车={default}）' if default else ''}：[/bold yellow]").strip()
                if not n and default:
                    n = str(default)
                if n.isdigit() and 1 <= int(n) <= len(options):
                    choice["pickup"] = options[int(n) - 1]["value"]
                    break
    answer = console.input("[bold yellow]执行这个操作吗？(y/N，或输入拒绝原因) [/bold yellow]").strip()
    if answer.lower() in {"y", "yes", "是", "确认"}:
        return {"approved": True, **choice}
    return {"approved": False, "reason": answer if answer.lower() not in {"", "n", "no"} else None}


async def _run_turn(graph, inputs, config) -> None:
    while True:
        interrupted = None
        async for update in graph.astream(inputs, config, stream_mode="updates"):
            if "__interrupt__" in update:
                interrupted = update["__interrupt__"][0].value
            _show_update(update)
        if interrupted is None:
            return
        inputs = Command(resume=_ask_confirmation(interrupted))


async def amain(mock: bool) -> None:
    load_dotenv()
    profile = ProfileStore()
    console.print(Panel(
        f"深大校园事务助手{'  [yellow](演示模式：mock 数据)[/yellow]' if mock else ''}\n{HELP}",
        border_style="cyan",
    ))
    with console.status("正在启动 MCP Server…"):
        ctx = open_runtime(mock=mock, profile=profile)
        runtime = await ctx.__aenter__()
        graph = runtime.graph
    try:
        thread_id = str(uuid.uuid4())
        while True:
            try:
                text = console.input("\n[bold cyan]你：[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "/exit":
                break
            if text == "/new":
                thread_id = str(uuid.uuid4())
                console.print("[dim]已开始新对话[/dim]")
                continue
            if text == "/login":
                await login(mock, use_password=False)
                continue
            if text == "/profile":
                console.print(profile.render())
                continue
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 30}
            try:
                await runtime.threads.touch(thread_id, text)  # 终端里的对话也会出现在网页的“历史”里
                await _run_turn(graph, {"messages": [("user", text)]}, config)
            except Exception as exc:  # 网络/LLM 错误不应该让整个程序退出
                console.print(f"[red]出错了：{type(exc).__name__}: {exc}[/red]")
    finally:
        await ctx.__aexit__(None, None, None)


async def login(mock: bool, use_password: bool) -> None:
    store = SessionStore(mock=mock)
    cas = make_cas(mock=mock, store=store)
    try:
        if use_password:
            username = console.input("学号/工号：").strip()
            password = getpass.getpass("密码（不回显）：")
            await cas.password_login(username, password)
        else:
            image = await cas.qr_image()
            # 终端里画不出可靠的二维码，存成图片再用系统默认程序打开
            header, b64 = image.split(",", 1)
            path = agent_home() / ("login-qr.svg" if "svg" in header else "login-qr.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            import base64

            path.write_bytes(base64.b64decode(b64))
            console.print(f"请用[bold]企业微信[/bold]扫描二维码：{path}")
            webbrowser.open(path.as_uri())
            with console.status("等待扫码…") as status:
                while True:
                    state = await cas.qr_poll()
                    if state == "scanned":
                        status.update("已扫码，请在手机上确认…")
                    if state == "confirmed":
                        break
                    if state == "expired":
                        raise CampusError("LOGIN_FAILED", "二维码已过期，请重新运行 szu-agent login")
                    await asyncio.sleep(1.5)
            path.unlink(missing_ok=True)
        console.print(f"[green]✓ 登录成功[/green]，凭证保存在 {store.path}（仅本机可读）")
    except CampusError as exc:
        console.print(f"[red]登录失败：{exc.message}[/red]")
        if exc.code == "CAPTCHA_REQUIRED":
            console.print("改用扫码：szu-agent login")
    finally:
        await cas.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="深大校园事务 Agent")
    parser.add_argument("--mock", action="store_true", help="使用 mock 数据演示，不访问校园系统")
    sub = parser.add_subparsers(dest="cmd")
    p_login = sub.add_parser("login", help="登录深大统一身份认证")
    p_login.add_argument("--password", action="store_true", help="用账号密码登录（默认扫码）")
    p_login.add_argument("--mock", action="store_true")
    p_check = sub.add_parser("check", help="用本机登录凭证对学校接口做只读自检，输出不含个人信息")
    p_check.add_argument("target", choices=["sports", "library", "booth", "webvpn"])
    p_check.add_argument("ctrlno", nargs="?", default="3351727", help="library：书目编号（默认《想念地坛》）")
    p_log = sub.add_parser("log", help="打印诊断日志里最近的相关行（只有地址、状态码、结果码，可以直接贴出来）")
    p_log.add_argument("pattern", nargs="?", default="mfa|swzx|webvpn|authserver-443", help="正则，默认看二次验证和事务中心")
    p_log.add_argument("-n", type=int, default=60, help="最多显示多少行")
    p_fetch = sub.add_parser("fetch", help="把公文通里的一类公告（正文 + 附件）下载到本地，整理资料库用")
    p_fetch.add_argument("keyword", help="公文通搜索关键词，如“奖学金”")
    p_fetch.add_argument("--include", help="标题必须匹配的正则，如“细则|办法”")
    p_fetch.add_argument("--exclude", default=None, help="标题排除的正则（默认排除公示、名单、结果等含个人信息的公告）")
    p_fetch.add_argument("--range", default="6m", help="时间范围：24h / 7d / 30d / 6m（默认）")
    p_fetch.add_argument("--in", dest="search_in", default="title", choices=["title", "body", "full"])
    p_fetch.add_argument("--out", help="保存目录，默认 resource/kb-raw/<关键词>")
    p_kb = sub.add_parser("kb-build", help="把 fetch 下载的原始材料整理成资料库文档（uv run --group kb）")
    p_kb.add_argument("name", help="resource/kb-raw 下的目录名，如“奖学金”")
    p_kb.add_argument("--category", default="scholarship", help="写到 knowledge/docs/ 下的哪个目录")
    p_usage = sub.add_parser("usage", help="模型用量：token、缓存命中率、估算花费")
    p_usage.add_argument("--days", type=int, default=7)
    p_logout = sub.add_parser("logout", help="删除本机保存的登录凭证")
    p_logout.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    if args.cmd == "login":
        asyncio.run(login(args.mock, args.password))
    elif args.cmd == "check":
        from .selfcheck import check_booth, check_library, check_sports, check_webvpn

        # markup=False：输出里的 [bookinfo.aspx] 这类方括号会被 rich 当成样式标签吞掉
        def plain(text: str) -> None:
            console.print(text, markup=False)

        if args.target == "library":
            asyncio.run(check_library(args.ctrlno, plain))
        elif args.target == "booth":
            asyncio.run(check_booth(plain))
        elif args.target == "webvpn":
            asyncio.run(check_webvpn(plain))
        else:
            asyncio.run(check_sports(plain))
    elif args.cmd == "log":
        import re

        from .campus.session import agent_home

        path = agent_home() / "debug.log"
        console.print(f"日志文件：{path}", markup=False)
        lines: list[str] = []
        for p in (path.with_name("debug.log.1"), path):  # 轮转出去的旧文件在前
            if p.exists():
                lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [ln for ln in lines if re.search(args.pattern, ln)]
        if not lines:
            console.print("日志还是空的：先在界面上操作一次（比如问“粤海有哪些静音舱”），再运行这个命令", markup=False)
        for ln in hits[-args.n:]:
            console.print(ln, markup=False)
    elif args.cmd == "fetch":
        from pathlib import Path

        from .fetchdocs import DEFAULT_EXCLUDE, fetch_notices, safe_name

        out_dir = Path(args.out) if args.out else Path("resource/kb-raw") / safe_name(args.keyword)
        try:
            asyncio.run(fetch_notices(args.keyword, include=args.include,
                                      exclude=DEFAULT_EXCLUDE if args.exclude is None else args.exclude,
                                      range_=args.range, search_in=args.search_in, out_dir=out_dir,
                                      out=lambda t: console.print(t, markup=False)))
        except CampusError as exc:
            console.print(f"[red]{exc.message}[/red]")
            if exc.code in {"LOGIN_REQUIRED", "MFA_REQUIRED"}:
                console.print("先运行 uv run szu-agent login 登录，再重试")
    elif args.cmd == "kb-build":
        from .kbbuild import DOCS_DIR, RAW_DIR, build

        build(RAW_DIR / args.name, DOCS_DIR / args.category, out=lambda t: console.print(t, markup=False))
    elif args.cmd == "usage":
        from .agent.pricing import SOURCE_URL, VERIFIED_AT
        from .agent.usage import UsageStore

        r = UsageStore().report(args.days)
        t = r["total"]
        pct = lambda x: "—" if x is None else f"{x * 100:.1f}%"  # noqa: E731
        cost = "—（当前模型没有配置价格）" if t["cost"] is None else f"¥{t['cost']:.4f}"
        console.print(f"近 {args.days} 天：{t['calls']} 次调用，估算花费 {cost}", markup=False)
        console.print(f"输入 {t['input']:,} tokens（缓存命中 {t['hit']:,}，未命中 {t['miss']:,}，命中率 {pct(t['hitRate'])}）；"
                      f"输出 {t['output']:,} tokens", markup=False)
        for d in r["daily"]:
            console.print(f"  {d['day']}  {d['calls']:>4} 次  输入 {d['input']:>9,}  命中率 {pct(d['hitRate']):>6}  "
                          f"输出 {d['output']:>8,}  ¥{d['cost'] or 0:.4f}", markup=False)
        console.print(f"价格来源 {SOURCE_URL}（{VERIFIED_AT} 核对），以 DeepSeek 后台账单为准", markup=False)
    elif args.cmd == "logout":
        SessionStore(mock=args.mock).clear()
        console.print("已退出登录")
    else:
        asyncio.run(amain(args.mock))


if __name__ == "__main__":
    main()
