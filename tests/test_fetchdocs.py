"""公文通批量下载：筛选、文件名、落盘结构（用假的 service，不访问网络）。"""

import json

from szu_agent.campus import CampusError
from szu_agent.fetchdocs import DEFAULT_EXCLUDE, attachment_filename, fetch_notices, safe_name, select


def test_select_excludes_lists_with_personal_info():
    items = [{"title": t} for t in ("计算机学院奖学金评选细则", "2026年奖学金获奖名单公示", "外国语学院奖学金评定办法")]
    kept = select(items, "细则|办法", DEFAULT_EXCLUDE)
    assert [i["title"] for i in kept] == ["计算机学院奖学金评选细则", "外国语学院奖学金评定办法"]


def test_filenames_are_windows_safe():
    assert safe_name('附件1：评分表?.docx') == "附件1：评分表_.docx"
    assert attachment_filename({"name": "附件1", "url": "https://x/down1oad.asp?file=a%2Edocx"}) == "附件1.docx"


class FakeCampus:
    async def notice_search(self, keyword, **kw):
        return {"total": 2, "items": [
            {"id": "1", "date": "2026-09-20", "publisher": "计算机学院", "title": "计算机学院奖学金评选细则"},
            {"id": "2", "date": "2026-10-20", "publisher": "计算机学院", "title": "奖学金名单公示"}]}

    async def notice_view(self, nid):
        return {"title": "计算机学院奖学金评选细则", "publisher": "计算机学院", "publishedAt": "2026-09-20 10:00",
                "url": "https://www1.szu.edu.cn/board/view.asp?id=1", "contentText": "第一条 ……",
                "attachments": [{"name": "细则.pdf", "url": "u1"}, {"name": "坏链接.doc", "url": "u2"}]}

    async def notice_attachment(self, url, nid):
        if url == "u2":
            raise CampusError("NOT_FOUND", "附件失效")
        return b"%PDF-1.4", "application/pdf"


async def test_fetch_writes_folder_per_notice(tmp_path, monkeypatch):
    monkeypatch.setattr("szu_agent.fetchdocs.asyncio.sleep", lambda s: _noop())
    lines = []
    index = await fetch_notices("奖学金", include="细则", exclude=DEFAULT_EXCLUDE, range_="6m", search_in="title",
                                out_dir=tmp_path, out=lines.append, svc=FakeCampus())
    assert len(index) == 1 and index[0]["attachments"] == ["细则.pdf"]
    folder = tmp_path / "2026-09-20_计算机学院_1"
    assert (folder / "细则.pdf").read_bytes() == b"%PDF-1.4"
    assert "第一条" in (folder / "正文.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))[0]["id"] == "1"
    assert any("附件失效" in ln for ln in lines)


async def _noop():
    return None


# ------------------------------ kb-build：原始材料 → 资料库文档 ------------------------------ #

def test_classify_and_metadata():
    from szu_agent.kbbuild import classify, normalize_title, publisher_from_title, redact, year_of

    assert classify("材料学院2026年博士研究生拔尖创新人才奖学金评选细则") == ("博士研究生拔尖创新人才奖学金", "博士生")
    assert classify("法学院2026年本科生国家励志奖学金及助学金评选细则")[0] == "国家励志奖学金、助学金"
    assert classify("人工智能学院2025-2026学年度本科生鹏程奖学金评审细则") == ("鹏程奖学金", "本科生")
    assert publisher_from_title("深圳大学传播学院国家励志奖学金评选细则") == "传播学院"
    assert year_of("经济学院2025-2026学年度本科生奖学金") == "2025-2026 学年"
    assert normalize_title("【更新】人工智能学院细则") == normalize_title("人工智能学院细则（有更新）")
    assert "13812345678" not in redact("电话 13812345678 邮箱 abc@qq.com 或 jwb@szu.edu.cn")
    assert "jwb@szu.edu.cn" in redact("jwb@szu.edu.cn") and "abc@qq.com" not in redact("abc@qq.com")


def test_docx_table_to_markdown(tmp_path):
    """Word 表格：横向合并只写一次、纵向合并向下展开、全空的假列去掉。"""
    import zipfile

    from szu_agent.kbbuild import docx_text

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    cell = lambda t, pr="": f"<w:tc><w:tcPr>{pr}</w:tcPr><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>"  # noqa: E731
    xml = (f'<w:document {w}><w:body><w:p><w:r><w:t>一、加分标准</w:t></w:r></w:p><w:tbl>'
           f'<w:tr>{cell("类别")}{cell("项目", "<w:gridSpan w:val=\"2\"/>")}{cell("分值")}</w:tr>'
           f'<w:tr>{cell("学生干部", "<w:vMerge w:val=\"restart\"/>")}{cell("班长", "<w:gridSpan w:val=\"2\"/>")}{cell("2分")}</w:tr>'
           f'<w:tr>{cell("", "<w:vMerge/>")}{cell("团支书", "<w:gridSpan w:val=\"2\"/>")}{cell("1分")}</w:tr>'
           '</w:tbl></w:body></w:document>')
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
    text = docx_text(path)
    assert "一、加分标准" in text
    assert "| 类别 | 项目 | 分值 |" in text
    assert "| 学生干部 | 团支书 | 1分 |" in text  # 纵向合并展开


def test_join_wrapped_pdf_lines():
    from szu_agent.kbbuild import join_wrapped_lines

    raw = "第一条 为激励研究生勤奋学习，\n全面提高培养质量。\n- 2 -\n第二条 参评对象为全日制研究生。"
    assert join_wrapped_lines(raw) == "第一条 为激励研究生勤奋学习，全面提高培养质量。\n第二条 参评对象为全日制研究生。"


def test_build_uses_converted_docx_and_extra_files(tmp_path):
    """同一份 .doc 只在一个文件夹里另存为 .docx，另一个学院挂的那份也能用上；手动放进来的文件也会读。"""
    import zipfile

    from szu_agent.kbbuild import build

    def docx(path, text):
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", f'<w:document {w}><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')

    raw = tmp_path / "raw"
    index = []
    for nid, pub in (("1", "法学院"), ("2", "人文学院")):
        folder = raw / f"2026-10-20_{pub}_{nid}"
        folder.mkdir(parents=True)
        (folder / "正文.md").write_text(f"# {pub}细则\n\n- 发文单位：{pub}\n- 发布时间：2026-10-20\n- 链接：https://x/{nid}\n\n一、条件\n正文", encoding="utf-8")
        (folder / "学院评定细则.doc").write_bytes(b"\xd0\xcf\x11\xe0 same old doc")
        index.append({"id": nid, "title": f"{pub}2026年本科生国家励志奖学金评选细则", "publisher": pub,
                      "publishedAt": "2026-10-20", "url": f"https://x/{nid}", "folder": folder.name,
                      "attachments": ["学院评定细则.doc"]})
    docx(raw / "2026-10-20_法学院_1" / "学院评定细则.docx", "第一条 另存为后的内容")
    (raw / "2026-10-20_人文学院_2" / "解压").mkdir()
    docx(raw / "2026-10-20_人文学院_2" / "解压" / "补充办法.docx", "补充办法正文")
    (raw / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    docs = {d.publisher: d for d in build(raw, tmp_path / "out", out=lambda _: None)}
    assert docs["法学院"].attachments == [("学院评定细则", "第一条 另存为后的内容")]
    assert docs["人文学院"].attachments == [("补充办法", "补充办法正文")]  # 同一份 .doc 已在法学院收录，这里不重复
    assert any("相同" in u for u in docs["人文学院"].unparsed)
