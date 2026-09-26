"""The buy plan as a document someone else can act on.

The screen is for deciding; this is for sending. Zaco's buyer does not sit in
front of the app: he gets told what to order, and he needs it in the form
everything else in this trade arrives in, a PDF on a phone. The browser's own
print dialog can make one, but in the desktop shell it offers no preview and
the destination has to be found every time, so the plan is drawn here instead
and handed over as a file.

Same content as the printable sheet in the page, deliberately: what to order
per market, the room to grow on top of it, what the market should return, and
the test loads worth taking a chance on. Nothing here computes anything -- it
is handed the plan `procurement.build` already worked out, so the paper and
the screen can never disagree about a figure.

It says on its face what the figures are not. The sheet leaves the building
with nobody attached to explain it, and every rand on it is what a market is
expected to RETURN, not a margin: Zaco takes fruit on consignment, so no
purchase price exists anywhere in this data.
"""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

BANDS = {"critical": "Critical", "high": "High", "steady": "Steady", "hold": "Hold"}
BAND_ORDER = ["critical", "high", "steady", "hold"]

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

INK = colors.HexColor("#111111")
RULE = colors.HexColor("#111111")
HAIRLINE = colors.HexColor("#bbbbbb")
MUTED = colors.HexColor("#555555")
BAND_FILL = colors.HexColor("#f2f2f2")


def rand(value) -> str:
    """R 12 500,00, the way every report in this business writes money."""
    try:
        out = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "R 0,00"
    return "R " + out.replace(",", " ").replace(".", ",")


def _int(value) -> str:
    try:
        return f"{round(float(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def month_label(month: str | None) -> str:
    """"2026-10" as "October 2026"."""
    try:
        year, num = str(month).split("-")[:2]
        return f"{MONTHS[int(num) - 1]} {year}"
    except (ValueError, IndexError):
        return str(month or "")


def _styles() -> dict:
    base = getSampleStyleSheet()["BodyText"]
    body = ParagraphStyle("body", parent=base, fontName="Helvetica", fontSize=9,
                          leading=13, textColor=INK, spaceAfter=0)
    return {
        "title": ParagraphStyle("title", parent=body, fontName="Helvetica-Bold",
                                fontSize=17, leading=20),
        "sub": ParagraphStyle("sub", parent=body, fontSize=8.5, leading=12, textColor=MUTED),
        "right": ParagraphStyle("right", parent=body, fontSize=8, leading=11,
                                alignment=TA_RIGHT, textColor=MUTED),
        "rightbold": ParagraphStyle("rightbold", parent=body, fontName="Helvetica-Bold",
                                    fontSize=9.5, leading=12, alignment=TA_RIGHT),
        "lede": ParagraphStyle("lede", parent=body, fontSize=9, leading=13.5),
        "market": ParagraphStyle("market", parent=body, fontName="Helvetica-Bold",
                                 fontSize=10.5, leading=14, spaceBefore=6, spaceAfter=3),
        "cell": ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11),
        "cellsub": ParagraphStyle("cellsub", parent=body, fontSize=7, leading=9,
                                  textColor=MUTED),
        "foot": ParagraphStyle("foot", parent=body, fontSize=7.5, leading=11, textColor=INK),
        "body": body,
    }


# Product takes what is left; the figures need only what they are.
COLS = [None, 34 * mm, 22 * mm, 26 * mm, 18 * mm]
TRIAL_COLS = [None, 20 * mm, 52 * mm, 26 * mm]

TABLE_STYLE = [
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 6.7),
    ("TEXTCOLOR", (0, 0), (-1, 0), INK),
    ("BACKGROUND", (0, 0), (-1, 0), BAND_FILL),
    ("LINEBELOW", (0, 0), (-1, 0), 0.9, RULE),
    ("LINEBELOW", (0, 1), (-1, -2), 0.3, HAIRLINE),
    ("LINEABOVE", (0, -1), (-1, -1), 0.9, RULE),
    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ("FONTSIZE", (0, -1), (-1, -1), 8.5),
    ("ALIGN", (1, 0), (-2, -1), "RIGHT"),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
]


def _market_groups(lines: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """The lines to order, under the market and agent they are going to."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for line in lines:
        key = (line.get("market") or "No destination on record", line.get("market_agent") or "")
        groups.setdefault(key, []).append(line)
    out = []
    for (market, agent), rows in sorted(groups.items()):
        rows.sort(key=lambda l: (BAND_ORDER.index(l["priority"]) if l.get("priority") in BAND_ORDER
                                 else 9, -l["take_on"]))
        out.append((market, agent, rows))
    return out


def _header(plan: dict, st: dict, today: date, prepared_for: str | None) -> list:
    """Who it is from, who it is for, and what month it covers."""
    # Written out rather than strftime'd: "%-d" is not a thing on Windows, and
    # this runs on the operator's laptop as well as in the function.
    prepared = f"{today.day} {MONTHS[today.month - 1]} {today.year}"
    left = [Paragraph("Procurement order", st["title"]),
            Paragraph(f"{month_label(plan.get('month'))} &middot; prepared {prepared}",
                      st["sub"])]
    who = prepared_for.strip() if prepared_for else ""
    left.append(Paragraph(f"For: <b>{who}</b>" if who
                          else "For: ______________________________", st["sub"]))
    right = [Paragraph("<b>Zaco Agents (Pty) Ltd</b>", st["right"]),
             Paragraph("ZacoAgents procurement plan", st["right"])]
    band = Table([[left, right]], colWidths=[105 * mm, 75 * mm])
    band.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1.4, RULE),
    ]))
    return [band, Spacer(1, 7)]


def _lede(plan: dict, st: dict) -> Paragraph:
    t = plan.get("totals", {})
    return Paragraph(
        f"Order <b>{_int(t.get('cartons'))} cartons</b> across "
        f"<b>{_int(t.get('to_take_on'))} lines</b>, which the markets are expected to return "
        f"<b>{rand(t.get('expected_value'))}</b> on. Quantities are what each market is "
        f"expected to sell next month less the {_int(t.get('on_hand'))} cartons already "
        f"sitting on its floor. Room to grow is extra on top of the order, offered only "
        f"where the market sold every carton sent, cleared it within a couple of days and "
        f"held the market average.", st["lede"])


def _market_table(market: str, agent: str, rows: list[dict], st: dict, width: float) -> KeepTogether:
    data = [["PRODUCT", "ORDER", "ROOM TO GROW", "SHOULD RETURN", "PRIORITY"]]
    for line in rows:
        name = [Paragraph(str(line.get("product") or ""), st["cell"])]
        if line.get("on_hand"):
            name.append(Paragraph(f"{_int(line['on_hand'])} already on the floor, "
                                  f"taken off this order", st["cellsub"]))
        grow = line.get("headroom")
        data.append([name, _int(line.get("take_on")),
                     f"+{_int(grow['cartons'])}" if grow else "—",
                     rand(line.get("expected_value")),
                     BANDS.get(line.get("priority"), "")])
    cartons = sum(l.get("take_on") or 0 for l in rows)
    grown = sum(l["headroom"]["cartons"] for l in rows if l.get("headroom"))
    value = sum(l.get("expected_value") or 0 for l in rows)
    data.append([f"{len(rows)} line{'' if len(rows) == 1 else 's'}", _int(cartons),
                 f"+{_int(grown)}" if grown else "—", rand(value), ""])

    widths = [width - sum(c for c in COLS if c)] + [c for c in COLS if c]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(TABLE_STYLE))
    heading = Paragraph(f"{market}{f' &middot; {agent}' if agent else ''}", st["market"])
    # The heading and the first rows travel together: a market name alone at the
    # foot of a page is how an order gets read as belonging to the wrong agent.
    return KeepTogether([heading, table])


def _trials_table(lines: list[dict], st: dict, width: float) -> KeepTogether | None:
    rows = [l for l in lines if l.get("trial")]
    if not rows:
        return None
    data = [["PRODUCT", "TEST LOAD", "SEND IT TO", "WORTH ABOUT"]]
    for line in rows:
        t = line["trial"]
        data.append([
            [Paragraph(str(line.get("product") or ""), st["cell"]),
             Paragraph(f"goes to {line.get('market') or 'one market'} today", st["cellsub"])],
            _int(t.get("cartons")),
            Paragraph(str(t.get("market") or "")
                      + (f" &middot; {t['market_agent']}" if t.get("market_agent") else ""),
                      st["cell"]),
            rand(t.get("worth"))])
    data.append([f"{len(rows)} test load{'' if len(rows) == 1 else 's'}",
                 _int(sum(l["trial"]["cartons"] for l in rows)), "",
                 rand(sum(l["trial"]["worth"] for l in rows))])
    widths = [width - sum(c for c in TRIAL_COLS if c)] + [c for c in TRIAL_COLS if c]
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [s for s in TABLE_STYLE if s[0] != "ALIGN"]
    style += [("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT")]
    table.setStyle(TableStyle(style))
    return KeepTogether([
        Paragraph("Worth a try: markets these have never been to", st["market"]), table])


def _footer(plan: dict, st: dict, width: float) -> list:
    out = [Spacer(1, 10),
           Paragraph("<b>How to read it.</b> Order is cartons. Priority is a fixed mark on "
                     "how a product has been trading, the same mark every month, not a "
                     "ranking within this list.", st["foot"]),
           Spacer(1, 4),
           Paragraph("<b>What these figures are not.</b> Zaco takes fruit on consignment "
                     "and earns a commission on what the market returns, so no purchase "
                     "price exists anywhere in this data. Every rand figure here is what "
                     "the market is expected to return, never a margin.", st["foot"])]
    for caveat in (plan.get("caveats") or [])[:4]:
        out.append(Spacer(1, 3))
        out.append(Paragraph(f"&bull; {caveat}", st["foot"]))
    sign = Table([["Ordered by", "Date"]], colWidths=[width * 0.55, width * 0.45])
    sign.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.6, RULE),
        ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    out += [Spacer(1, 26), sign]
    return out


def filename(plan: dict) -> str:
    """What the file is called once it lands in his downloads."""
    return f"zaco-procurement-{plan.get('month') or 'plan'}.pdf"


def build(plan: dict, today: date | None = None, prepared_for: str | None = None) -> bytes:
    """The plan as a PDF, ready to send.

    `plan` is exactly what `procurement.build` returned and what the screen is
    showing; nothing is recomputed here.
    """
    today = today or date.today()
    st = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Procurement order, {month_label(plan.get('month'))}",
        author="Zaco Agents (Pty) Ltd", subject="What to take on, and where to send it")
    width = doc.width

    lines = [l for l in plan.get("lines", []) if (l.get("take_on") or 0) > 0]
    story: list = _header(plan, st, today, prepared_for)
    story += [_lede(plan, st), Spacer(1, 6)]
    if lines:
        for market, agent, rows in _market_groups(lines):
            story.append(_market_table(market, agent, rows, st, width))
            story.append(Spacer(1, 8))
    else:
        story.append(Paragraph("Nothing to order: every line is covered by stock already "
                               "on the floor.", st["body"]))
    if (trials := _trials_table(plan.get("lines", []), st, width)) is not None:
        story += [Spacer(1, 4), trials]
    story += _footer(plan, st, width)

    def stamp(canvas, document):
        """Page numbers, so a printed order cannot be read half missing."""
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm,
                          f"Zaco Agents procurement order · {month_label(plan.get('month'))}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=stamp, onLaterPages=stamp)
    return buffer.getvalue()
