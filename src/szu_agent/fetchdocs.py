"""把公文通里的一类文件批量下载到本地，作为整理资料库的原始材料（不进对话、不给模型）。

    uv run szu-agent fetch 奖学金 --include "细则|办法|方案" --exclude "公示|名单|结果|拟"

在你自己的电脑上用本机登录凭证运行（公文通只能在校园网 / 登录后访问）。
每条公告存一个文件夹：正文.md（标题、发文单位、时间、链接、正文）+ 原附件；外加 index.json 汇总。

隐私：名单、公示类公告里有学号姓名，默认用 --exclude 排除；下载目录 resource/kb-raw/ 已加入 .gitignore，
只把整理后的规则文本放进资料库。请求逐条串行、间隔 1 秒，不给学校服务器添负担。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .campus import CampusError, SessionStore
from .campus.http import CampusHttp
from .campus.service import RealCampus

DEFAULT_EXCLUDE = "公示|名单|结果|拟|获奖|表彰"


def safe_name(name: str, limit: int = 80) -> str:
    """Windows 文件名不能有 <>:"/\\|?*，也不能以点或空格结尾。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:limit] or "未命名"


def attachment_filename(att: dict[str, str]) -> str:
    name = att["name"]
    if not re.search(r"\.[A-Za-z0-9]{2,5}$", name):  # 链接文字没有扩展名时，从地址里补
        if m := re.search(r"\.(docx?|xlsx?|pptx?|pdf|zip|rar|7z)(?:$|\?)", unquote(att["url"]), re.I):
            name += "." + m[1].lower()
    return safe_name(name, 120)


def select(items: list[dict[str, Any]], include: str | None, exclude: str | None) -> list[dict[str, Any]]:
    return [i for i in items
            if (not include or re.search(include, i["title"])) and not (exclude and re.search(exclude, i["title"]))]


async def fetch_notices(keyword: str, *, include: str | None, exclude: str | None, range_: str, search_in: str,
                        out_dir: Path, out: Callable[[str], None] = print, svc: RealCampus | None = None) -> list[dict]:
    own = svc is None
    http = None
    if own:
        http = CampusHttp(SessionStore(), cache_ttl=0)
        svc = RealCampus(http)
    try:
        res = await svc.notice_search(keyword, search_in=search_in, range=range_, limit=1000)
        items = select(res["items"], include, exclude)
        out(f"公文通搜索“{keyword}”：共 {res['total']} 条，本页拿到 {len(res['items'])} 条，筛选后 {len(items)} 条")
        if res["total"] > len(res["items"]):
            out("  注意：结果不止一页，只处理了第一页。可以换更具体的关键词，或缩小时间范围分几次下载")
        out_dir.mkdir(parents=True, exist_ok=True)
        index: list[dict[str, Any]] = []
        for n, it in enumerate(items, 1):
            out(f"[{n}/{len(items)}] {it['date']} {it['publisher']} {it['title']}")
            folder = out_dir / safe_name(f"{it['date']}_{it['publisher']}_{it['id']}")
            folder.mkdir(exist_ok=True)
            try:
                detail = await svc.notice_view(it["id"])
            except CampusError as exc:
                out(f"    正文读取失败：{exc.message}")
                continue
            (folder / "正文.md").write_text(
                f"# {detail['title']}\n\n- 发文单位：{detail['publisher']}\n- 发布时间：{detail['publishedAt']}\n"
                f"- 链接：{detail['url']}\n\n{detail['contentText']}\n", encoding="utf-8")
            files = []
            for att in detail["attachments"]:
                fname = attachment_filename(att)
                try:
                    content, _ = await svc.notice_attachment(att["url"], it["id"])
                except CampusError as exc:
                    out(f"    附件 {fname} 下载失败：{exc.message}")
                    continue
                (folder / fname).write_bytes(content)
                files.append(fname)
                out(f"    附件 {fname}（{len(content) // 1024} KB）")
                await asyncio.sleep(1)
            index.append({"id": it["id"], "title": detail["title"], "publisher": detail["publisher"],
                          "publishedAt": detail["publishedAt"], "url": detail["url"],
                          "folder": folder.name, "attachments": files})
            await asyncio.sleep(1)
        (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        out(f"\n完成：{len(index)} 条，保存在 {out_dir.resolve()}")
        return index
    finally:
        if own and http is not None:
            await http.aclose()
