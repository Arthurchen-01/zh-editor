#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qydocx —— 用标准库写 .docx（零第三方依赖）。

为什么不用 python-docx
----------------------
`python-docx` 会拖进 `lxml`（C 扩展，Windows 上要编译轮子）。用户的要求是
「不需要更多依赖或者系统要求」—— 只要装了 Python 就能跑。而 .docx 本质是
一个 ZIP 装了几个 XML，标准库的 `zipfile` + 字符串模板足够写出 Word
能正常打开的文档。

本模块做到的事
--------------
* 知乎正文 HTML → Word 段落（p / h1-h6 / blockquote / li / pre / code）
* **内嵌图片**：真的把图片字节放进 `word/media/`，不是只留个链接
* 图片尺寸：从图片头（JPEG SOF / PNG IHDR / GIF）直接读出像素尺寸，
  按 96 DPI 换算成 EMU，超宽自动等比缩到版心内 —— 不依赖 PIL
* 超链接、加粗、斜体、下划线
* 多篇合并成一本「备份 Word」（带目录页）

为什么图片尺寸要自己解
----------------------
不知道尺寸就只能给一个固定宽度，竖图会被拉成横的、长图会被压扁。
解 JPEG/PNG/GIF 的头只要几十行，换来的是「导出的 Word 看起来是对的」。
"""
from __future__ import annotations

import base64
import html as html_mod
import io
import json
import os
import re
import struct
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

EMU_PER_INCH = 914400
EMU_PER_PX_96 = 9525          # 96 DPI
TWIP_PER_INCH = 1440

PAGE_SIZES = {
    "A4": (11906, 16838),      # twips
    "Letter": (12240, 15840),
}

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_SPLIT_RE = re.compile(
    r"(<(?:p|h[1-6]|blockquote|li|pre|figure|div|table)\b[^>]*>[\s\S]*?"
    r"</(?:p|h[1-6]|blockquote|li|pre|figure|div|table)>|<img\b[^>]*/?>)",
    re.I)
_INLINE_RE = re.compile(
    r"(<a\b[^>]*>[\s\S]*?</a>|<(?:b|strong)\b[^>]*>[\s\S]*?</(?:b|strong)>"
    r"|<(?:i|em)\b[^>]*>[\s\S]*?</(?:i|em)>"
    r"|<(?:u|ins)\b[^>]*>[\s\S]*?</(?:u|ins)>"
    r"|<(?:code|span)\b[^>]*>[\s\S]*?</(?:code|span)>"
    r"|<br\s*/?>)", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-\w:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_BLOCK_TYPE_RE = re.compile(r"^<(p|h[1-6]|blockquote|li|pre|figure|div|table)\b", re.I)

# data:image/png;base64,xxxx  —— 内联图片。知乎正文里没有，但用户从别处
# 粘贴过来的内容会有。不处理的话这类图会被当成「下载失败」静默丢掉。
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[^;,]*)(?P<params>(?:;[^,]*)*),(?P<data>.*)$", re.S | re.I)


def _decode_data_uri(url: str) -> Optional[bytes]:
    """把 data: URI 解成字节。不是 data: URI 就返回 None。"""
    m = _DATA_URI_RE.match((url or "").strip())
    if not m:
        return None
    payload = m.group("data") or ""
    params = (m.group("params") or "").lower()
    try:
        if "base64" in params:
            return base64.b64decode(payload, validate=False)
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload)
    except Exception:  # noqa: BLE001
        return None


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                   .replace(">", "&gt;").replace('"', "&quot;")


def _attrs(tag: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5))
        out[key] = html_mod.unescape(val or "")
    return out


# --------------------------------------------------------------------------- #
# 图片头解析（无 PIL）
# --------------------------------------------------------------------------- #

def image_size(data: bytes) -> Tuple[int, int]:
    """从图片字节里读出 (宽, 高) 像素。认不出就返回 (0, 0)。"""
    if not data or len(data) < 16:
        return (0, 0)
    # PNG: 8 字节签名 + IHDR
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", data[16:24])
            return (int(w), int(h))
        except Exception:  # noqa: BLE001
            return (0, 0)
    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a"):
        try:
            w, h = struct.unpack("<HH", data[6:10])
            return (int(w), int(h))
        except Exception:  # noqa: BLE001
            return (0, 0)
    # BMP
    if data[:2] == b"BM":
        try:
            w, h = struct.unpack("<ii", data[18:26])
            return (abs(int(w)), abs(int(h)))
        except Exception:  # noqa: BLE001
            return (0, 0)
    # JPEG: 扫 SOFn 标记
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xD9:
                break
            try:
                seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            except Exception:  # noqa: BLE001
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                try:
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (int(w), int(h))
                except Exception:  # noqa: BLE001
                    return (0, 0)
            i += 2 + seglen
        return (0, 0)
    # WEBP (VP8X / VP8 / VP8L)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        try:
            fmt = data[12:16]
            if fmt == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return (w, h)
            if fmt == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return (int(w) & 0x3FFF, int(h) & 0x3FFF)
            if fmt == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        except Exception:  # noqa: BLE001
            return (0, 0)
    return (0, 0)


def sniff_ext(data: bytes, fallback: str = "jpg") -> str:
    """按魔数判扩展名 —— 不能信 URL 的后缀，知乎 CDN 会不按后缀给内容。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    return fallback


# --------------------------------------------------------------------------- #
# 图片下载（带磁盘缓存）
# --------------------------------------------------------------------------- #

class ImageFetcher:
    """下载图片并缓存到本地。默认用 qynet（零依赖）。

    知乎图床有防盗链，必须带 Referer，否则拿到 403 的空图。
    """

    def __init__(self, cache_dir: Optional[Path] = None,
                 referer: str = "https://zhuanlan.zhihu.com/") -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else \
            Path(__file__).resolve().parent.parent / "data" / "img_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.referer = referer
        self.stats = {"hit": 0, "miss": 0, "fail": 0, "bytes": 0}

    def _cache_path(self, url: str) -> Path:
        import hashlib
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / h

    def fetch(self, url: str, timeout: float = 25.0) -> Optional[bytes]:
        if not url:
            return None
        p = self._cache_path(url)
        if p.exists():
            try:
                data = p.read_bytes()
                if data:
                    self.stats["hit"] += 1
                    return data
            except Exception:  # noqa: BLE001
                pass
        try:
            import qynet  # type: ignore
            r = qynet.get(url, headers={"Referer": self.referer},
                          timeout=timeout)
            if r.status_code != 200 or not r.content:
                self.stats["fail"] += 1
                return None
            data = r.content
        except Exception:  # noqa: BLE001
            self.stats["fail"] += 1
            return None
        # webp 在 Word 里打不开 —— 换 .jpg 再要一次（知乎图床支持）
        if sniff_ext(data) == "webp" and not url.lower().endswith(".jpg"):
            alt = re.sub(r"\.webp$", ".jpg", url, flags=re.I)
            if alt != url:
                try:
                    import qynet  # type: ignore
                    r2 = qynet.get(alt, headers={"Referer": self.referer},
                                   timeout=timeout)
                    if r2.status_code == 200 and r2.content \
                            and sniff_ext(r2.content) != "webp":
                        data = r2.content
                except Exception:  # noqa: BLE001
                    pass
        try:
            p.write_bytes(data)
        except Exception:  # noqa: BLE001
            pass
        self.stats["miss"] += 1
        self.stats["bytes"] += len(data)
        return data


# --------------------------------------------------------------------------- #
# 样式表
# --------------------------------------------------------------------------- #

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="PingFang SC"/>
<w:sz w:val="22"/><w:szCs w:val="22"/>
</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>
<w:spacing w:before="60" w:after="60" w:line="300" w:lineRule="auto"/>
</w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:before="240" w:after="180"/></w:pPr>
<w:rPr><w:rFonts w:eastAsia="PingFang SC"/><w:b/><w:sz w:val="40"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:before="300" w:after="120"/>
<w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:before="240" w:after="100"/>
<w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:before="200" w:after="80"/>
<w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="25"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:ind w:left="480" w:right="240"/>
<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="D0D5DD"/></w:pBdr>
<w:spacing w:before="120" w:after="120"/></w:pPr>
<w:rPr><w:i/><w:color w:val="475467"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph">
<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:ind w:left="480"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:ind w:left="240"/>
<w:shd w:val="clear" w:color="auto" w:fill="F5F6F8"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Menlo" w:hAnsi="Menlo" w:eastAsia="Menlo"/>
<w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="Hyperlink">
<w:name w:val="Hyperlink"/>
<w:rPr><w:color w:val="1D4ED8"/><w:u w:val="single"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="Caption"/>
<w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:jc w:val="center"/><w:spacing w:before="40" w:after="160"/></w:pPr>
<w:rPr><w:sz w:val="18"/><w:color w:val="667085"/></w:rPr></w:style>
</w:styles>
"""

_CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>{title}</dc:title>
<dc:creator>{author}</dc:creator>
<cp:lastModifiedBy>{author}</cp:lastModifiedBy>
<dc:description>{desc}</dc:description>
<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
<dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>
"""

_APP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<Application>QingyiEdu Local</Application>
<AppVersion>1.0</AppVersion>
<Company></Company>
</Properties>
"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="jpg" ContentType="image/jpeg"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Default Extension="png" ContentType="image/png"/>
<Default Extension="gif" ContentType="image/gif"/>
<Default Extension="bmp" ContentType="image/bmp"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

_DOC_OPEN = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">
<w:body>
"""

_DOC_CLOSE = """<w:sectPr>
<w:pgSz w:w="{pw}" w:h="{ph}"/>
<w:pgMar w:top="{mt}" w:right="{mr}" w:bottom="{mb}" w:left="{ml}"
 w:header="720" w:footer="720" w:gutter="0"/>
</w:sectPr>
</w:body></w:document>
"""


# --------------------------------------------------------------------------- #
# 构建器
# --------------------------------------------------------------------------- #

class DocxBuilder:
    """增量拼一个 .docx。`save()` 之前不落盘。"""

    def __init__(self, page: str = "A4", margin_in: float = 1.0,
                 max_img_in: float = 6.0) -> None:
        self.body: List[str] = []
        self._rels: List[Tuple[str, str, str, Optional[str]]] = []
        self._media: List[Tuple[str, bytes]] = []
        self._rid = 10           # 1-9 留给根关系
        self._pid = 1
        self._media_seq = 0
        self.page = page if page in PAGE_SIZES else "A4"
        self.margin = int(margin_in * TWIP_PER_INCH)
        self.max_img_emu = int(max_img_in * EMU_PER_INCH)
        self.img_embedded = 0
        self.img_failed: List[str] = []

    # ---- 关系 / 媒体 ---- #

    def _new_rid(self) -> str:
        self._rid += 1
        return f"rId{self._rid}"

    def _add_rel(self, rtype: str, target: str,
                 mode: Optional[str] = None) -> str:
        rid = self._new_rid()
        self._rels.append((rid, rtype, target, mode))
        return rid

    # ---- 段落 ---- #

    def para(self, runs: List[Dict[str, Any]], style: Optional[str] = None,
             align: Optional[str] = None,
             spacing: Optional[Tuple[int, int]] = None) -> None:
        ppr: List[str] = []
        if style:
            ppr.append(f'<w:pStyle w:val="{style}"/>')
        if align:
            ppr.append(f'<w:jc w:val="{align}"/>')
        if spacing:
            ppr.append(f'<w:spacing w:before="{spacing[0]}" '
                       f'w:after="{spacing[1]}"/>')
        xml = "<w:p>"
        if ppr:
            xml += "<w:pPr>" + "".join(ppr) + "</w:pPr>"
        xml += "".join(self._run(r) for r in runs) or \
            '<w:r><w:t xml:space="preserve"></w:t></w:r>'
        xml += "</w:p>"
        self.body.append(xml)

    def _run(self, r: Dict[str, Any]) -> str:
        text = r.get("text") or ""
        if r.get("break"):
            return "<w:r><w:br/></w:r>"
        if r.get("image"):
            return r["image"]
        if r.get("link"):
            rid = r["rid"]
            inner = self._rpr_wrap(text, r)
            return (f'<w:hyperlink r:id="{rid}" w:history="1">'
                    f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
                    f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'
                    f'</w:hyperlink>')
        return self._rpr_wrap(text, r)

    @staticmethod
    def _rpr_wrap(text: str, r: Dict[str, Any]) -> str:
        rpr = ""
        if r.get("b"):
            rpr += "<w:b/>"
        if r.get("i"):
            rpr += "<w:i/>"
        if r.get("u"):
            rpr += '<w:u w:val="single"/>'
        if r.get("code"):
            rpr += ('<w:rFonts w:ascii="Menlo" w:hAnsi="Menlo" '
                    'w:eastAsia="Menlo"/><w:shd w:val="clear" w:color="auto" '
                    'w:fill="F5F6F8"/>')
        if r.get("color"):
            rpr += f'<w:color w:val="{r["color"]}"/>'
        if r.get("sz"):
            rpr += f'<w:sz w:val="{r["sz"]}"/>'
        return ("<w:r>" + (f"<w:rPr>{rpr}</w:rPr>" if rpr else "")
                + f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>')

    def heading(self, text: str, level: int = 1) -> None:
        self.para([{"text": text}], style=f"Heading{max(1, min(3, level))}")

    def title(self, text: str) -> None:
        self.para([{"text": text}], style="Title", align="center")

    def quote(self, runs: List[Dict[str, Any]]) -> None:
        self.para(runs, style="Quote")

    def caption(self, text: str) -> None:
        self.para([{"text": text}], style="Caption")

    def page_break(self) -> None:
        self.body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    def spacer(self, pts: int = 12) -> None:
        self.body.append(f'<w:p><w:pPr><w:spacing w:before="{pts*20}" '
                         f'w:after="0"/></w:pPr></w:p>')

    # ---- 图片 ---- #

    def image(self, data: bytes, px_w: int = 0, px_h: int = 0,
              alt: str = "", url: str = "") -> bool:
        """内嵌一张图。返回是否成功。

        失败（例如格式 Word 不支持）时**不静默丢弃** —— 会把 URL 作为
        文字写进文档，并在 `img_failed` 里登记。用户要的「图片都在」
        包含「丢了什么必须看得见」这一层含义。
        """
        if not data:
            return self._img_fallback(url, alt, "下载失败")
        ext = sniff_ext(data)
        if ext == "webp":
            return self._img_fallback(url, alt, "格式 WebP，Word 不支持")
        if ext not in ("jpg", "png", "gif", "bmp"):
            return self._img_fallback(url, alt, f"未知格式 {ext}")

        w, h = (px_w, px_h)
        if not (w and h):
            w, h = image_size(data)
        if not (w and h):
            w, h = 800, 600          # 认不出尺寸时给一个中性比例

        cx = w * EMU_PER_PX_96
        cy = h * EMU_PER_PX_96
        if cx > self.max_img_emu:
            ratio = self.max_img_emu / cx
            cx = self.max_img_emu
            cy = int(cy * ratio)

        self._media_seq += 1
        name = f"image{self._media_seq}.{ext}"
        self._media.append((name, data))
        rid = self._add_rel(
            "http://schemas.openxmlformats.org/officeDocument/2006/"
            "relationships/image", f"media/{name}")

        self._pid += 1
        pid = self._pid
        drawing = (
            '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'<wp:docPr id="{pid}" name="Picture {pid}" '
            f'descr="{_esc(alt or name)}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{pid}" name="{_esc(name)}"/>'
            '<pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/>'
            '<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '</pic:spPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r>')
        self.para([{"image": drawing}], align="center")
        self.img_embedded += 1
        return True

    def _img_fallback(self, url: str, alt: str, reason: str) -> bool:
        label = alt or "图片"
        self.para([{"text": f"[{label} — 未嵌入：{reason}]", "i": True,
                    "color": "B45309", "sz": "19"}], align="center")
        if url:
            self.para([{"text": url, "color": "667085", "sz": "17"}],
                      align="center")
        self.img_failed.append(url or label)
        return False

    # ---- 保存 ---- #

    def _document_rels(self) -> str:
        items = [
            ('<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/styles" Target="styles.xml"/>')]
        for rid, rtype, target, mode in self._rels:
            m = f' TargetMode="{mode}"' if mode else ""
            items.append(f'<Relationship Id="{rid}" Type="{rtype}" '
                         f'Target="{_esc(target)}"{m}/>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">' + "".join(items)
                + "</Relationships>")

    def save(self, path: Path | str, title: str = "", author: str = "清一新教育",
             desc: str = "") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pw, ph = PAGE_SIZES[self.page]
        doc = (_DOC_OPEN + "".join(self.body)
               + _DOC_CLOSE.format(pw=pw, ph=ph, mt=self.margin, mb=self.margin,
                                   ml=self.margin, mr=self.margin))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        core = _CORE_XML.format(title=_esc(title), author=_esc(author),
                                desc=_esc(desc), created=now)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _CONTENT_TYPES)
            z.writestr("_rels/.rels", _ROOT_RELS)
            z.writestr("word/document.xml", doc)
            z.writestr("word/styles.xml", _STYLES_XML)
            z.writestr("word/_rels/document.xml.rels", self._document_rels())
            z.writestr("docProps/core.xml", core)
            z.writestr("docProps/app.xml", _APP_XML)
            for name, data in self._media:
                z.writestr(f"word/media/{name}", data)
        os.replace(tmp, path)
        return path


# --------------------------------------------------------------------------- #
# 知乎 HTML → docx
# --------------------------------------------------------------------------- #

_STYLE_MAP = {"h1": "Heading1", "h2": "Heading2", "h3": "Heading3",
              "h4": "Heading3", "h5": "Heading3", "h6": "Heading3",
              "blockquote": "Quote", "li": "ListParagraph", "pre": "Code"}


def _inline_runs(html: str, b: DocxBuilder,
                 on_link: Optional[Callable[[str], None]] = None
                 ) -> List[Dict[str, Any]]:
    """把一段块内 HTML 拆成 runs。支持 b/strong、i/em、u/ins、code/span、a、br。"""
    runs: List[Dict[str, Any]] = []
    pos = 0
    for m in _INLINE_RE.finditer(html):
        if m.start() > pos:
            runs.extend(_plain_runs(html[pos:m.start()]))
        raw = m.group(0)
        low = raw.lower()
        if low.startswith("<br"):
            runs.append({"break": True})
        elif low.startswith("<a"):
            a = _attrs(raw[:raw.find(">") + 1])
            href = a.get("href") or ""
            inner = re.sub(r"^<a\b[^>]*>|</a>$", "", raw, flags=re.I)
            text = html_mod.unescape(_TAG_RE.sub("", inner)).strip()
            if not text:
                text = href
            if href:
                rid = b._add_rel(  # noqa: SLF001
                    "http://schemas.openxmlformats.org/officeDocument/2006/"
                    "relationships/hyperlink", href, mode="External")
                runs.append({"text": text, "link": True, "rid": rid})
                if on_link:
                    on_link(href)
            else:
                runs.append({"text": text})
        else:
            tag = re.match(r"<([a-z0-9]+)", low)
            name = tag.group(1) if tag else ""
            inner = re.sub(r"^<[^>]+>|</[^>]+>$", "", raw)
            flag = {"b": "b", "strong": "b", "i": "i", "em": "i",
                    "u": "u", "ins": "u", "code": "code", "span": None}.get(name)
            for r in _plain_runs(inner):
                if flag:
                    r[flag] = True
                runs.append(r)
        pos = m.end()
    if pos < len(html):
        runs.extend(_plain_runs(html[pos:]))
    return [r for r in runs if r.get("break") or r.get("text")
            or r.get("image")]


def _plain_runs(html: str) -> List[Dict[str, Any]]:
    t = html_mod.unescape(_TAG_RE.sub("", html or ""))
    if not t:
        return []
    return [{"text": t}]


def html_to_builder(html: str, b: DocxBuilder,
                    fetcher: Optional[ImageFetcher] = None,
                    max_images: int = 0,
                    on_image: Optional[Callable[[Dict[str, Any]], None]] = None
                    ) -> Dict[str, Any]:
    """把知乎正文 HTML 写进 builder。返回统计。"""
    src = html or ""
    stats = {"blocks": 0, "images_seen": 0, "images_embedded": 0,
             "images_failed": 0, "links": 0}
    parts = _BLOCK_SPLIT_RE.split(src)
    for chunk in parts:
        if not chunk or not chunk.strip():
            continue
        c = chunk.strip()
        m = _BLOCK_TYPE_RE.match(c)
        tag = m.group(1).lower() if m else ""
        style = _STYLE_MAP.get(tag) if tag else None

        # 含图片的块：把图片抠出来单独内嵌，剩下的文字照常成段。
        # 必须放在通用分支之前 —— 否则 <p><img></p> 这种结构会被
        # _inline_runs 整个吃掉，图片静默消失。
        if "<img" in c.lower():
            buf: List[str] = []

            def _flush(buf: List[str] = buf) -> None:  # type: ignore[misc]
                if not buf:
                    return
                runs = _inline_runs("".join(buf), b)
                buf.clear()
                if runs:
                    b.para(runs, style=style)
                    stats["blocks"] += 1

            for pc in re.split(r"(<img\b[^>]*/?>)", c, flags=re.I):
                if not pc or not pc.strip():
                    continue
                if pc.lstrip().lower().startswith("<img"):
                    _flush()
                    _emit_img(pc, b, fetcher, stats, max_images, on_image)
                else:
                    buf.append(pc)
            _flush()
            continue

        if not m:
            # 裸文本（没有块标签的段落）
            runs = _inline_runs(c, b)
            if runs:
                b.para(runs)
                stats["blocks"] += 1
            continue
        runs = _inline_runs(c, b)
        if not runs:
            continue
        if tag == "li":
            runs = [{"text": "• "}] + runs
        b.para(runs, style=style)
        stats["blocks"] += 1
        stats["links"] += sum(1 for r in runs if r.get("link"))
    return stats


def _emit_img(tag: str, b: DocxBuilder, fetcher: Optional[ImageFetcher],
              stats: Dict[str, Any], max_images: int,
              on_image: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    a = _attrs(tag)
    stats["images_seen"] += 1
    if max_images and stats["images_seen"] > max_images:
        return
    url = a.get("src") or a.get("data-original") or a.get("data-actualsrc") or ""
    alt = a.get("data-caption") or a.get("alt") or ""
    w = int(a.get("data-rawwidth") or a.get("width") or 0 or 0)
    h = int(a.get("data-rawheight") or a.get("height") or 0 or 0)
    # 内联图片直接解码，不走网络
    data = _decode_data_uri(url)
    if data is None:
        data = fetcher.fetch(url) if (fetcher and url) else None
    if on_image:
        on_image({"url": url if not url.startswith("data:") else "data:<内联>",
                  "alt": alt, "bytes": len(data or b""), "ok": bool(data)})
    ok = b.image(data or b"", w, h, alt=alt, url=url)
    if ok:
        stats["images_embedded"] += 1
    else:
        stats["images_failed"] += 1
    if alt:
        b.caption(alt)


# --------------------------------------------------------------------------- #
# 高层接口
# --------------------------------------------------------------------------- #

def write_article_docx(path: Path | str, title: str, body_html: str,
                       meta: Optional[Dict[str, Any]] = None,
                       fetcher: Optional[ImageFetcher] = None,
                       max_images: int = 0) -> Dict[str, Any]:
    """导出**单篇** Word（含图片）。"""
    b = DocxBuilder()
    b.title(title or "(无标题)")
    meta = meta or {}
    line = []
    if meta.get("author"):
        line.append(f"作者：{meta['author']}")
    if meta.get("url"):
        line.append(f"原文：{meta['url']}")
    if meta.get("exported_at"):
        line.append(f"导出时间：{meta['exported_at']}")
    if line:
        b.para([{"text": "  ·  ".join(line), "sz": "18", "color": "667085"}],
               align="center")
    if meta.get("note"):
        b.para([{"text": str(meta["note"]), "sz": "18", "color": "667085"}],
               align="center")
    b.spacer(8)
    st = html_to_builder(body_html, b, fetcher, max_images)
    p = b.save(path, title=title, desc=f"导出 {meta.get('exported_at','')}")
    return {"path": str(p), "bytes": Path(p).stat().st_size,
            "images_embedded": b.img_embedded,
            "images_failed": len(b.img_failed),
            "failed_urls": b.img_failed[:50], **st}


def write_bundle_docx(path: Path | str, items: Sequence[Dict[str, Any]],
                      fetcher: Optional[ImageFetcher] = None,
                      max_images_per_doc: int = 0,
                      title: str = "清一新教育 · 文章备份",
                      toc: bool = True) -> Dict[str, Any]:
    """导出**整本** Word 备份：一篇一章，可选目录页。

    items: [{"id","title","html","url","author","note"}, ...]
    """
    b = DocxBuilder()
    b.title(title)
    b.para([{"text": time.strftime("%Y-%m-%d %H:%M"), "sz": "18",
             "color": "667085"}], align="center")
    b.para([{"text": f"共 {len(items)} 篇", "sz": "18", "color": "667085"}],
           align="center")
    b.spacer(10)

    if toc:
        b.heading("目录", 1)
        for i, it in enumerate(items, 1):
            t = (it.get("title") or "(无标题)").strip()
            b.para([{"text": f"{i}. {t}", "sz": "20"}], style="ListParagraph")
        b.page_break()

    total = {"images_embedded": 0, "images_failed": 0, "blocks": 0}
    failed: List[str] = []
    per: List[Dict[str, Any]] = []
    for i, it in enumerate(items, 1):
        if i > 1:
            b.page_break()
        t = (it.get("title") or "(无标题)").strip()
        b.heading(f"{i}. {t}", 1)
        sub = []
        if it.get("url"):
            sub.append(str(it["url"]))
        if it.get("author"):
            sub.append(f"作者：{it['author']}")
        if it.get("note"):
            sub.append(str(it["note"]))
        if sub:
            b.para([{"text": "  ·  ".join(sub), "sz": "18",
                     "color": "667085"}])
        b.spacer(4)
        st = html_to_builder(it.get("html") or "", b, fetcher,
                             max_images_per_doc)
        total["images_embedded"] += st["images_embedded"]
        total["images_failed"] += st["images_failed"]
        total["blocks"] += st["blocks"]
        failed.extend(b.img_failed)
        b.img_failed.clear()
        per.append({"id": it.get("id"), "title": t, **st})

    p = b.save(path, title=title, desc=f"{len(items)} 篇备份")
    return {"path": str(p), "bytes": Path(p).stat().st_size,
            "documents": len(items), "images_embedded": total["images_embedded"],
            "images_failed": total["images_failed"], "blocks": total["blocks"],
            "failed_urls": failed[:100], "per_document": per}


def write_markdown(path: Path | str, items: Sequence[Dict[str, Any]],
                   title: str = "清一新教育 · 文章备份") -> Dict[str, Any]:
    """纯文本备份 —— 没有任何格式风险，作为兜底永远可用。"""
    out = [f"# {title}", "", f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}",
           f"> 共 {len(items)} 篇", "", "---", ""]
    for i, it in enumerate(items, 1):
        out.append(f"## {i}. {it.get('title') or '(无标题)'}")
        if it.get("url"):
            out.append(f"<{it['url']}>")
        out.append("")
        text = re.sub(r"<br\s*/?>", "\n", it.get("html") or "", flags=re.I)
        text = re.sub(r"</(p|h[1-6]|li|blockquote|div)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<img\b[^>]*>",
                      lambda m: "\n![" + (_attrs(m.group(0)).get("data-caption")
                                          or "图片") + "]("
                                + (_attrs(m.group(0)).get("src") or "") + ")\n",
                      text, flags=re.I)
        text = html_mod.unescape(_TAG_RE.sub("", text))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        out.append(text)
        out.append("")
        out.append("---")
        out.append("")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out), encoding="utf-8")
    return {"path": str(p), "bytes": p.stat().st_size, "documents": len(items)}


# --------------------------------------------------------------------------- #
# 自检（不需要网络：自己造一张 PNG）
# --------------------------------------------------------------------------- #

def _tiny_png(w: int = 320, h: int = 180,
              rgb: Tuple[int, int, int] = (185, 28, 28)) -> bytes:
    """用标准库造一张合法 PNG —— 让「图片内嵌」这条能被离线验证。"""
    raw = b"".join(
        b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp())

    png = _tiny_png(320, 180)
    print("造 PNG      :", len(png), "字节；解出尺寸", image_size(png))

    jpg = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000"
        "ffc00011080168028003012200021101031101")  # SOF0 里 宽0x0280=640 高0x0168=360
    print("解 JPEG 头  :", image_size(jpg), "（期望 (640, 360)）")

    body = (
        "<h2>第一段小标题</h2>"
        "<p>这是<strong>加粗</strong>、<em>斜体</em>和<code>行内代码</code>。</p>"
        "<p>一个链接：<a href=\"https://www.zhihu.com/people/rancho\">我的主页</a></p>"
        f'<img src="x.png" data-rawwidth="320" data-rawheight="180" '
        f'data-caption="示例图">'
        "<blockquote>这是引用。</blockquote>"
        "<ul><li>条目一</li><li>条目二</li></ul>"
        f'<img src="https://pic1.zhimg.com/v2-deadbeef_broken.jpg" '
        f'data-caption="坏图">'
    )

    class _Fake(ImageFetcher):
        def __init__(self) -> None:
            super().__init__(cache_dir=tmp / "cache")
        def fetch(self, url: str, timeout: float = 25.0):  # type: ignore[override]
            return png if url == "x.png" else None

    fetcher = _Fake()

    single = tmp / "single.docx"
    r1 = write_article_docx(single, "我的成长经历", body,
                            meta={"url": "https://zhuanlan.zhihu.com/p/1",
                                  "author": "胡启岩",
                                  "exported_at": "2026-09-25 22:00"},
                            fetcher=fetcher)
    print("单篇导出    :", json.dumps({k: v for k, v in r1.items()
                                       if k != "failed_urls"},
                                      ensure_ascii=False))

    bundle = tmp / "bundle.docx"
    items = [{"id": str(i), "title": f"第 {i} 篇", "html": body,
              "url": f"https://zhuanlan.zhihu.com/p/{i}"} for i in range(1, 4)]
    r2 = write_bundle_docx(bundle, items, fetcher=fetcher)
    print("整本导出    :", json.dumps({k: v for k, v in r2.items()
                                       if k not in ("failed_urls",
                                                    "per_document")},
                                      ensure_ascii=False))

    md = tmp / "backup.md"
    r3 = write_markdown(md, items)
    print("Markdown    :", json.dumps(r3, ensure_ascii=False))

    # 验证 zip 结构合法 + 图片真的在里面
    with zipfile.ZipFile(single) as z:
        names = z.namelist()
        bad = z.testzip()
        doc = z.read("word/document.xml").decode("utf-8")
        media = [n for n in names if n.startswith("word/media/")]
        print("单篇 zip    :", "合法" if bad is None else f"损坏@{bad}",
              f"| {len(names)} 个部件 | media {len(media)} 张")
        print("  含 drawing:", "<w:drawing>" in doc,
              "| 含 hyperlink:", "<w:hyperlink" in doc,
              "| 含表格外嵌提示:", "未嵌入" in doc)
        print("  内嵌图尺寸:", re.search(r'<a:ext cx="(\d+)" cy="(\d+)"/>', doc)
              .groups() if re.search(r'<a:ext cx="(\d+)" cy="(\d+)"/>', doc)
              else "未找到")
    with zipfile.ZipFile(bundle) as z:
        d = z.read("word/document.xml").decode("utf-8")
        print("整本 zip    : 合法" if z.testzip() is None else "损坏",
              f"| media {len([n for n in z.namelist() if n.startswith('word/media/')])} 张",
              f"| 分页 {d.count('w:type=\"page\"')} 处")
    print()
    print("产物目录    :", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
