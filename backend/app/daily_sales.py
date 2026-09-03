"""Parser for the newer "Consignment Reports / Daily Sales Detail" format.

This is a different report from the old account-sales statement (see
``extraction.py``): it lists consignments and their individual sale dockets,
with no deduction/nett section. It is a "Pre" report — sales detail only.

Mapping to the workbook's A-T columns was reverse-engineered from a real
human-filled sheet (Book1.xlsx). The confident columns are filled here; Nett
(column N) has no source on this report and is left for the operator, per their
instruction. Assumptions that still need confirming against a matched
PDF/sheet pair are called out in comments and as row flags.

Layout of one consignment block::

    TSHWANE MARKET      Farmers Trust (Pre)
    Delivery ID: 1181705Z Supplier Ref:  Qty Sent: 71 Qty Amended To: Qty Avail: 70
    Consignment ID: 118170501Z Comment:
    Product:  NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 11kg
      Date Sold  Docket Number Qty Sold Market Avg   Price     Sales Value
    2026-07-27 PRE*B6G27S89670*01Z  1        R 0.00       R 50.00   R 50.00
                                    1                               R 50.00   <- subtotal
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .schemas import Flag, StatementRow

# --- format detection -----------------------------------------------------

_DAILY_MARKERS = (
    re.compile(r"Daily\s+Sales\s+Detail", re.I),
    re.compile(r"Consignment\s+Reports", re.I),
    re.compile(r"Delivery\s+ID\s*:", re.I),
)


def is_daily_sales(text: str) -> bool:
    """True if the text looks like the Daily Sales Detail report."""
    return sum(bool(p.search(text)) for p in _DAILY_MARKERS) >= 2


# --- labels ---------------------------------------------------------------

_BLOCK_START = re.compile(r"^\s*Delivery\s+ID\s*:", re.I | re.M)
# The market/agent header, e.g. "TSHWANE MARKET      Farmers Trust (Pre)" or
# "JOBURG MKT - TFRESH Subtropico (Jhb)". An ALL-CAPS market/depot prefix, then
# a Title-Case agent name, then a parenthesised region. Requiring the region
# in parentheses keeps the "Market Avg  Price  Sales Value" table header (which
# has no parentheses) from matching. Group 1 is the market/depot (fills the
# `market` field, kept for per-market analytics); group 2 is the agent (column B).
_MARKET_AGENT = re.compile(
    r"^\s*([A-Z0-9][A-Z0-9 /.\-]*?)\s+([A-Z][a-z][\w .'&-]*?)\s*\([^)]*\)\s*$", re.M
)
_DELIVERY_ID = re.compile(r"Delivery\s+ID\s*:\s*(\S+)", re.I)
# "Supplier Ref: 20026*14847" -- the DN shared with Payment Details. Often blank
# on older exports; keep the number after the '*'. This is the reconciliation key.
# The prefix is the producer code and is NOT always Zaco's own 20026: produce
# delivered for another producer carries theirs (e.g. 14013*14798). Requiring
# 20026 silently dropped the supplier ref on those, which then could not
# reconcile against their payment at all.
_SUPPLIER_REF = re.compile(r"Supplier\s+Ref\s*:\s*\d+\*\s*(\d+)", re.I)
_CONSIGNMENT_ID = re.compile(r"Consignment\s+ID\s*:\s*(\S+)", re.I)
_QTY_SENT = re.compile(r"Qty\s+Sent\s*:\s*(\d+)", re.I)
_QTY_AVAIL = re.compile(r"Qty\s+Avail\s*:\s*(\d+)", re.I)
_PRODUCT = re.compile(r"^\s*Product\s*:\s*(.+?)\s*$", re.I | re.M)
# The Product line runs to end of line, and on some exports a neighbouring
# column bleeds a bare number onto it:
#   "GRAPES STARLIGHT CLASS 2 NO SIZE (PUNNET 5kg) 10"
# Left in, the same product reads as two different ones -- splitting its totals
# and stopping it matching the payment report. Every real case follows the
# closing bracket of the container, and no genuine name ends in a bare number,
# so the bracket is required before stripping.
_PRODUCT_TAIL = re.compile(r"(?<=\))\s+\d+$")
# A docket/sale line: date, docket no, qty sold, market avg, price, sales value.
_DOCKET = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+\S+\s+(-?\d+)\s+"
    r"R\s*(-?[\d,]*\.?\d*)\s+"        # Market Avg
    r"R\s*(-?[\d,]*\.?\d*)\s+"        # Price
    r"R\s*(-?[\d,]*\.?\d*)\s*$",      # Sales Value
    re.M,
)


def _market_avg(dockets) -> float | None:
    """What the market averaged for this commodity, over the dockets that say.

    Weighted by cartons, so a one-carton line cannot outvote a hundred-carton
    one. Dockets reporting 0.00 are not reporting an average and are left out
    rather than dragging it down; if none of them report one, the answer is that
    we do not know, not zero.
    """
    weighted = total = 0.0
    for _, qty, avg, _, _ in dockets:
        q, a = abs(int(qty)), _num(avg)
        if q and a > 0:
            weighted += a * q
            total += q
    return round(weighted / total, 2) if total else None


def _num(token: str) -> float:
    return float(token.replace(",", "").replace(" ", ""))


def _strip_id(raw: str) -> int | None:
    """Delivery/Consignment IDs carry a trailing letter (…01Z). The workbook
    holds them as plain numbers, so keep the digits only."""
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def _blocks(text: str) -> list[str]:
    """Split the document into one text block per consignment."""
    starts = [m.start() for m in _BLOCK_START.finditer(text)]
    if not starts:
        return []
    bounds = starts + [len(text)]
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _market_agent_for(block: str, text: str) -> tuple[str | None, str | None]:
    """Return (market, agent) for a consignment block.

    The 'MARKET  Agent (Pre)' line sits just above the Delivery ID line, so
    search the whole document up to this block's start for the nearest one.
    """
    upto = text[: text.find(block) + 1] if block in text else text
    matches = _MARKET_AGENT.findall(upto)
    if not matches:
        return None, None
    market, agent = matches[-1]
    return " ".join(market.split()) or None, agent.strip() or None


def parse_daily_sales(pages: list[str], filename: str) -> list[StatementRow]:
    """One workbook row per consignment (its dockets summed into a subtotal).

    The CSV export of this same report produces one row per **account sale**
    instead, which is the shape the workbook is actually kept in (see
    ``csv_reports._statement_rows``). That split is only possible because the CSV
    names, on every docket, the account sale it was paid under. **The PDF does
    not print it at all**, so this parser cannot tell one run from the next and
    its column E holds the Consignment ID rather than an account sale number.
    Prefer the CSV wherever there is a choice.
    """
    text = "\n".join(pages)
    rows: list[StatementRow] = []

    for i, block in enumerate(_blocks(text), start=1):
        flags: list[Flag] = []
        name = filename if len(_blocks(text)) == 1 else f"{filename} · consignment {i}"
        row = StatementRow(source_file=name)

        row.market, row.market_agent = _market_agent_for(block, text)

        # Supplier Ref is the DN the payment documents (and the historical
        # workbook) use, so it fills column A. Older exports leave it blank, so
        # fall back to the Delivery ID there.
        delivery = _strip_id(m.group(1)) if (m := _DELIVERY_ID.search(block)) else None
        if m := _SUPPLIER_REF.search(block):
            row.supplier_ref = int(m.group(1))
        row.dn = row.supplier_ref if row.supplier_ref is not None else delivery  # A ← Supplier Ref
        if row.dn is None:
            flags.append(Flag(field="dn", message="No Supplier Ref or Delivery ID on this consignment."))

        if m := _CONSIGNMENT_ID.search(block):
            row.stm_no = _strip_id(m.group(1))      # E ← Consignment ID
            # And keep it as the consignment it actually is. Without this the
            # field stays empty, every row becomes its own delivery, and Qty Sent
            # is counted once per row instead of once per consignment -- so a
            # consignment of 18 that sold across three daily reports reports 54
            # cartons sent and a third of its real sell-through.
            row.consignment_id = row.stm_no
        else:
            flags.append(Flag(field="stm_no", message="No Consignment ID on this consignment."))

        if m := _PRODUCT.search(block):
            row.product = _PRODUCT_TAIL.sub("", " ".join(m.group(1).split()))
        else:
            flags.append(Flag(field="product", message="No Product line on this consignment."))

        # Qty Sent (circled green) → Qty Received; Qty Avail (circled cyan) →
        # Opening Stock. Both are the operator's marked fields.
        if m := _QTY_SENT.search(block):
            row.qty_received = int(m.group(1))
        if m := _QTY_AVAIL.search(block):
            row.opening_stock = int(m.group(1))

        # Sum the consignment's docket lines. Qty Sold → Cartons Sold; the
        # weighted price = total sales value / total qty sold recovers the
        # sheet's Gross Total (M = J x L).
        dockets = _DOCKET.findall(block)
        # A negative Qty Sold is a RETURN, not a sale of minus one carton: it
        # reverses a docket already booked. Netting it off is right -- the fruit
        # is back on the floor -- but the sale it reverses did happen, so both
        # halves are kept instead of only their difference. Classified on the
        # quantity's sign; a return's Sales Value prints negative alongside it
        # and is held positive here, so "10 returned, R2,000" reads as it should.
        returns = [(q, v) for _, q, _, _, v in dockets if int(q) < 0]
        returned_qty = -sum(int(q) for q, _ in returns)
        returned_value = abs(sum(_num(v) for _, v in returns))
        total_qty = sum(int(q) for _, q, _, _, _ in dockets)
        total_value = sum(_num(v) for _, _, _, _, v in dockets)
        sale_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d, *_ in dockets)

        row.cartons_sold = total_qty
        row.market_avg = _market_avg(dockets)
        row.cartons_returned = returned_qty
        row.returns_total = round(returned_value, 2)
        row.price = round(total_value / total_qty, 2) if total_qty else 0.0
        # Exact consignment sales value (not cartons x rounded price), so it
        # reconciles to Payment Details' Sales Total to the cent.
        row.sales_total = round(total_value, 2)
        if not dockets:
            flags.append(Flag(field="cartons_sold", message="No sale lines found for this consignment."))

        if sale_dates:
            # Earliest sale date drives both column D (per delivery group) and
            # column T (DD.MM). This is an assumption — Book1's column D looked
            # like a delivery date, which this report doesn't print.
            row.date_received = sale_dates[0]
            row.invoice_date = sale_dates[0]
            row.status = sale_dates[0].strftime("%d.%m")
            # Last docket date. Against date_received this gives how long the
            # consignment took to sell, which ranges from same-day to weeks and
            # is one of the few forward-looking signals the reports carry.
            row.last_sale = sale_dates[-1]

        # Nett Total (N) is not on this report — the operator computes the
        # deduction themselves. Leave it empty and flag for entry.
        row.nett_total = None
        flags.append(
            Flag(
                field="nett_total",
                severity="warning",
                message="Nett is not on this report — enter it (the deduction isn't printed here).",
            )
        )

        row.flags = flags
        rows.append(row)

    return rows
