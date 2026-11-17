"""PDF rendering with ReportLab.

This module is imported inside worker *processes* (parallel PDF rendering), so it must not
import settings, the database or anything with side effects. All text is XML-escaped before it
reaches ReportLab's mini-markup, so stored text can never inject markup.
"""

from __future__ import annotations

import io
import os
import unicodedata
from xml.sax.saxutils import escape

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]
_fonts: tuple[str, str, bool] | None = None


def _register_fonts() -> tuple[str, str, bool]:
    """Return (regular, bold, unicode_capable)."""
    global _fonts
    if _fonts is not None:
        return _fonts
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("PQSans", regular))
                pdfmetrics.registerFont(TTFont("PQSans-Bold", bold))
                _fonts = ("PQSans", "PQSans-Bold", True)
                return _fonts
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
    _fonts = ("Helvetica", "Helvetica-Bold", False)
    return _fonts


def _fmt(seconds: int | float | None) -> str:
    total = max(0, int(round(seconds or 0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render_pdf(payload: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font, bold, unicode_ok = _register_fonts()

    def safe(text: object) -> str:
        value = str(text or "")
        if not unicode_ok:
            value = unicodedata.normalize("NFKD", value).encode("latin-1", "replace").decode("latin-1")
        return escape(value).replace("\n", "<br/>")

    brand = colors.HexColor("#6d28d9")
    muted = colors.HexColor("#52525b")
    line = colors.HexColor("#e4e4e7")
    base = ParagraphStyle("base", fontName=font, fontSize=9.5, leading=13.5, textColor=colors.HexColor("#18181b"))
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=muted)
    h1 = ParagraphStyle("h1", parent=base, fontName=bold, fontSize=20, leading=25, textColor=brand, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=13, leading=17, spaceBefore=10, spaceAfter=6, textColor=brand)
    h3 = ParagraphStyle("h3", parent=base, fontName=bold, fontSize=10.5, leading=14, spaceBefore=4)
    cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=12)
    cell_bold = ParagraphStyle("cellb", parent=cell, fontName=bold)

    podcast = payload["podcast"]
    plan = payload["plan"]
    is_host = podcast.get("mode") == "host"
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=str(podcast.get("title", "Podcast"))[:120],
        author=str(payload.get("app_name", "Podcast Q&A Studio")),
        subject="Podcast questions and answers",
    )
    story: list = []
    story.append(Paragraph(safe(podcast.get("title")), h1))
    mode_label = "Interview question plan" if is_host else "Guest preparation pack"
    story.append(Paragraph(safe(f"{mode_label} - generated {payload.get('generated_at', '')}"), small))
    story.append(Spacer(1, 8))

    meta_rows = []
    for label, key in (
        ("Theme", "theme"),
        ("Show", "show_name"),
        ("Host", "host_name"),
        ("Guest", "guest_name"),
        ("Audience", "audience"),
        ("Tone", "tone"),
        ("Language", "language"),
    ):
        if podcast.get(key):
            meta_rows.append([Paragraph(safe(label), cell_bold), Paragraph(safe(podcast[key]), cell)])
    meta_rows.append(
        [
            Paragraph("Run time", cell_bold),
            Paragraph(
                safe(f"Target {_fmt(plan['target_seconds'])} | planned {_fmt(plan['planned_seconds'])} "
                     f"(intro {_fmt(plan['intro_seconds'])}, questions {_fmt(plan['questions_seconds'])}, "
                     f"outro {_fmt(plan['outro_seconds'])})"),
                cell,
            ),
        ]
    )
    meta = Table(meta_rows, colWidths=[28 * mm, None])
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(meta)

    for title, key in (("Brief", "brief"), ("About the guest", "guest_bio"), ("Key messages", "talking_points")):
        if podcast.get(key):
            story.append(Paragraph(safe(title), h2))
            story.append(Paragraph(safe(podcast[key]), base))
    instructions = podcast.get("standing_instructions") or []
    if instructions:
        story.append(Paragraph("Standing instructions", h2))
        for item in instructions:
            story.append(Paragraph(safe(f"- {item}"), base))

    story.append(Paragraph("Run of show", h2))
    rows = [[Paragraph(h, cell_bold) for h in ("#", "Starts", "Question", "Type", "Slot")]]
    clock = int(plan["intro_seconds"])
    rows.append([Paragraph("-", cell), Paragraph("0:00", cell), Paragraph("Intro", cell), Paragraph("", cell), Paragraph(safe(_fmt(plan["intro_seconds"])), cell)])
    for item in payload["questions"]:
        rows.append(
            [
                Paragraph(safe(item["number"]), cell),
                Paragraph(safe(_fmt(clock)), cell),
                Paragraph(safe(item["text"]), cell),
                Paragraph(safe(item["category"].replace("_", " ")), cell),
                Paragraph(safe(_fmt(item["est_seconds"])), cell),
            ]
        )
        clock += int(item["est_seconds"])
    rows.append([Paragraph("-", cell), Paragraph(safe(_fmt(clock)), cell), Paragraph("Outro", cell), Paragraph("", cell), Paragraph(safe(_fmt(plan["outro_seconds"])), cell)])
    table = Table(rows, colWidths=[9 * mm, 15 * mm, None, 24 * mm, 14 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ede9fe")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(table)

    with_answers = [q for q in payload["questions"] if q.get("answer")]
    if with_answers:
        story.append(Paragraph("Answers and coaching", h2))
        for item in with_answers:
            answer = item["answer"]
            block = [Paragraph(safe(f"Q{item['number']}. {item['text']}"), h3)]
            score = answer.get("score")
            info = f"{answer.get('word_count', 0)} words, about {_fmt(answer.get('est_seconds'))} spoken"
            if score is not None:
                info = f"Score {score}/10 - " + info
            block.append(Paragraph(safe(info), small))
            block.append(Paragraph(safe(answer.get("text", "")), base))
            for label, key in (("What worked", "worked"), ("To improve", "improve")):
                values = answer.get(key) or []
                if values:
                    block.append(Paragraph(safe(label), cell_bold))
                    block.extend(Paragraph(safe(f"- {v}"), base) for v in values)
            if answer.get("stronger_answer"):
                block.append(Paragraph("Stronger version", cell_bold))
                block.append(Paragraph(safe(answer["stronger_answer"]), base))
            if answer.get("timing"):
                block.append(Paragraph(safe(f"Timing: {answer['timing']}"), small))
            block.append(Spacer(1, 6))
            story.append(KeepTogether(block))

    story.append(Spacer(1, 10))
    story.append(Paragraph("Disclaimer", h3))
    story.append(Paragraph(safe(payload.get("disclaimer", "")), small))

    footer_text = str(payload.get("footer", ""))

    def footer(canvas, document) -> None:  # noqa: ANN001 - reportlab callback
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(16 * mm, 10 * mm, footer_text[:150] if unicode_ok else footer_text[:150].encode("latin-1", "replace").decode("latin-1"))
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
