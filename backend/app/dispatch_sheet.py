"""The dispatch sheet: what goes on the truck to each market.

The order sheet says what to take on. This says where it goes once it is in:
one section per market and agent, the cartons of each product to send there,
what that market has been fetching for it, and why that market. It is the page
handed to whoever loads the trucks, so it is grouped the way the loading is
done, by destination, and a section never breaks from its heading.

Each market carries how it pays. A market sitting on money owed for months is
still named, with that said in bold above its load, because the decision to
send it more is the owner's, not the sheet's.

Loads that are optional, room to grow and test loads, sit below a rule in each
section and are named as such, so nobody sends them thinking they were the
order. Nothing is computed here: it is drawn from `plan["dispatch"]`, which
`procurement.build` worked out, so the paper and the screen cannot disagree.
"""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .order_sheet import (BANDS, HAIRLINE, MONTHS, MUTED, RULE, TABLE_STYLE, _int, _styles,
                          horizon, month_label, rand)

COLS = [None, 18 * mm, 27 * mm, 28 * mm]
KIND = {"stretch": "Room to grow, optional", "test": "Test load, optional"}
NO_MARKET = "No destination on record"


def filename(plan: dict) -> str:
    days = (plan.get("horizon") or {}).get("days")
    span = f"-{days}d" if days and days != 30 else ""
    return f"zaco-dispatch-{plan.get('month') or 'plan'}{span}.pdf"


def _header(plan: dict, st: dict, today: date, prepared_for: str | None) -> list:
    prepared = f"{today.day} {MONTHS[today.month - 1]} {today.year}"
    covers = horizon(plan)
    if (plan.get("horizon") or {}).get("is_month", True):
        covers += f" ({month_label(plan.get('month'))})"
    who = (prepared_for or "").strip()
    left = [Paragraph("Dispatch sheet", st["title"]),
            Paragraph(f"For {covers} &middot; prepared {prepared}", st["sub"]),
            Paragraph(f"For: <b>{who}</b>" if who else "For: ______________________________",
                      st["sub"])]
    right = [Paragraph("<b>Zaco Agents (Pty) Ltd</b>", st["right"]),
             Paragraph("What goes to which market", st["right"])]
    band = Table([[left, right]], colWidths=[105 * mm, 75 * mm])
    band.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1.4, RULE),
    ]))
    return [band, Spacer(1, 7)]


def _lede(plan: dict, st: dict) -> Paragraph:
    d = plan.get("dispatch") or {}
    markets = [g for g in d.get("markets", []) if g["market"] != NO_MARKET]
    split = d.get("split_lines") or 0
    return Paragraph(
        f"Send <b>{_int(d.get('cartons'))} cartons</b> to <b>{len(markets)} market"
        f"{'' if len(markets) == 1 else 's'}</b> for <b>{horizon(plan)}</b>. Each product goes "
        f"first to the market that returns it best, up to what that market has shown it can "
        f"take, then to the next"
        f"{f'; {split} product' + (' is' if split == 1 else 's are') + ' split across markets' if split else ''}. "
        f"Optional loads, room to grow and tests, sit below the line in each market and come "
        f"to {_int(d.get('optional'))} cartons more.", st["lede"])


def _pays(g: dict, st: dict) -> Paragraph | None:
    bits = []
    if g.get("days_to_pay") is not None:
        bits.append(f"pays about {g['days_to_pay']:.0f} day{'' if g['days_to_pay'] == 1 else 's'} "
                    f"after the sale")
    # The note, where there is one, already says what is owed and for how long.
    if g.get("owed") and not g.get("note"):
        old = f", the oldest {g['oldest_days']} days" if g.get("oldest_days") is not None else ""
        bits.append(f"owes {rand(g['owed'])} now{old}")
    if not bits and not g.get("note"):
        return None
    text = "; ".join(bits)
    text = (text[:1].upper() + text[1:] + ".") if text else ""
    if g.get("note"):
        text += f" <b>{g['note'][:1].upper() + g['note'][1:]}.</b>"
    return Paragraph(text.strip(), st["cellsub"])


# The heading travels with this many rows of its load; the rest may go on to
# the next page. Kept whole, a long market left the first page blank.
KEEP_ROWS = 5


def _table(rows: list, width: float, header: bool, total: bool, divider: int | None) -> Table:
    widths = [width - sum(c for c in COLS if c)] + [c for c in COLS if c]
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
             ("LINEBELOW", (0, 0), (-1, -1), 0.3, HAIRLINE),
             ("FONTSIZE", (0, 0), (-1, -1), 8.5)]
    if header:
        style += [s for s in TABLE_STYLE if s[1] == (0, 0) and s[2] == (-1, 0)]
    if total:
        style += [("LINEABOVE", (0, -1), (-1, -1), 0.9, RULE),
                  ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                  ("FONTSIZE", (0, -1), (-1, -1), 8.5), ("LINEBELOW", (0, -1), (-1, -1), 0, HAIRLINE)]
    if divider is not None:
        style.append(("LINEABOVE", (0, divider), (-1, divider), 0.9, RULE))
    table.setStyle(TableStyle(style))
    return table


def _section(g: dict, st: dict, width: float) -> list:
    data = [["PRODUCT", "CARTONS", "A CARTON", "SHOULD RETURN"]]
    divider = None
    for line in g["lines"]:
        if line["kind"] != "order" and divider is None:
            divider = len(data)
        name = [Paragraph(str(line.get("product") or ""), st["cell"])]
        sub = []
        if line["kind"] in KIND:
            sub.append(f"<b>{KIND[line['kind']]}</b>")
        elif line.get("priority") in BANDS:
            sub.append(BANDS[line["priority"]])
        if line.get("over"):
            sub.append(f"<b>{_int(line['over'])} more than this market has taken in that time</b>")
        if line.get("why"):
            sub.append(str(line["why"]))
        name.append(Paragraph(" &middot; ".join(sub), st["cellsub"]))
        data.append([name, _int(line.get("cartons")),
                     rand(line["price"]) if line.get("price") else "—",
                     rand(line["value"]) if line.get("value") else "—"])
    data.append([f"{len([l for l in g['lines'] if l['kind'] == 'order'])} to send"
                 + (f", {_int(g['optional'])} cartons optional" if g.get("optional") else ""),
                 _int(g.get("cartons")), "", rand(g.get("value"))])

    agent = g.get("market_agent")
    title = (f"{g['market']}{f' &middot; {agent}' if agent else ''}"
             if g["market"] != NO_MARKET else "Not placed yet: no market has sold these well enough")
    head = [Paragraph(title, st["market"])]
    if (pays := _pays(g, st)) is not None:
        head += [pays, Spacer(1, 3)]
    cut = 1 + KEEP_ROWS
    if len(data) <= cut + 2:
        return [KeepTogether(head + [_table(data, width, True, True, divider)])]
    first = _table(data[:cut], width, True, False,
                   divider if divider is not None and divider < cut else None)
    rest = _table(data[cut:], width, False, True,
                  divider - cut if divider is not None and divider >= cut else None)
    return [KeepTogether(head + [first]), rest]


def _footer(st: dict, width: float) -> list:
    sign = Table([["Loaded by", "Date"]], colWidths=[width * 0.55, width * 0.45])
    sign.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.6, RULE), ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("FONTSIZE", (0, 0), (-1, 0), 7), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [
        Spacer(1, 10),
        Paragraph("<b>How to read it.</b> Cartons are what to send. \"At about\" is what that "
                  "market has been fetching a carton for that product; \"should return\" is "
                  "the cartons at that price. A market is filled up to what it has taken of a "
                  "product in its best recent month, scaled to the time being sent for, so a "
                  "market is not sent more than it has shown it can sell.", st["foot"]),
        Spacer(1, 4),
        Paragraph("<b>What these figures are not.</b> Zaco takes fruit on consignment and earns "
                  "a commission on what the market returns, so no purchase price exists "
                  "anywhere in this data. Every rand figure is what the market is expected to "
                  "return, never a margin.", st["foot"]),
        Spacer(1, 26), sign]


def build(plan: dict, today: date | None = None, prepared_for: str | None = None) -> bytes:
    """`plan` as `procurement.build` returned it, drawn by destination."""
    today = today or date.today()
    st = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Dispatch sheet, {horizon(plan)}", author="Zaco Agents (Pty) Ltd",
        subject="What goes to which market")
    width = doc.width
    markets = (plan.get("dispatch") or {}).get("markets") or []
    story: list = _header(plan, st, today, prepared_for)
    story += [_lede(plan, st), Spacer(1, 6)]
    if not markets:
        story.append(Paragraph("Nothing to send: every line is covered by stock already on "
                               "the floor.", st["body"]))
    # Placed markets first, biggest load first; the unplaced lines last.
    for g in sorted(markets, key=lambda g: (g["market"] == NO_MARKET, -g["cartons"])):
        story += _section(g, st, width) + [Spacer(1, 9)]
    story += _footer(st, width)

    def stamp(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm, f"Zaco Agents dispatch sheet · {horizon(plan)}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=stamp, onLaterPages=stamp)
    return buffer.getvalue()
