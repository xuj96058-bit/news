"""Export the saved, editable report body without re-running research or a model.

Markdown and HTML need only the standard library. PDF imports ReportLab lazily,
so a missing optional PDF dependency never prevents the application from loading.
Only a small, escaped Markdown subset is rendered; raw HTML stays plain text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit


class ResearchExportError(Exception):
    """A safe, user-facing export failure; never includes report configuration."""


def _fields(report: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    if not isinstance(report, Mapping):
        raise ResearchExportError("無法匯出：報告資料格式不正確。")
    body = report.get("body", "")
    if not isinstance(body, str):
        raise ResearchExportError("無法匯出：報告正文必須為文字。")
    title = str(report.get("title") or "研究報告").replace("\r", " ").replace("\n", " ")
    # Deliberately whitelist display metadata. Provider settings and credentials
    # are never serialized, even when an application passes them in this mapping.
    metadata = []
    for key, label in (("version", "版本"), ("as_of", "資料截至"), ("topic", "主題")):
        value = report.get(key)
        if value is not None and value != "":
            metadata.append(f"{label}：{str(value).replace(chr(13), ' ').replace(chr(10), ' ')}")
    return title, body, metadata


def export_markdown(report: dict) -> bytes:
    """Return UTF-8 Markdown, preserving the current body and its source appendix."""
    title, body, metadata = _fields(report)
    preamble = f"# {title}\n\n"
    if metadata:
        preamble += " · ".join(metadata) + "\n\n"
    return (preamble + body).encode("utf-8", errors="replace")


@dataclass(frozen=True)
class _Block:
    kind: str
    value: Any
    level: int = 0


_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s{0,3}([-+*]|\d+[.)])\s+(.+)$")
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_INLINE = re.compile(
    r"(?P<escaped>\\[\\`*{}\[\]()#+.!_|>-])"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<strong>\*\*[^\n]+?\*\*|__[^\n]+?__)"
    r"|(?P<image>!\[(?P<alt>[^\]\n]*)\]\((?P<image_url>[^)\n]*)\))"
    r"|(?P<link>\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\n]*)\))"
    r"|(?P<auto><https?://[^<>\s]+>|https?://[^<>\s()]+)"
)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    header, separator = _cells(lines[index]), _cells(lines[index + 1])
    return len(header) == len(separator) and all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    )


def _block_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(_HEADING.match(line) or _LIST.match(line) or _RULE.match(line)
                or line.lstrip().startswith(("```", "~~~", ">"))
                or _table_start(lines, index))


def _blocks(body: str) -> list[_Block]:
    """Tolerate incomplete Markdown as ordinary readable text."""
    lines = body.splitlines()
    blocks: list[_Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            blocks.append(_Block("heading", heading.group(2), len(heading.group(1))))
            index += 1
        elif line.lstrip().startswith(("```", "~~~")):
            fence = line.lstrip()[:3]
            index += 1
            content = []
            while index < len(lines) and not lines[index].lstrip().startswith(fence):
                content.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(_Block("code", "\n".join(content)))
        elif _table_start(lines, index):
            rows = [_cells(line)]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                rows.append(_cells(lines[index]))
                index += 1
            count = max(len(row) for row in rows)
            blocks.append(_Block("table", [row + [""] * (count - len(row)) for row in rows]))
        elif _LIST.match(line):
            ordered = _LIST.match(line).group(1)[0].isdigit()
            items = []
            while index < len(lines):
                item = _LIST.match(lines[index])
                if not item or item.group(1)[0].isdigit() != ordered:
                    break
                items.append((item.group(1), item.group(2)))
                index += 1
            blocks.append(_Block("ordered" if ordered else "list", items))
        elif _RULE.match(line):
            blocks.append(_Block("rule", ""))
            index += 1
        elif line.lstrip().startswith(">"):
            content = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                content.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            blocks.append(_Block("quote", "\n".join(content)))
        else:
            content = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and not _block_start(lines, index):
                content.append(lines[index])
                index += 1
            blocks.append(_Block("paragraph", "\n".join(content)))
    return blocks


def _safe_url(value: str) -> str | None:
    value = value.strip()
    # Markdown permits a single angle-wrapped link destination. The research
    # appendix uses this form so punctuation in source URLs stays unambiguous.
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
    except ValueError:
        return None
    return value


def _inline(text: str, target: str = "html", clean: Callable[[str], str] = lambda text: text) -> str:
    """Escape all input before emitting our own tightly controlled markup."""
    output = []
    offset = 0
    for token in _INLINE.finditer(text):
        output.append(escape(clean(text[offset:token.start()]), quote=True))
        kind = token.lastgroup
        # Named captures inside image/link alternatives make lastgroup unsuitable
        # there; test the outer alternative explicitly.
        if token.group("escaped"):
            output.append(escape(clean(token.group(0)[1:]), quote=True))
        elif token.group("code"):
            content = escape(clean(token.group(0)[1:-1]), quote=True)
            output.append(f"<code>{content}</code>" if target == "html" else content)
        elif token.group("strong"):
            content = escape(clean(token.group(0)[2:-2]), quote=True)
            output.append(f"<b>{content}</b>")
        elif token.group("image"):
            output.append(escape(clean(f"[圖片：{token.group('alt')}]"), quote=True))
        elif token.group("link") or kind == "auto":
            if token.group("link"):
                label, url = token.group("label"), token.group("url")
                suffix = ""
            else:
                raw = token.group(0)
                url = raw[1:-1] if raw.startswith("<") else raw
                url = url.rstrip(".,;:!?。，；：！？）]")
                label = url
                suffix = "" if raw.startswith("<") else raw[len(url):]
            label = escape(clean(label), quote=True)
            safe = _safe_url(url)
            if safe:
                href = escape(safe, quote=True)
                output.append(f'<a href="{href}" rel="noopener noreferrer">{label}</a>'
                              if target == "html" else f'<link href="{href}" color="#215a88">{label}</link>')
            else:
                output.append(label)
            output.append(escape(clean(suffix), quote=True))
        offset = token.end()
    output.append(escape(clean(text[offset:]), quote=True))
    return "".join(output).replace("\n", "<br/>\n")


_CSS = """
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f3f5f8;
color:#243244;font:16px/1.85 'Microsoft JhengHei','PingFang TC',sans-serif}
main{max-width:960px;margin:32px auto;padding:42px 52px;background:white;border-radius:12px}
header{border-bottom:2px solid #dce5ef;margin-bottom:30px;padding-bottom:18px}
h1,h2,h3,h4,h5,h6{color:#183b56;line-height:1.45;margin:1.3em 0 .55em;overflow-wrap:anywhere}
header h1{margin:0 0 12px;font-size:30px}.metadata{color:#65758b;font-size:14px}
p,li,td,th{overflow-wrap:anywhere}a{color:#215a88;text-decoration:underline}
table{border-collapse:collapse;width:100%;margin:22px 0;font-size:14px}
th,td{border:1px solid #dce5ef;padding:9px 12px;text-align:left;vertical-align:top}
th{background:#edf3f9}tr:nth-child(even) td{background:#fafbfd}
blockquote{border-left:4px solid #a5bcd0;color:#52657a;margin:20px 0;padding:4px 18px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:16px;border-radius:6px}
code{font-family:Consolas,'Microsoft JhengHei',monospace;background:#f4f6f8}
hr{border:0;border-top:1px solid #dce5ef;margin:28px 0}
@media(max-width:640px){main{margin:0;padding:24px 20px;border-radius:0}header h1{font-size:25px}}
@media print{body{background:white}main{max-width:none;margin:0;padding:0}thead{display:table-header-group}}
"""


def export_html(report: dict) -> bytes:
    """Return a self-contained reading document; never execute embedded Markdown HTML."""
    title, body, metadata = _fields(report)
    content = []
    for block in _blocks(body):
        if block.kind == "heading":
            content.append(f"<h{block.level}>{_inline(block.value)}</h{block.level}>")
        elif block.kind in {"list", "ordered"}:
            tag = "ol" if block.kind == "ordered" else "ul"
            content.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for _, item in block.value) + f"</{tag}>")
        elif block.kind == "table":
            rows = block.value
            header = "".join(f"<th scope=\"col\">{_inline(cell)}</th>" for cell in rows[0])
            data = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in rows[1:])
            content.append(f"<table><thead><tr>{header}</tr></thead><tbody>{data}</tbody></table>")
        elif block.kind == "code":
            content.append(f"<pre><code>{escape(block.value)}</code></pre>")
        elif block.kind == "rule":
            content.append("<hr>")
        else:
            tag = "blockquote" if block.kind == "quote" else "p"
            content.append(f"<{tag}>{_inline(block.value)}</{tag}>")
    document = (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<header><h1>{escape(title)}</h1><div class=\"metadata\">{escape(' · '.join(metadata))}</div></header>"
        + "\n".join(content) + "</main></body></html>"
    )
    return document.encode("utf-8", errors="replace")


def _pdf_font(pdfmetrics, TTFont, UnicodeCIDFont) -> tuple[str, Callable[[str], str]]:
    """Embed fixed local Windows fonts when present; otherwise use a portable CJK font."""
    name = "ResearchJhengHei"
    if name not in pdfmetrics.getRegisteredFontNames():
        for path in (Path("C:/Windows/Fonts/msjh.ttf"), Path("C:/Windows/Fonts/msjh.ttc")):
            if path.is_file():
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
                    bold_path = Path("C:/Windows/Fonts/msjhbd.ttc")
                    bold_name = name
                    if bold_path.is_file():
                        try:
                            pdfmetrics.registerFont(TTFont(name + "Bold", str(bold_path), subfontIndex=0))
                            bold_name = name + "Bold"
                        except Exception:
                            pass
                    pdfmetrics.registerFontFamily(name, normal=name, bold=bold_name,
                                                  italic=name, boldItalic=bold_name)
                    break
                except Exception:
                    continue
    if name in pdfmetrics.getRegisteredFontNames():
        coverage = pdfmetrics.getFont(name).face.charToGlyph

        def clean(text: str) -> str:
            return "".join(char if ord(char) in coverage or char in "\n\t" else f"[U+{ord(char):04X}]"
                           for char in text if ord(char) >= 32 or char in "\n\t")

        return name, clean
    name = "ResearchMSung"
    if name not in pdfmetrics.getRegisteredFontNames():
        cid_font = UnicodeCIDFont("MSung-Light")
        # Some ReportLab releases pair this Adobe-CNS1 font with UniGB-UCS2-H.
        # Set the matching Traditional Chinese CMap on this font instance only;
        # readers otherwise cannot render the glyphs, despite text extraction.
        cid_font.encodingName = "UniCNS-UCS2-H"
        cid_font.encoding.name = "UniCNS-UCS2-H"
        cid_font.name = cid_font.fontName = name
        pdfmetrics.registerFont(cid_font)
        pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)

    def clean(text: str) -> str:
        # Unicode CID fonts reliably cover BMP Chinese text. Represent unsupported
        # supplementary characters visibly instead of emitting missing-glyph boxes.
        return "".join(char if ord(char) <= 0xFFFF and not 0xD800 <= ord(char) <= 0xDFFF
                       else f"[U+{ord(char):04X}]" for char in text
                       if ord(char) >= 32 or char in "\n\t")

    return name, clean


def export_pdf(report: dict) -> bytes:
    """Return an A4 PDF in memory with Chinese typography and numbered pages."""
    title, body, metadata = _fields(report)
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import HRFlowable, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    except ImportError:
        raise ResearchExportError("PDF 匯出需要 ReportLab，請安裝 reportlab>=4.2.0 後重試。") from None

    try:
        font, clean = _pdf_font(pdfmetrics, TTFont, UnicodeCIDFont)
        normal = ParagraphStyle("ResearchBody", fontName=font, fontSize=10.3, leading=17,
                                alignment=TA_LEFT, wordWrap="CJK", spaceAfter=9,
                                textColor=colors.HexColor("#243244"))
        muted = ParagraphStyle("ResearchMetadata", parent=normal, fontSize=8.4, leading=13,
                               textColor=colors.HexColor("#65758b"), spaceAfter=18)
        title_style = ParagraphStyle("ResearchTitle", parent=normal, fontSize=21, leading=29,
                                     textColor=colors.HexColor("#183b56"), spaceAfter=12)
        headings = {level: ParagraphStyle(f"ResearchH{level}", parent=normal,
                    fontSize=max(10.5, 17 - (level - 1) * 1.6), leading=max(17, 24 - (level - 1)),
                    textColor=colors.HexColor("#183b56"), spaceBefore=13, spaceAfter=8,
                    keepWithNext=True) for level in range(1, 7)}
        quote = ParagraphStyle("ResearchQuote", parent=normal, leftIndent=12,
                               textColor=colors.HexColor("#52657a"))
        item_style = ParagraphStyle("ResearchItem", parent=normal, leftIndent=13,
                                   firstLineIndent=-10, spaceAfter=5)
        stream = BytesIO()
        doc = SimpleDocTemplate(stream, pagesize=A4, leftMargin=45, rightMargin=45,
                                topMargin=49, bottomMargin=46, title=clean(title),
                                author="", subject="研究報告", creator="Research report export")
        story = [Paragraph(escape(clean(title)), title_style)]
        if metadata:
            story.append(Paragraph(escape(clean(" · ".join(metadata))), muted))
        for block in _blocks(body):
            if block.kind == "heading":
                story.append(Paragraph(_inline(block.value, "pdf", clean), headings[block.level]))
            elif block.kind in {"list", "ordered"}:
                for marker, item in block.value:
                    prefix = marker if block.kind == "ordered" else "•"
                    story.append(Paragraph(escape(clean(prefix)) + " " + _inline(item, "pdf", clean), item_style))
                story.append(Spacer(1, 5))
            elif block.kind == "table":
                rows = block.value
                # A very wide edited table is easier to read as labelled records.
                # Keep every cell rather than clipping columns beyond the page.
                if len(rows[0]) > 6:
                    for row_index, row in enumerate(rows[1:], 1):
                        story.append(Paragraph(f"{row_index}.", headings[6]))
                        for label, value in zip(rows[0], row):
                            story.append(Paragraph(_inline(label + "：" + value, "pdf", clean), normal))
                    if len(rows) == 1:
                        story.append(Paragraph(_inline(" | ".join(rows[0]), "pdf", clean), normal))
                    continue
                cell_style = ParagraphStyle("ResearchCell", parent=normal,
                                            fontSize=8.4, leading=13, spaceAfter=0)
                data = [[Paragraph(_inline(cell, "pdf", clean), cell_style) for cell in row] for row in rows]
                table = LongTable(data, colWidths=[doc.width / len(rows[0])] * len(rows[0]),
                                  repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3f9")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfd")]),
                    ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#dce5ef")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]))
                story.extend([table, Spacer(1, 13)])
            elif block.kind == "rule":
                story.extend([Spacer(1, 5), HRFlowable(width="100%", thickness=0.6,
                              color=colors.HexColor("#dce5ef")), Spacer(1, 11)])
            elif block.kind == "code":
                for line in block.value.split("\n"):
                    story.append(Paragraph(escape(clean(line)) or "&#160;", normal))
            else:
                story.append(Paragraph(_inline(block.value, "pdf", clean),
                                       quote if block.kind == "quote" else normal))

        def page_frame(canvas, document):
            canvas.saveState()
            canvas.setFont(font, 8)
            canvas.setFillColor(colors.HexColor("#65758b"))
            short_title = clean(title)
            while short_title and pdfmetrics.stringWidth(short_title, font, 8) > doc.width:
                short_title = short_title[:-1]
            canvas.drawString(doc.leftMargin, A4[1] - 29, short_title)
            canvas.setStrokeColor(colors.HexColor("#dce5ef"))
            canvas.line(doc.leftMargin, 35, A4[0] - doc.rightMargin, 35)
            canvas.drawRightString(A4[0] - doc.rightMargin, 23, clean(f"第 {document.page} 頁"))
            if metadata:
                footer = clean(metadata[0])
                while footer and pdfmetrics.stringWidth(footer, font, 8) > doc.width - 80:
                    footer = footer[:-1]
                canvas.drawString(doc.leftMargin, 23, footer)
            canvas.restoreState()

        doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
        return stream.getvalue()
    except Exception:
        # ReportLab errors may contain raw report text or configuration details.
        raise ResearchExportError("PDF 匯出未完成。請檢查報告內容或 PDF 套件後重試；也可先下載 Markdown 或 HTML。") from None
