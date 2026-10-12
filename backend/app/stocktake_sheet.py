"""The stocktake sheet as a PDF, for walking the market floor with.

The page used to open the sheet in a new window to print, which the desktop
shell blocks ("Allow pop-ups"). Drawn here instead and handed over as a file,
the same way the order sheet is: a PDF opens on a phone, prints from anywhere,
and can be sent to whoever does the count.

It is handed the lines exactly as the Stock page shows them (its tab, market
and search), so the paper and the screen list the same thing. Each delivery
note is a band, each line a row, and the last column is left empty for the
count. Nothing is worked out here.
"""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .order_sheet import _int

INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#555555")
HAIRLINE = colors.HexColor("#bbbbbb")
BAND = colors.HexColor("#eef1f6")


def _date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{d.day} {d.strftime('%b')} {d.year}"


def build(groups: list[dict], title: str, counted: str | None) -> bytes:
    """``groups``: [{dn, ref, market, delivered, days, lines: [{product, sent,
    amended, sold, on_hand}]}] as the page shows them."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Stocktake {title}", author="Zaco Agents")
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=16, leading=19, textColor=INK, spaceAfter=2)
    small = ParagraphStyle("s", parent=base["Normal"], fontSize=8.5, leading=11, textColor=MUTED)
    cell = ParagraphStyle("c", parent=base["Normal"], fontSize=8.5, leading=10.5, textColor=INK)
    band = ParagraphStyle("b", parent=cell, fontName="Helvetica-Bold")

    total = sum(max(int(l.get("on_hand") or 0), 0) for g in groups for l in g.get("lines") or [])
    story = [
        Paragraph(f"Stocktake: {title}", h1),
        Paragraph(f"Zaco Agents · the market's count of {_date(counted)} · printed {_date(date.today().isoformat())}"
                  f" · {_int(total)} cartons on {_int(len(groups))} delivery note"
                  f"{'' if len(groups) == 1 else 's'}", small),
        Spacer(1, 5 * mm),
    ]

    rows = [["Product", "Booked", "Sold", "Market says", "Counted"]]
    style = [
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("ALIGN", (1, 0), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for g in groups:
        label = f"DN {g['dn']}" if g.get("dn") else str(g.get("ref") or "No delivery note")
        where = " · ".join(x for x in [g.get("market"),
                                        f"delivered {_date(g['delivered'])}" if g.get("delivered") else "",
                                        f"{g['days']} days" if g.get("days") is not None else ""] if x)
        rows.append([Paragraph(f"{label} <font color='#555555' size='8'>&nbsp;&nbsp;{where}</font>", band), "", "", "", ""])
        r = len(rows) - 1
        style += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), BAND),
                  ("TOPPADDING", (0, r), (-1, r), 5)]
        for l in g.get("lines") or []:
            booked = l["amended"] if l.get("amended") is not None else l.get("sent")
            rows.append([Paragraph(str(l.get("product") or ""), cell), _int(booked), _int(l.get("sold")),
                         _int(l.get("on_hand")), ""])
            r = len(rows) - 1
            style += [("LINEBELOW", (0, r), (-2, r), 0.3, HAIRLINE),
                      ("LINEBELOW", (-1, r), (-1, r), 0.6, INK),
                      ("FONT", (3, r), (3, r), "Helvetica-Bold", 9)]
    if len(rows) == 1:
        rows.append(["Nothing on the floor.", "", "", "", ""])
    table = Table(rows, colWidths=[None, 17 * mm, 15 * mm, 22 * mm, 26 * mm], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 12 * mm),
              Paragraph("Counted by: ______________________________ &nbsp;&nbsp; Date: ______________", cell)]
    doc.build(story)
    return buf.getvalue()
