"""Opening stock is a running balance, not a per-file figure.

A consignment sits on the market floor and is sold off over several account
sales. Each of those is a row, and the row's Opening Stock is what was still on
the floor when that run started::

    sent 120 -> opening 120, sold 115, left 5
                opening   5, sold   5, left 0

Which means a row's opening stock depends on **every earlier account sale for
the same consignment**, and those are not necessarily in the same file. Sales
arrive weekly, a consignment straddles the boundary, and the earlier weeks may
already be saved in the history. Carrying stock forward inside one file was
right for 33 of 35 rows checked against the historical sheet; the two that were
wrong were both consignments that had started selling the month before.

So the carry-forward runs once over everything a round has, and is told what a
consignment had already sold before the round began.
"""

from __future__ import annotations

from datetime import date

from .schemas import Flag, StatementRow


def _order(row: StatementRow) -> tuple:
    """Rows of one consignment, in the order they were settled."""
    return (
        row.invoice_date or row.last_sale or date.max,
        row.stm_no if row.stm_no is not None else 1 << 62,
    )


def carry_forward(rows: list[StatementRow], already_sold: dict[int, int] | None = None) -> int:
    """Set Opening Stock across a whole round, in place. Returns rows changed.

    ``already_sold`` maps a consignment id to cartons it had sold in earlier
    rounds, so the first row in this round opens where the last saved row left
    off rather than back at the full delivery. Consignments it says nothing
    about start at what was sent, which is correct for a consignment first seen
    in this round.

    A consignment with no id cannot be tracked across files, so its rows are
    left exactly as the parser set them rather than being pooled with others.
    """
    prior = already_sold or {}
    groups: dict[int, list[StatementRow]] = {}
    for row in rows:
        if row.consignment_id is not None:
            groups.setdefault(row.consignment_id, []).append(row)

    changed = 0
    for consignment, group in groups.items():
        group.sort(key=_order)
        sent = next((r.qty_received for r in group if r.qty_received is not None), None)
        if sent is None:
            continue
        opening = sent - prior.get(consignment, 0)
        for row in group:
            if row.opening_stock != opening:
                row.opening_stock = opening
                changed += 1
            else:
                row.opening_stock = opening
            opening -= row.cartons_sold or 0
    return changed


def flag_impossible_stock(rows: list[StatementRow]) -> int:
    """Flag rows that sold more than was on the floor. Returns rows flagged.

    Not a parser fault: the historical sheet has these too (a delivery amended
    after the fact, a return booked against the wrong consignment). Worth
    saying, because Baby Stock is computed now and a negative leftover would
    otherwise appear in the sheet with no explanation.
    """
    flagged = 0
    for row in rows:
        left = row.baby_stock
        if left is None or left >= 0:
            continue
        row.flags.append(
            Flag(
                field="opening_stock",
                severity="warning",
                message=(
                    f"Sold {row.cartons_sold} out of {row.opening_stock} on the floor, "
                    f"so this leaves {left}. Check the delivery quantity or a return "
                    "booked against this consignment."
                ),
            )
        )
        flagged += 1
    return flagged
