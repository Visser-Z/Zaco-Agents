"""Sales reports that cover days already read, whether saved or in the same drop.

Dropping the same days twice has to be harmless, because it is how a problem
is diagnosed: pull the market's report for a week, drop it over the days
already on the book, and see where the two disagree. So a day already on the
book stays exactly as it was saved, a day that is new is added, and a day where
the report now says something different is reported, not quietly rewritten.

A day's identity is its consignment and the day it sold. Not the statement
number: the PDF files a row under its Consignment ID and the CSV export under
its account sale, so keyed on the statement the same day read from the two
exports looked like two different days and was counted twice.
"""

from __future__ import annotations

from .schemas import Flag, StatementRow

# The codes the review screen keys on. All three mean "do not add this row".
ALREADY = "duplicate"          # on the book, and the report agrees
DIFFERS = "duplicate_differs"  # on the book, and the report says otherwise
REPEAT = "repeat"              # a second copy of a day in this same drop


def day_of(row: StatementRow) -> str | None:
    """The day a row sold. Its date is the consignment's, shared by every day
    it traded, so it only stands in when the row has no sale date."""
    sold = row.last_sale or row.date
    return sold.isoformat() if sold else None


def key(row: StatementRow) -> tuple | None:
    """What makes two rows the same day of the same fruit."""
    day = day_of(row)
    if row.consignment_id:
        return ("c", int(row.consignment_id), day)
    if row.stm_no is not None:
        return ("s", int(row.stm_no), day)
    return None


def collapse_batch(rows: list[StatementRow]) -> tuple[list[StatementRow], list[StatementRow]]:
    """One row per day per consignment across everything dropped together.

    A day's report and the week's report that covers it both hold that day.
    The copy that sold more cartons is kept: sales on a day only grow as the
    day goes on, so the fuller copy is the later word on it. The other is
    returned apart and marked, so the screen can say it was left out and why.
    """
    kept: dict[tuple, StatementRow] = {}
    order: list[tuple] = []
    loose: list[StatementRow] = []
    dropped: list[StatementRow] = []
    for row in rows:
        k = key(row)
        if k is None:
            loose.append(row)
            continue
        have = kept.get(k)
        if have is None:
            kept[k] = row
            order.append(k)
            continue
        if (row.cartons_sold or 0) > (have.cartons_sold or 0):
            kept[k], row = row, have
        dropped.append(row)
    for row in dropped:
        other = kept[key(row)]
        row.flags.append(Flag(
            field="stm_no", severity="warning", code=REPEAT,
            message=f"The same day is also in {other.source_file}, which is the copy used."))
    return [kept[k] for k in order] + loose, dropped


def mark_saved(rows: list[StatementRow], saved: list[dict]) -> None:
    """Mark each row whose day is already on the book, and say whether the
    report agrees with what was saved. What was saved is left as it is."""
    book: dict[tuple, dict] = {}
    for rec in saved:
        day = (rec.get("sale_day") or rec.get("last_sale") or "")[:10] or None
        cid = rec.get("consignment_id") or 0
        k = ("c", int(cid), day) if cid else ("s", int(rec["stm_no"]), day)
        b = book.setdefault(k, {"value": 0.0, "cartons": 0, "created_at": rec.get("created_at"),
                                "agent": rec.get("market_agent")})
        b["value"] += float(rec.get("sales_total") or 0)
        b["cartons"] += int(rec.get("cartons_sold") or 0)
    for row in rows:
        k = key(row)
        b = book.get(k) if k else None
        if b is None:
            continue
        when = (b.get("created_at") or "")[:10]
        on = f" on {when}" if when else ""
        value = round(float(row.sales_total or 0), 2)
        cartons = int(row.cartons_sold or 0)
        if abs(value - b["value"]) < 0.005 and cartons == b["cartons"]:
            row.flags.append(Flag(
                field="stm_no", severity="warning", code=ALREADY,
                message=f"Already on the book (saved{on}). Left as it is."))
        else:
            row.flags.append(Flag(
                field="stm_no", severity="warning", code=DIFFERS,
                message=(f"Already on the book (saved{on}) as {b['cartons']} cartons for "
                         f"R {b['value']:,.2f}; this report says {cartons} cartons for "
                         f"R {value:,.2f}. The saved figures are kept.")))
