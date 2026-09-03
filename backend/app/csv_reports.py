"""Readers for the CSV exports of the same TechnoFresh reports.

The CSVs are strictly better than the PDFs they replace. A PDF is a picture of
a table, so every value has to be recovered from whitespace and column
positions -- the source of every layout bug so far. The CSV *is* the table.

They also carry three fields the PDFs never printed:

  Delivery Date       when the consignment actually arrived, so time on market
                      can be measured from arrival rather than from first sale
  Date Paid           when the money came
  Payment Reference   the AccSale number each docket was paid under

That last one is the important one. It is a direct join between a sale and its
payment, which replaces matching on supplier + product name + value with an
exact key. See ``reconcile.by_payment_reference``.

Both files are report-shaped rather than flat: header bands, indented detail
rows, subtotals and grand totals interleaved. Everything that is not a data row
is skipped explicitly rather than by position, so a change in spacing or an
extra blank line cannot shift the parse.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta

from .daily_sales import _PRODUCT_TAIL, _strip_id
from .nett_adjustments import (
    _acc_to_stmno as acc_to_stmno,
    _ref_producer as ref_producer,
    _ref_to_dn as ref_to_dn,
)
from .schemas import Flag, StatementRow

# --- shared helpers -------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Rows that are totals or repeated headers, never data.
_SKIP = re.compile(r"total sales|daily total|grand total|^line no$|^delivery date$", re.I)


def _rows(text: str) -> list[list[str]]:
    return [_unwrap([c.strip() for c in row]) for row in csv.reader(io.StringIO(text))]


def _unwrap(row: list[str]) -> list[str]:
    """Undo a row that was quoted a second time on its way out of the report.

    Most of the weekly exports arrive double-encoded: the whole line sits inside
    one quoted field with every inner quote doubled, so a nine-column data row
    reads back as::

        ['Delivery ID : ,1178361Z,"Supplier Ref : ",20026*30559,"Qty Sent : ",25']

    ``csv.reader`` is right to hand that back as a single cell, and every parser
    downstream then matches nothing and reports **zero** rows rather than an
    error -- 11 of 14 real weekly files parsed as empty. Re-reading the lone
    cell as CSV recovers the columns. A genuine one-column row has no commas, so
    it passes through untouched.
    """
    if len(row) != 1 or "," not in row[0]:
        return row
    inner = next(csv.reader(io.StringIO(row[0])), None)
    return [c.strip() for c in inner] if inner and len(inner) > 1 else row


def _cell(row: list[str], i: int) -> str:
    return row[i] if i < len(row) else ""


def _num(token: str) -> float | None:
    token = (token or "").replace(",", "").replace(" ", "")
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _int(token: str) -> int | None:
    value = _num(token)
    return int(round(value)) if value is not None else None


def _date(token: str) -> date | None:
    token = (token or "").strip()
    if not _DATE.match(token):
        return None
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_total(row: list[str]) -> bool:
    return any(_SKIP.search(c) for c in row if c)


# --- format detection -----------------------------------------------------

def _is_all(value: str) -> bool:
    return (value or "").strip().upper() in ("", "ALL")


def _agent_name(value: str) -> str:
    """"Farmers Trust (Pre)" -> "Farmers Trust"."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", value or "").strip()


def export_filter(text: str) -> dict:
    """What the export was narrowed to, read from the file itself.

    The Payment Details CSV states its scope in one of two shapes:

        Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"
        Destination,"Subtropico (Jhb)"

    The second names only the agent, so the market stays unknown rather than
    being guessed from the account-sale prefix. An unfiltered export says ALL.
    Anything else means whole markets or agents were left out, and the file
    cannot be trusted as a complete picture of the period -- worth saying out
    loud, because the person exporting usually believes they took everything.

    The header is **not** always unique. A weekly export can hold several
    sections one after another, each with its own header (a Subtropico section
    then a Farmers Trust one). Reading only the first would have reported such a
    file as narrowed to Subtropico while half its money was Farmers Trust, so
    the whole file is scanned and it counts as narrowed only when it declares a
    single specific scope. ``agents`` lists everything it does name.
    """
    scopes: list[tuple[str | None, str | None]] = []
    said_all = False
    for row in _rows(text):
        head = row[0].strip().lower() if row else ""
        if head == "market":
            market, agent = _cell(row, 1), _cell(row, 4)
        elif head == "destination":
            market, agent = "", _cell(row, 1)
        else:
            continue
        if _is_all(market) and _is_all(agent):
            said_all = True
            continue
        scopes.append(
            (None if _is_all(market) else market, None if _is_all(agent) else _agent_name(agent))
        )

    agents = sorted({a for _, a in scopes if a})
    if said_all or not scopes:
        return {"market": None, "agent": None, "agents": agents, "filtered": False}
    if len(scopes) == 1:
        market, agent = scopes[0]
        return {"market": market, "agent": agent, "agents": agents, "filtered": True}
    return {"market": None, "agent": None, "agents": agents, "filtered": False}


def is_daily_sales_csv(text: str) -> bool:
    head = text[:800].lower()
    return "date sold" in head and "docket number" in head and "sales value" in head


def is_payment_details_csv(text: str) -> bool:
    head = text[:800].lower()
    return "acc sales number" in head and "gross payments" in head


def looks_like_csv(filename: str, data: bytes) -> bool:
    if filename.lower().endswith(".csv"):
        return True
    return not data[:5].startswith(b"%PDF")


def decode(data: bytes) -> str:
    """CSV exports are commonly UTF-8 with a BOM, sometimes Latin-1."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --- daily sales ----------------------------------------------------------

# "Delivery ID : ",1176362Z,"Supplier Ref : ",14013*14798,"Qty Sent : ",170,...
_LABEL = re.compile(r"^(delivery id|consignment id|supplier ref|qty sent|qty avail|product)\s*:?\s*$", re.I)


def _labelled(row: list[str]) -> dict[str, str]:
    """Read a "Label :", value, "Label :", value row into a dict."""
    out: dict[str, str] = {}
    for i, cell in enumerate(row):
        m = _LABEL.match(cell.rstrip(" :"))
        if m:
            out[m.group(1).lower()] = _cell(row, i + 1)
    return out


def _settled_under(ref: str | None) -> str:
    """The account sale a docket was paid under, or "" if it has not been paid.

    A docket awaiting payment carries a **placeholder** reference, not a blank
    one: the real export writes ``PRE*BT*0`` with ``Date Paid`` of
    ``0000-00-00``. Taken literally that is a reference, so every unpaid
    consignment across every DN pooled under one imaginary account sale and the
    reconciliation panel showed it as a single line owing thousands. Nothing was
    ever mis-paid off it, because no payment file contains ``PRE*BT*0`` to match
    against, but it is not a reference and is not stored as one.

    Recognised by having no statement number in it, so any other placeholder the
    export invents is caught the same way.
    """
    return ref if ref and acc_to_stmno(ref) else ""


def parse_daily_sales_csv(text: str, filename: str) -> list[StatementRow]:
    """Workbook rows from the Daily Sales CSV export.

    One row per **account sale**, not per consignment. See ``_statement_rows``.
    """
    rows = _rows(text)
    out: list[StatementRow] = []
    consignments = 0

    market = agent = None
    cur: dict | None = None

    def flush() -> None:
        nonlocal consignments
        if cur is None or not cur["dockets"]:
            return
        consignments += 1
        out.extend(_statement_rows(cur, filename, consignments))

    for row in rows:
        if not any(row):
            continue
        if _is_total(row):
            continue

        labels = _labelled(row)
        if "delivery id" in labels:
            flush()
            cur = {
                "market": market, "agent": agent,
                "delivery": _strip_id(labels.get("delivery id", "")),
                "supplier_ref": ref_to_dn(labels.get("supplier ref", "")),
                "producer": ref_producer(labels.get("supplier ref", "")),
                "qty_sent": _int(labels.get("qty sent", "")),
                "consignment": None, "product": None, "dockets": [],
            }
            continue
        if "consignment id" in labels and cur is not None:
            cur["consignment"] = _strip_id(labels["consignment id"])
            continue
        if "product" in labels and cur is not None:
            cur["product"] = _PRODUCT_TAIL.sub("", " ".join(labels["product"].split()))
            continue

        # A docket: Delivery Date, Date Sold, Date Paid, Docket, Payment Ref,
        # Qty Sold, Market Avg, Price, Sales Value.
        sold = _date(_cell(row, 1))
        if sold is not None and cur is not None:
            cur["dockets"].append(
                {
                    "delivered": _date(_cell(row, 0)),
                    "sold": sold,
                    "paid": _date(_cell(row, 2)),
                    "docket": _cell(row, 3),
                    "payment_ref": _cell(row, 4),
                    "qty": _int(_cell(row, 5)) or 0,
                    "value": _num(_cell(row, 8)) or 0.0,
                }
            )
            continue

        # A bare "MARKET","Agent (Pre)" band introduces the next consignment,
        # or Destination,"Subtropico (Jhb)" which names the agent only.
        if len(row) >= 2 and row[0] and row[1] and not labels and sold is None:
            if row[0].lower() == "destination":
                agent = _agent_name(row[1])
            elif row[0].isupper() and "(" in row[1]:
                market, agent = row[0], _agent_name(row[1])

    flush()
    return out


def _statement_rows(block: dict, filename: str, index: int) -> list[StatementRow]:
    """One row per account sale this consignment was settled under.

    A consignment does not sell in one go. It sits on the market floor and is
    sold off over days, and every few days the agent closes off an account sale
    covering whatever went in that run -- so a single consignment commonly spans
    two to five account sales (measured across June: 43 of 81 span one, the rest
    up to five). The workbook is kept the same way, one row per account sale,
    which is why a consignment cannot be one row: its stock position changes
    between them.

    Each docket names the account sale it was paid under, so the split is exact
    rather than inferred, and the rows are ordered by when they sold so **stock
    carries forward**: the first row opens at what was sent, and each later row
    opens at what the row before it left behind::

        sent 120 -> opening 120, sold 115, left 5
                    opening   5, sold   5, left 0

    Quantity Received stays the consignment's Qty Sent on every row, as the
    sheet keeps it. ``consignment_id`` marks which rows came from one delivery,
    so anything measured per consignment (what was sent, how long it took to
    clear) counts it once instead of once per row.
    """
    dockets = block["dockets"]
    groups: dict[str, list[dict]] = {}
    for d in dockets:
        groups.setdefault(_settled_under(d["payment_ref"]), []).append(d)

    def order(item: tuple[str, list[dict]]) -> tuple:
        ref, ds = item
        dates = [d["sold"] for d in ds if d["sold"]]
        # An unpaid group has no account sale yet, so it sorts last: it is the
        # stock still open, whatever the dates say.
        return (not ref, min(dates) if dates else date.max, acc_to_stmno(ref) or 0)

    ordered = sorted(groups.items(), key=order)
    delivered = sorted(d["delivered"] for d in dockets if d["delivered"])
    all_sold = sorted(d["sold"] for d in dockets if d["sold"])
    arrived = delivered[0] if delivered else (all_sold[0] if all_sold else None)

    out: list[StatementRow] = []
    opening = block["qty_sent"]
    for position, (ref, ds) in enumerate(ordered, start=1):
        name = f"{filename} · consignment {index}"
        if len(ordered) > 1:
            name += f" · statement {position} of {len(ordered)}"
        row = StatementRow(source_file=name)
        flags: list[Flag] = []

        row.market, row.market_agent = block["market"], block["agent"]
        row.supplier_ref = block["supplier_ref"]
        row.producer_code = block["producer"]
        row.delivery_id = block["delivery"]
        # Column A starts from the Supplier Ref, matching the payment documents,
        # with the Delivery ID as fallback. Zaco's real DN is often neither, and
        # is filled in from the captured mapping afterwards -- see ``delivery.py``.
        row.dn = block["supplier_ref"] or block["delivery"]
        row.consignment_id = block["consignment"]
        row.product = block["product"]
        row.qty_received = block["qty_sent"]

        # Column E is the account sale, which is what the payment report pays
        # against and what the workbook is keyed on.
        row.stm_no = acc_to_stmno(ref) if ref else None
        if row.stm_no is None:
            flags.append(
                Flag(
                    field="stm_no",
                    severity="warning",
                    code="unpaid",
                    message="Sold but not in a payment run yet, so it has no account sale number.",
                )
            )

        qty = sum(d["qty"] for d in ds)
        value = sum(d["value"] for d in ds)
        # A negative docket is a return -- fruit back on the floor, reversing a
        # sale already booked under this same account sale. It nets off, because
        # that is what the payment run paid and what the stock did, but the sale
        # and the return are both real and are kept as their own figures rather
        # than surviving only as a difference. See ``daily_sales.parse_daily_sales``.
        returns = [d for d in ds if d["qty"] < 0]
        row.cartons_sold = qty
        row.cartons_returned = -sum(d["qty"] for d in returns)
        row.returns_total = round(abs(sum(d["value"] for d in returns)), 2)
        row.sales_total = round(value, 2)
        row.price = round(value / qty, 2) if qty else 0.0
        row.opening_stock = opening
        if opening is not None:
            opening -= qty

        sold = sorted(d["sold"] for d in ds if d["sold"])
        paid = sorted(d["paid"] for d in ds if d["paid"])
        if sold:
            row.last_sale = sold[-1]
        # Column T is the account sale's own date -- the day the run was closed
        # off and paid -- not the day the fruit sold. Checked against the
        # historical sheet: 38 of 38 rows agree with the payment date, none with
        # the first sale date. Falls back to the first sale for an unpaid run,
        # which has no account sale date yet.
        row.invoice_date = paid[0] if paid else (sold[0] if sold else None)
        if row.invoice_date:
            row.status = row.invoice_date.strftime("%d.%m")
        # The real delivery date, at last: time on market can be measured from
        # arrival instead of from the first sale. It belongs to the delivery, so
        # every row of the consignment carries the same one.
        row.date_received = arrived
        # Column D dates the load leaving, a day before the market books it in.
        row.date_sent = arrived - timedelta(days=1) if arrived else None

        # This row's own share of the payment, as "ref=value" -- the exact join
        # to the payment report.
        row.payment_refs = f"{ref}={value:.2f}" if ref else None

        row.nett_total = None
        flags.append(
            Flag(
                field="nett_total",
                severity="warning",
                message="Nett comes from the payment report — reconcile to fill it in.",
            )
        )
        row.flags = flags
        out.append(row)

    return out


# --- payment details ------------------------------------------------------

# 203423,20026*14799,PRE*BT*382860,2026-06-01,329.19,61.57,9.24,400,
_ACCSALE = re.compile(r"^[A-Z]{2,}\*[A-Z0-9]+\*[A-Z0-9/]+$")


def parse_payment_details_csv(text: str, filename: str) -> list[dict]:
    """One record per account sale, with its commodity lines."""
    rows = _rows(text)
    out: list[dict] = []
    market = agent = None
    cur: dict | None = None

    for row in rows:
        if not any(row) or _is_total(row):
            continue

        # The filter line: Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)".
        # On a filtered export it names the only market and agent present, so it
        # is a usable default. On an unfiltered one it says ALL, which is not a
        # market -- taking it literally would label every row "ALL".
        if row and row[0].lower() == "market":
            found = _cell(row, 1)
            if not _is_all(found):
                market = found
            found_agent = _cell(row, 4)
            if not _is_all(found_agent):
                agent = _agent_name(found_agent)
            continue

        # Subtropico's exports name the agent alone: Destination,"Subtropico
        # (Jhb)". Missing this left every row on those weeks with no agent at
        # all, which is the field the whole workbook and every per-agent figure
        # keys on. The market is genuinely not in the file, so it stays unset.
        if row and row[0].lower() == "destination":
            found_agent = _cell(row, 1)
            if not _is_all(found_agent):
                agent = _agent_name(found_agent)
            continue

        # An agent band inside the data, as an unfiltered export carries:
        #   "JOBURG MKT - TFRESH","Subtropico (Jhb)"
        if (len(row) >= 2 and row[0] and row[1] and not _ACCSALE.match(_cell(row, 2))
                and row[0].isupper() and "(" in row[1] and not _date(_cell(row, 3))):
            market = row[0]
            agent = re.sub(r"\s*\([^)]*\)\s*$", "", row[1]).strip()
            continue

        # Account-sale header: the third cell is the AccSale number.
        if _ACCSALE.match(_cell(row, 2)) and _date(_cell(row, 3)):
            cur = {
                "market_agent": agent,
                "market": market,
                "supplier_ref": _cell(row, 1),
                "dn": ref_to_dn(_cell(row, 1)),
                "accsale": _cell(row, 2),
                "stm_no": acc_to_stmno(_cell(row, 2)),
                "date": _cell(row, 3),
                "nett": _num(_cell(row, 4)) or 0.0,
                "deductions": _num(_cell(row, 5)) or 0.0,
                "vat": _num(_cell(row, 6)) or 0.0,
                "gross": _num(_cell(row, 7)) or 0.0,
                "lines": [],
            }
            out.append(cur)
            continue

        # Commodity line: ,,,01,20026*14799,"CHERRIES ...",14,2,400
        if cur is not None and _cell(row, 3).isdigit() and _cell(row, 5):
            cur["lines"].append(
                {
                    "product": " ".join(_cell(row, 5).split()),
                    "delivered": _int(_cell(row, 6)) or 0,
                    "sold": _int(_cell(row, 7)) or 0,
                    "sales_total": _num(_cell(row, 8)) or 0.0,
                }
            )
    return out


def date_range(text: str) -> tuple[str | None, str | None]:
    """Earliest and latest date anywhere in the export."""
    found = sorted({d for row in _rows(text) for c in row if (d := _date(c))})
    return (found[0].isoformat(), found[-1].isoformat()) if found else (None, None)
