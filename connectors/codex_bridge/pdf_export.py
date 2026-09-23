"""Private text PDF export. No HTML rendering, scripts, URLs or remote assets."""
from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO
import os
from pathlib import Path
import re
from threading import RLock

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

PDF_LOCK = RLock()


def register_font():
    name = "PaperReaderCJK"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    candidates = [os.environ.get("PAPER_PDF_FONT", ""),
                  str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
                  "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                  "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                pdfmetrics.registerFont(TTFont(name, candidate, subfontIndex=0))
                return name
            except Exception:
                continue
    raise ValueError("PDF 导出需要中文 TrueType 字体。Windows 请检查微软雅黑字体；其他系统安装 fonts-wqy-microhei，或用 PAPER_PDF_FONT 指定中文 TTF/TTC 文件。")


def register_symbols():
    name = "PaperReaderSymbols"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    for path in (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/times.ttf",
                 Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if path.is_file():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            return name
    return None


def text_markup(text, primary, fallback):
    glyphs = pdfmetrics.getFont(primary).face.charToGlyph
    extra = pdfmetrics.getFont(fallback).face.charToGlyph if fallback else {}
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", str(text))
    runs, chars, active = [], [], primary
    for char in clean:
        font = fallback if ord(char) not in glyphs and ord(char) in extra else primary
        if font != active and chars:
            runs.append((active, "".join(chars)))
            chars = []
        active = font
        chars.append(char)
    if chars:
        runs.append((active, "".join(chars)))
    return "".join((f'<font name="{font}">{escape(value)}</font>' if font != primary else escape(value))
                   for font, value in runs).replace("\n", "<br/>")


def export_pdf(paper, messages):
    if not messages:
        raise ValueError("暂无可导出的对话。")
    with PDF_LOCK:
        font = register_font()
        symbols = register_symbols()
        buffer = BytesIO()
        title = str(paper.get("title_zh") or paper.get("title") or "论文对话")
        pdf = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=46, leftMargin=46,
                                topMargin=48, bottomMargin=48, title=title, author="Codex 论文助手")
        normal = ParagraphStyle("body", fontName=font, fontSize=10, leading=17, spaceAfter=7,
                                textColor=colors.HexColor("#181818"), wordWrap="CJK", splitLongWords=True)
        heading = ParagraphStyle("heading", parent=normal, fontSize=13, leading=20, spaceBefore=12, spaceAfter=9)
        meta = ParagraphStyle("meta", parent=normal, fontSize=9, leading=14, textColor=colors.HexColor("#444444"))
        story = []

        def add(text, style=normal):
            # Treat all user/model/paper text as text, never ReportLab markup.
            story.append(Paragraph(text_markup(text, font, symbols), style))

        add(title, ParagraphStyle("title", parent=heading, fontSize=17, leading=25))
        add(f"导出时间：{datetime.now():%Y-%m-%d %H:%M} · 本机论文助手", meta)
        if paper.get("doi"):
            add("DOI：" + paper["doi"], meta)
        if paper.get("landing_url"):
            add("原文链接：" + paper["landing_url"], meta)
        add("P 为原 PDF 页码，S 为网页段落编号；本文件页脚为导出文件页码。译文与分析由模型生成，公式和扫描文字请对照原文核查。", meta)
        for message in messages:
            story.append(Spacer(1, 10))
            label = "你" if message["role"] == "user" else "Codex"
            if message.get("model"):
                label += " · " + message["model"]
            state = message.get("status", "completed")
            if state != "completed":
                label += " · 未完成 / 部分内容"
            add(label, heading)
            # Short paragraphs can break across PDF pages; no fixed-height boxes
            # that cut off long translations or unbroken identifiers.
            for block in re.split(r"\n\s*\n", message.get("content", "")):
                if not block.strip():
                    continue
                is_heading = re.match(r"^#{1,6}\s+", block)
                text = re.sub(r"^#{1,6}\s+", "", block) if is_heading else block
                text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
                add(text, heading if is_heading else normal)
            if message.get("error"):
                add("未完成原因：" + message["error"], meta)

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setFont(font, 8)
            canvas.setFillColor(colors.HexColor("#444444"))
            canvas.drawRightString(A4[0] - 46, 26, f"导出文件 · 第 {doc.page} 页")
            canvas.restoreState()

        pdf.build(story, onFirstPage=footer, onLaterPages=footer)
        return buffer.getvalue()
