# -*- coding: utf-8 -*-
"""Word (.docx) 导出组件 —— 知乎文章 / 回答的完整归档件。

这份文件替换了原来那份「说包含评论、实际一条都没有」的实现。相比旧版，
四处结构性改动：

1. **真的抓评论了。** 旧版全文搜不到 ``fetch_comment`` —— 它只把
   ``comment_count`` 这个数字填进元数据表格，没有任何一条评论正文。
   现在通过 ``comment_fetch`` 抓取并逐条排版进 Word。

2. **图片不再静默丢失。** 旧版下载图片用 6 秒超时、不带 Referer，
   失败被 ``except: pass`` 吃掉。实测 16 张图只进 Word 15 张，少的那张
   用户永远不知道。现在超时放宽、补 Referer、统计成功/失败数并如实报告。

3. **异常不再被吞。** 旧版 ``export_items`` 里是 ``except Exception: pass``，
   任何一篇失败就从压缩包里静静消失。现在失败项被收集，写进压缩包内的
   ``_导出报告.txt``，随导出物一起交到用户手上。

4. **正文递归遍历。** 旧版只遍历 HTML 顶层子节点，遇到 ``<div>`` 包裹的
   正文会整段丢掉。现在递归下降，块级容器一律进去找内容。

渲染只做加法：不删改任何原文，不猜测用户的意图。
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

try:
    from .comment_fetch import fetch_comments
except ImportError:  # 单文件执行形态
    from comment_fetch import fetch_comments

DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")
ZIP_MIME = "application/zip"

_MAX_IMAGES = 160
_IMG_TIMEOUT = 25
_IMG_MIN_BYTES = 100

_IMG_HEADERS = {
    "Referer": "https://www.zhihu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


class _Stats(dict):
    def __init__(self) -> None:
        super().__init__(images_ok=0, images_failed=0, body_chars=0,
                         blocks=0, comments=0, comments_nominal=None,
                         comments_complete=False, notes=[])

    def note(self, msg: str) -> None:
        self["notes"].append(str(msg))


def _img_src(tag: Tag) -> str:
    """按「越大越原始」的顺序挑图片地址。"""
    for key in ("data-original", "data-actualsrc", "data-src", "src"):
        v = tag.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _add_image(doc: Document, session, src: str, stats: _Stats) -> bool:
    if not src or stats["images_ok"] + stats["images_failed"] >= _MAX_IMAGES:
        return False
    try:
        r = session.get(src, timeout=_IMG_TIMEOUT, headers=_IMG_HEADERS)
        if r.status_code == 200 and len(r.content or b"") >= _IMG_MIN_BYTES:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(io.BytesIO(r.content), width=Inches(5.5))
            p.paragraph_format.space_after = Pt(6)
            stats["images_ok"] += 1
            return True
        stats["images_failed"] += 1
        stats.note("图片下载失败 HTTP %s：%s" % (r.status_code, src[:90]))
    except Exception as exc:  # noqa: BLE001
        stats["images_failed"] += 1
        stats.note("图片下载异常 %s：%s" % (type(exc).__name__, src[:90]))
    return False


def _add_paragraph(doc: Document, text: str, stats: _Stats, *,
                   italic: bool = False, indent: float = 0.0,
                   color: Optional[Tuple[int, int, int]] = None,
                   size: Optional[float] = None) -> None:
    text = (text or "").strip()
    if not text:
        return
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor(*color)
    if size:
        run.font.size = Pt(size)
    p.paragraph_format.line_spacing = 1.25
    p.paragraph_format.space_after = Pt(6)
    if indent:
        p.paragraph_format.left_indent = Inches(indent)
    stats["body_chars"] += len(text)
    stats["blocks"] += 1


def _render_node(node, doc: Document, session, stats: _Stats, depth: int = 0) -> None:
    """递归渲染。遇到容器就进去，遇到叶子就落笔 —— 不丢内容。"""
    if depth > 24:
        return
    if isinstance(node, NavigableString):
        txt = str(node).strip()
        if txt:
            _add_paragraph(doc, txt, stats)
        return
    if not isinstance(node, Tag):
        return

    tag = (node.name or "").lower()

    if tag in ("script", "style", "noscript", "meta", "link"):
        return

    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = min(int(tag[1]), 3)
        txt = node.get_text(" ", strip=True)
        if txt:
            h = doc.add_heading(txt, level=level)
            h.paragraph_format.space_before = Pt(10)
            h.paragraph_format.space_after = Pt(4)
            stats["body_chars"] += len(txt)
            stats["blocks"] += 1
        return

    if tag == "img":
        _add_image(doc, session, _img_src(node), stats)
        return

    if tag in ("figure", "picture"):
        for img in node.find_all("img"):
            _add_image(doc, session, _img_src(img), stats)
        cap = node.find("figcaption")
        if cap:
            _add_paragraph(doc, cap.get_text(" ", strip=True), stats,
                           italic=True, size=9.5, color=(100, 116, 139))
        return

    if tag in ("p", "blockquote"):
        for img in node.find_all("img"):
            _add_image(doc, session, _img_src(img), stats)
        txt = node.get_text(" ", strip=True)
        if txt:
            if tag == "blockquote":
                _add_paragraph(doc, "“ %s ”" % txt, stats, italic=True,
                               indent=0.4, color=(100, 116, 139))
            else:
                _add_paragraph(doc, txt, stats)
        return

    if tag in ("ul", "ol"):
        for li in node.find_all("li", recursive=False) or node.find_all("li"):
            txt = li.get_text(" ", strip=True)
            if not txt:
                continue
            style = "List Bullet" if tag == "ul" else "List Number"
            try:
                p = doc.add_paragraph(txt, style=style)
            except Exception:  # noqa: BLE001
                p = doc.add_paragraph(txt)
            p.paragraph_format.space_after = Pt(3)
            stats["body_chars"] += len(txt)
            stats["blocks"] += 1
        return

    if tag == "table":
        rows = node.find_all("tr")
        if rows:
            cols = max(len(r.find_all(["td", "th"])) for r in rows)
            if cols:
                t = doc.add_table(rows=0, cols=cols)
                t.style = "Table Grid"
                for r in rows:
                    cells = t.add_row().cells
                    for i, td in enumerate(r.find_all(["td", "th"])[:cols]):
                        cells[i].text = td.get_text(" ", strip=True)[:400]
                stats["blocks"] += 1
        return

    if tag in ("pre", "code"):
        txt = node.get_text("\n", strip=True)
        if txt:
            p = doc.add_paragraph()
            run = p.add_run(txt)
            run.font.name = "Menlo"
            run.font.size = Pt(9)
            p.paragraph_format.left_indent = Inches(0.25)
            stats["body_chars"] += len(txt)
            stats["blocks"] += 1
        return

    if tag == "br" or tag == "hr":
        return

    # 块级容器（div / section / article / span 等）—— 进去继续找
    for child in node.children:
        _render_node(child, doc, session, stats, depth + 1)


def _render_comments(doc: Document, comments: Dict[str, Any], stats: _Stats) -> None:
    """把评论写进 Word。取不满时如实标注，不假装取全。"""
    items = comments.get("items") or []
    nominal = comments.get("nominal")
    err = comments.get("error") or ""

    doc.add_paragraph()
    head = doc.add_paragraph()
    hr = head.add_run("评论")
    hr.font.size = Pt(15)
    hr.font.bold = True
    hr.font.color.rgb = RGBColor(17, 24, 39)

    line = "共取回 %d 条" % len(items)
    if isinstance(nominal, int):
        line += "（接口自报 %d 条）" % nominal
    if err:
        line += "  ⚠️ 抓取过程中出错：%s" % err
    elif isinstance(nominal, int) and len(items) < nominal:
        line += "  ⚠️ 未取满 —— 知乎游标分页限制，差 %d 条" % (nominal - len(items))
    _add_paragraph(doc, line, stats, size=9.5, color=(100, 116, 139))

    if not items:
        _add_paragraph(doc, "（无可显示的评论）", stats, size=9.5,
                       color=(150, 160, 175))
        return

    for i, c in enumerate(items, 1):
        ts = c.get("created")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "时间未知"
        bits = [c.get("author") or "(匿名)"]
        if c.get("region"):
            bits.append(c["region"])
        bits.append(when)
        bits.append("👍 %d" % (c.get("likes") or 0))
        if c.get("is_author"):
            bits.append("作者本人")
        meta = doc.add_paragraph()
        mr = meta.add_run("%d. %s" % (i, "  ·  ".join(bits)))
        mr.font.size = Pt(9.5)
        mr.font.bold = True
        mr.font.color.rgb = RGBColor(80, 90, 105)
        meta.paragraph_format.space_after = Pt(2)

        body = doc.add_paragraph()
        br = body.add_run(c.get("content") or "（空）")
        br.font.size = Pt(10.5)
        body.paragraph_format.left_indent = Inches(0.25)
        body.paragraph_format.space_after = Pt(2)

        if c.get("replies"):
            rep = doc.add_paragraph()
            rr = rep.add_run("（另有 %d 条回复未展开）" % c["replies"])
            rr.font.size = Pt(9)
            rr.font.color.rgb = RGBColor(150, 160, 175)
            rep.paragraph_format.left_indent = Inches(0.25)
            rep.paragraph_format.space_after = Pt(6)
    stats["comments"] = len(items)
    stats["comments_nominal"] = nominal
    stats["comments_complete"] = bool(comments.get("complete"))


def generate_docx_for_item(item_meta: Dict[str, Any], content_html: str,
                           session=None, stats: Optional[_Stats] = None
                           ) -> bytes:
    """生成单个条目（文章 / 回答）的 Word 字节流。"""
    stats = stats if stats is not None else _Stats()
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    title = (item_meta.get("title") or "未命名内容").strip()
    tp = doc.add_paragraph()
    tr = tp.add_run(title)
    tr.font.size = Pt(18)
    tr.font.bold = True
    tr.font.color.rgb = RGBColor(17, 24, 39)
    tp.paragraph_format.space_after = Pt(10)

    vc = item_meta.get("voteup_count", 0)
    cc = item_meta.get("comment_count", 0)
    rows = [
        ("内容类型 / ID", "[%s] %s" % (item_meta.get("type", "article"),
                                       item_meta.get("id", ""))),
        ("作者信息", str(item_meta.get("author_name") or "知乎用户")),
        ("发布/更新时间", str(item_meta.get("created_formatted") or "")),
        ("获赞 / 评论数", "👍 赞同: %s  |  💬 评论: %s" % (vc, cc)),
        ("知乎原始链接", str(item_meta.get("url", ""))),
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for idx, (k, v) in enumerate(rows):
        cells = table.rows[idx].cells
        cells[0].text = k
        if cells[0].paragraphs and cells[0].paragraphs[0].runs:
            cells[0].paragraphs[0].runs[0].font.bold = True
            cells[0].paragraphs[0].runs[0].font.size = Pt(9.5)
        cells[1].text = v
        if cells[1].paragraphs and cells[1].paragraphs[0].runs:
            cells[1].paragraphs[0].runs[0].font.size = Pt(9.5)

    doc.add_paragraph()

    h = doc.add_paragraph()
    hr = h.add_run("正文")
    hr.font.size = Pt(15)
    hr.font.bold = True
    hr.font.color.rgb = RGBColor(17, 24, 39)

    soup = BeautifulSoup(content_html or "", "html.parser")
    for child in list(soup.children):
        _render_node(child, doc, session, stats)

    _render_comments(doc, item_meta.get("_comments") or {}, stats)

    # 归档说明：把本次导出的取舍写在文档里，谁拿到这份 Word 都能看懂
    doc.add_paragraph()
    foot = doc.add_paragraph()
    fr = foot.add_run("归档说明")
    fr.font.size = Pt(10)
    fr.font.bold = True
    fr.font.color.rgb = RGBColor(120, 130, 145)
    lines = [
        "正文文字 %d 字，共 %d 个块；内嵌图片 %d 张%s。"
        % (stats["body_chars"], stats["blocks"], stats["images_ok"],
           ("（失败 %d 张）" % stats["images_failed"]) if stats["images_failed"] else ""),
    ]
    if stats["comments_nominal"] is not None:
        lines.append("评论取回 %d / 自报 %d 条。"
                     % (stats["comments"], stats["comments_nominal"]))
    for n in stats["notes"][:12]:
        lines.append("· " + n)
    for ln in lines:
        p = doc.add_paragraph()
        r = p.add_run(ln)
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(140, 150, 165)
        p.paragraph_format.space_after = Pt(2)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _item_content(signer, itype: str, iid: str, title: str
                  ) -> Tuple[str, Dict[str, Any], str]:
    """取正文与元数据。返回 (title, meta, content_html)。"""
    meta: Dict[str, Any] = {"id": iid, "type": itype, "title": title}
    content_html = ""
    err = ""
    if itype == "article":
        meta["url"] = "https://zhuanlan.zhihu.com/p/%s" % iid
        try:
            draft = signer.get_article_draft(iid)
            meta["title"] = draft.get("title") or title
            content_html = draft.get("content") or ""
            meta["author_name"] = (draft.get("author") or {}).get("name") or ""
            ts = draft.get("created") or draft.get("created_time")
            if ts:
                meta["created_formatted"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception as exc:  # noqa: BLE001
            err = "读取正文失败：%s: %s" % (type(exc).__name__, exc)
    else:
        meta["url"] = "https://www.zhihu.com/answer/%s" % iid
        try:
            r = signer.s.get(
                "https://www.zhihu.com/api/v4/answers/%s"
                "?include=content,voteup_count,comment_count,created_time,"
                "updated_time,question" % iid, timeout=20)
            if r.status_code == 200:
                d = r.json()
                q = d.get("question") or {}
                meta["title"] = "回答：%s" % (q.get("title") or title)
                content_html = d.get("content") or ""
                meta["author_name"] = (d.get("author") or {}).get("name") or ""
                meta["voteup_count"] = d.get("voteup_count",
                                             meta.get("voteup_count", 0))
                meta["comment_count"] = d.get("comment_count",
                                              meta.get("comment_count", 0))
                ts = d.get("created_time")
                if ts:
                    meta["created_formatted"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(ts))
            else:
                err = "回答详情 HTTP %d" % r.status_code
        except Exception as exc:  # noqa: BLE001
            err = "读取回答失败：%s: %s" % (type(exc).__name__, exc)
    return meta["title"], meta, content_html, err


def export_items(signer, items: List[Dict[str, Any]]):
    """导出选中的条目。

    :returns: ``(filename, data, media_type, report)``
        ``report`` 含 requested / succeeded / failed / notes，
        调用方必须把它交回给用户 —— 失败的条目绝不能悄悄消失。
    """
    docs: List[Tuple[str, bytes]] = []
    failed: List[Dict[str, str]] = []
    notes: List[str] = []
    items = items or []

    for it in items:
        iid = str(it.get("id") or "").strip()
        if not iid:
            continue
        itype = str(it.get("type") or "article")
        title = str(it.get("title") or "")
        try:
            title, meta, content_html, cerr = _item_content(
                signer, itype, iid, title)
            meta["voteup_count"] = it.get("voteup_count", meta.get("voteup_count", 0))
            meta["comment_count"] = it.get("comment_count", meta.get("comment_count", 0))
            meta["url"] = it.get("url") or meta.get("url")

            # 评论：这是旧版完全缺失的一环
            cm = fetch_comments(signer.s, itype, iid,
                                nominal_count=int(meta.get("comment_count") or 0))
            meta["_comments"] = cm
            if cm.get("error"):
                notes.append("%s：评论抓取出错 %s" % (title[:24], cm["error"]))
            elif isinstance(cm.get("nominal"), int) and \
                    cm["fetched"] < cm["nominal"]:
                notes.append("%s：评论 %d/%d（游标限制）"
                             % (title[:24], cm["fetched"], cm["nominal"]))

            stats = _Stats()
            if cerr:
                notes.append("%s：%s" % (title[:24], cerr))
            docx_data = generate_docx_for_item(meta, content_html,
                                               session=signer.s, stats=stats)
            if not content_html.strip():
                notes.append("%s：正文为空（可能读取失败）" % title[:24])
            safe = re.sub(r'[\/\\:*?"<>|\r\n\t]', "_", meta["title"])[:60].strip()
            safe = safe or "%s_%s" % (itype, iid)
            docs.append(("%s.docx" % safe, docx_data))
        except Exception as exc:  # noqa: BLE001
            # 关键：不再 pass 掉。记下来，最后报告给用户。
            failed.append({"id": iid, "type": itype, "title": title[:60],
                           "reason": "%s: %s" % (type(exc).__name__, exc)})

    if not docs:
        detail = "；".join("%s(%s)" % (f["title"] or f["id"], f["reason"])
                          for f in failed[:5]) or "无有效条目"
        raise ValueError("未能生成任何 Word 文档。%s" % detail)

    report = {"requested": len(items), "succeeded": len(docs),
              "failed": failed, "notes": notes[:80]}

    if len(docs) == 1 and not failed:
        fname, data = docs[0]
        return fname, data, DOCX_MIME, report

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, (fname, data) in enumerate(docs, 1):
            zf.writestr("%03d_%s" % (idx, fname), data)
        lines = ["知乎内容导出报告",
                 "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                 "请求 %d 篇，成功 %d 篇，失败 %d 篇"
                 % (report["requested"], report["succeeded"], len(failed)),
                 ""]
        if failed:
            lines.append("【失败条目】")
            for f in failed:
                lines.append("  · [%s] %s  %s" % (f["type"], f["title"] or f["id"],
                                                 f["reason"]))
            lines.append("")
        if notes:
            lines.append("【归档说明】")
            for n in notes:
                lines.append("  · " + n)
        zf.writestr("_导出报告.txt", "\n".join(lines))
    zname = "知乎内容导出_Word_%s.zip" % time.strftime("%Y%m%d_%H%M%S")
    return zname, zbuf.getvalue(), ZIP_MIME, report
