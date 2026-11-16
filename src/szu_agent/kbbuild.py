"""把 `szu-agent fetch` 下载的原始材料整理成资料库文档（Markdown + YAML 头）。

    uv run --group kb szu-agent kb-build 奖学金       # 读 resource/kb-raw/奖学金 → 写 knowledge/docs/scholarship/

只在整理资料时跑（需要 kb 依赖组里的 pypdf），生成的 .md 提交进仓库；运行时检索只读 .md，不依赖 pypdf。

取舍：
- 收：公告正文 + 规则类附件（名字里有 细则/办法/制度/方法/规定/标准/流程/义务/指引）的 docx / pdf 文本
- 不收：名单、申请表、汇总表、模板、承诺书、竞赛/期刊目录（要么含个人信息，要么是给人填的表，问答用不上）；
  .doc / .xls 老格式解析不了：旁边有同名 .docx（Word 另存为）就读它——同一份文件只要在任一文件夹转过一次，
  别的学院挂的那份也用上；都没有就在文档末尾列出名字，提示“见原通知”
- 用户手动放进公告文件夹的文件（如解压出来的细则）也会读，同样按名字筛
- 打码：个人手机号、非学校域名邮箱（老师留的 qq/163 邮箱）。办公电话、@szu.edu.cn 邮箱保留——用户需要知道找谁
- 同一学院同一标题出了“更新”版，只保留最新一条
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

import yaml

DOCS_DIR = Path(__file__).parent / "knowledge" / "docs"
RAW_DIR = Path("resource/kb-raw")

KEEP_ATTACHMENT = re.compile(r"细则|办法|制度|方法|规定|标准|流程|义务|指引|要求")
DROP_ATTACHMENT = re.compile(r"名单|申请表|审批表|汇总表|模板|总结|承诺书|列表|清单|目录|证明|推免|竞赛范围|检索指引")

# 学校统一下发、各学院转发时挂在附件里的校级文件：单独成篇，发文单位记为“深圳大学”，查任何学院都能检索到
SCHOOL_LEVEL = re.compile(r"^深圳大学(?!.*(学院|学部|研究院|实验室))|^深大校发|^受助学生义务")

MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 类型：按顺序匹配标题，第一个命中的算数。level：本科生 / 研究生 / 博士生 / 不限
KINDS: list[tuple[str, str, str]] = [
    (r"拔尖", "博士研究生拔尖创新人才奖学金", "博士生"),
    (r"学业奖学金", "研究生学业奖学金", "研究生"),
    (r"励志|助学金", "国家励志奖学金、助学金", "本科生"),
    (r"荔园", "荔园之星、荔园卓越之星奖学金", "本科生"),
    (r"卓越班", "卓越班奖学金", "本科生"),
    (r"鹏程", "鹏程奖学金", ""),
    (r"研究生.*(国家奖学金|社会奖学金|干部)", "研究生国家奖学金、社会奖学金、优秀研究生干部", "研究生"),
    (r"研究生", "研究生奖学金", "研究生"),
    (r"本科生.*国家奖学金", "本科生国家奖学金", "本科生"),
    (r"本科生.*(奖学金|先进班集体)", "本科生奖学金及先进班集体", "本科生"),
]


def classify(title: str) -> tuple[str, str]:
    for pattern, kind, level in KINDS:
        if re.search(pattern, title):
            if not level:
                level = "研究生" if "研究生" in title else "本科生" if "本科" in title else "不限"
            return kind, level
    return "其他奖学金", "研究生" if "研究生" in title else "本科生" if "本科" in title else "不限"


def publisher_from_title(title: str) -> str:
    m = re.match(r"^(?:深圳大学)?(.+?(?:学院|学部|研究院|实验室(?:（深圳）)?))", title)
    return m[1] if m else "未知单位"


def year_of(title: str) -> str:
    if m := re.search(r"(\d{4})\s*[-－‑–]\s*(\d{4})\s*学?年?度?", title):
        return f"{m[1]}-{m[2]} 学年"
    if m := re.search(r"(\d{4})\s*年度?", title):
        return f"{m[1]} 年"
    return ""


def normalize_title(title: str) -> str:
    """去掉“【更新】”“（有更新）”“(科研成果的备注说明有更新)”之类，用来识别同一份细则的多个版本。"""
    t = re.sub(r"[【（(][^】）)]*更新[^】）)]*[】）)]|更新$", "", title)
    return re.sub(r"\s+", "", t)


def redact(text: str) -> str:
    text = MOBILE.sub("（手机号已省略，见原通知）", text)
    return EMAIL.sub(lambda m: m[0] if m[0].lower().endswith("szu.edu.cn") else "（个人邮箱已省略，见原通知）", text)


# ------------------------------ 附件文本 ------------------------------ #

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t")).strip()


def _table_md(tbl: ET.Element) -> str:
    """Word 表格 → Markdown 表格。纵向合并（vMerge）展开成重复内容，这样每一行单独看也是完整的
    （检索切段后模型只看到一部分行时仍然读得懂）；横向合并（gridSpan）只写在第一格。"""
    grid: list[list[str]] = []
    for tr in tbl.findall(f"{W}tr"):
        row: list[str] = []
        col = 0
        for tc in tr.findall(f"{W}tc"):
            pr = tc.find(f"{W}tcPr")
            span = int(pr.find(f"{W}gridSpan").get(f"{W}val", "1")) if pr is not None and pr.find(f"{W}gridSpan") is not None else 1
            vmerge = pr.find(f"{W}vMerge") if pr is not None else None
            text = " ".join(filter(None, (_para_text(p) for p in tc.iter(f"{W}p")))).replace("|", "／")
            if vmerge is not None and vmerge.get(f"{W}val") != "restart" and grid and col < len(grid[-1]):
                text = grid[-1][col]
            row.extend([text] + [""] * (span - 1))  # 横向合并：只写在第一格
            col += span
        if any(row):
            grid.append(row)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    # 整列都空的是 Word 网格线造成的假列（横向合并留下的），去掉省 token
    keep = [j for j in range(width) if any(r[j] for r in grid)]
    grid = [[r[j] for j in keep] for r in grid]
    width = len(keep)
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(lines)


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    out: list[str] = []
    for el in body if body is not None else []:
        if el.tag == f"{W}p":
            if t := _para_text(el):
                out.append(t)
        elif el.tag == f"{W}tbl":
            if t := _table_md(el):
                out.append(t)
    return "\n\n".join(out)


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # 只在整理资料时需要：uv run --group kb

    raw = "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
    return join_wrapped_lines(raw)


HEADING = re.compile(r"^\s*([一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)]|第[一二三四五六七八九十百]+[章条节]|\d+[.、．]|附件)")


def join_wrapped_lines(text: str) -> str:
    """PDF 抽出来的文字按版面折行，一句话被拆成几行。上一行没以标点结束、下一行也不是标题时接回去。"""
    out: list[str] = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line or re.fullmatch(r"[-—\s]*\d+[-—\s]*", line):  # 页码
            continue
        if out and not re.search(r"[。；：！？:;）)]$", out[-1]) and not HEADING.match(line):
            out[-1] += line
        else:
            out.append(line)
    return "\n".join(out)


def attachment_text(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return docx_text(path)
        if suffix == ".pdf":
            return pdf_text(path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        return f"（附件解析失败：{type(exc).__name__}）"
    return None


# ------------------------------ 组装 ------------------------------ #

@dataclass
class Doc:
    id: str
    title: str
    publisher: str
    published: str
    url: str
    kind: str
    level: str
    year: str
    body: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (名字, 文本)
    unparsed: list[str] = field(default_factory=list)

    def filename(self) -> str:
        return re.sub(r'[<>:"/\\|?*\s]', "", f"{self.publisher}-{self.kind}-{self.id}") + ".md"

    def render(self, verified: date) -> str:
        meta = {"title": self.title, "publisher": self.publisher, "kind": self.kind, "level": self.level,
                "year": self.year, "published": self.published, "source": self.url, "verified_at": verified}
        parts = ["---", yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip(), "---", "",
                 f"# {self.title}", "", self.body.strip()]
        for name, text in self.attachments:
            parts += ["", f"## 附件：{name}", "", text.strip()]
        if self.unparsed:
            parts += ["", "## 未收录的附件（格式无法解析或是表格/名单，见原通知）", ""]
            parts += [f"- {n}" for n in self.unparsed]
        return redact("\n".join(parts)) + "\n"


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def notice_body(md: str) -> str:
    """fetch 存的 正文.md：标题行、三行元信息、空行之后是正文。"""
    lines = md.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("- 链接")), 3) + 1
    return "\n".join(lines[start:]).strip()


def build(raw_dir: Path, out_dir: Path, *, verified: date | None = None,
          out: Callable[[str], None] = print) -> list[Doc]:
    verified = verified or date.today()
    index: list[dict[str, Any]] = json.loads((raw_dir / "index.json").read_text(encoding="utf-8"))
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for item in index:
        item["publisher"] = item.get("publisher") or publisher_from_title(item["title"])
        key = (item["publisher"], normalize_title(item["title"]))
        if key not in latest or int(item["id"]) > int(latest[key]["id"]):
            latest[key] = item
    out(f"{len(index)} 条公告，去掉旧版本后 {len(latest)} 条")

    # 用户把 .doc 另存为同名 .docx 放在旁边：按原文件内容哈希记下来，别的学院挂的同一份 .doc 也能用上
    converted: dict[str, Path] = {}
    for old in list(raw_dir.glob("*/*.doc")) + list(raw_dir.glob("*/*.xls")):
        if old.with_suffix(".docx").exists():
            converted[sha1(old)] = old.with_suffix(".docx")
    seen_attachments: dict[str, str] = {}  # 内容哈希 → 第一次出现的文档标题（学校统一下发的附件各学院都会挂一份）
    docs: list[Doc] = []
    school: list[Doc] = []
    for item in sorted(latest.values(), key=lambda x: int(x["id"])):
        folder = raw_dir / item["folder"]
        kind, level = classify(item["title"])
        doc = Doc(item["id"], item["title"], item["publisher"], (item.get("publishedAt") or "")[:10],
                  item["url"], kind, level, year_of(item["title"]),
                  notice_body((folder / "正文.md").read_text(encoding="utf-8")))
        listed = set(item["attachments"])
        # 用户手动放进来的文件（解压出来的、另存为的）也读；另存为的 .docx 已经通过原 .doc 处理，不重复
        extra = [str(p.relative_to(folder)) for p in sorted(folder.rglob("*")) if p.is_file()
                 and p.name != "正文.md" and not p.name.startswith("~$")  # Word 打开文件时的临时锁文件
                 and str(p.relative_to(folder)) not in listed
                 and not (p.suffix.lower() == ".docx" and (p.with_suffix(".doc").name in listed or p.with_suffix(".xls").name in listed))]
        for name in item["attachments"] + extra:
            clean = re.sub(r"^\d{8,}", "", Path(name).name)
            if DROP_ATTACHMENT.search(clean) or not KEEP_ATTACHMENT.search(clean):
                continue
            path = folder / name
            digest = sha1(path)  # 按原文件去重：同一份 .doc 各学院都挂了，转没转 docx 都算同一份
            if path.suffix.lower() in {".doc", ".xls"}:
                path = path.with_suffix(".docx") if path.with_suffix(".docx").exists() else converted.get(digest, path)
            if digest in seen_attachments:
                doc.unparsed.append(f"{clean}（与《{seen_attachments[digest]}》的附件相同）")
                continue
            text = attachment_text(path)
            if text is None:
                doc.unparsed.append(clean)
                continue
            seen_attachments[digest] = item["title"]
            stem = re.sub(r"^附件\S{0,3}?[：:.\-、]\s*|^附件(?=深圳大学)", "", Path(clean).stem)
            if SCHOOL_LEVEL.search(stem):
                school_kind, school_level = classify(stem)
                school.append(Doc(f"{item['id']}-{len(school)}", stem, "深圳大学", doc.published, item["url"],
                                  school_kind, school_level, year_of(stem),
                                  f"（校级文件，随《{item['title']}》附发）\n\n{text}"))
                doc.unparsed.append(f"{clean}（校级文件，已单独收录为《{stem}》）")
                continue
            doc.attachments.append((stem, text))
        docs.append(doc)
    # 同名校级文件各学院挂的版本略有出入（有的多附一份承诺书），留内容最全的一份
    by_title: dict[str, Doc] = {}
    for d in school:
        if d.title not in by_title or len(d.body) > len(by_title[d.title].body):
            by_title[d.title] = d
    docs += by_title.values()
    # 其他学院挂的同名校级文件版本（哈希不同、没另存为 docx）：已经有一份收录了，就不要再标“未收录”
    for d in docs:
        d.unparsed = [f"{u}（校级文件，已单独收录为《{t}》）"
                      if (t := re.sub(r"^附件\S{0,3}?[：:.\-、]\s*", "", Path(u).stem)) in by_title and re.search(r"\.\w{2,5}$", u) else u
                      for u in d.unparsed]

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()
    for d in docs:
        (out_dir / d.filename()).write_text(d.render(verified), encoding="utf-8")
    out(f"写入 {len(docs)} 篇到 {out_dir}（附件收录 {sum(len(d.attachments) for d in docs)} 份）")
    return docs
