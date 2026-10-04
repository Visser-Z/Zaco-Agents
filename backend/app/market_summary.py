"""The market's own word on what it has paid, and every outstanding line checked
against it.

The FMS "Summary of Deliveries By Agent" report prints, for each delivery and
product, what the market sold and what it has paid and not paid. It is the one
report that states the market's position outright, and every wrong "owed" line
found by hand in September 2026 was found by holding the app's figures against
it: a payment printed with no lines, a return the market had already paid for,
a payment deleted from the book. This module does that comparison for every
line, every time Tracking is opened.

The report filters by the date a delivery was SENT. A delivery sent in July and
still selling in September is in July's summary, not September's, so a line no
summary covers is said to be unchecked, with the month whose summary would
settle it, never assumed to be either paid or owed.

Nothing here changes what the app says is owed. The matcher decides that from
the payments; this only says whether the market agrees, and if it does not,
what to fetch to find out why.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from . import analytics, reconcile, tracking

TITLE = "Summary of Deliveries By Agent"

_RUN = re.compile(r"Run Date:\s*(\d{4})/(\d\d)/(\d\d)\s+(\d\d:\d\d:\d\d)")
_RANGE = re.compile(r"Date Range:\s*(\d{4})/(\d\d)/(\d\d)\s*-\s*(\d{4})/(\d\d)/(\d\d)")
# 1855491Z 20026*N 2026-08-27 480 360 360 0 R 133190.00 R 133190.00 R 0.00 R 369.97
# Qty Amend To is printed only where the market changed what it booked, and
# Qty Avail goes negative when more sold than was booked (2398482Z: -10).
_ROW = re.compile(
    r"^(\d{6,8})Z\s+\d+\*(\S+)\s+(\d{4}-\d\d-\d\d)\s+(-?\d+)\s+(?:(-?\d+)\s+)?(-?\d+)\s+(-?\d+)"
    r"\s+R\s*(-?[\d.]+)\s+R\s*(-?[\d.]+)\s+R\s*(-?[\d.]+)")
_TOTAL = re.compile(r"^-?\d+\s+-?\d+\s+-?\d+\s+-?\d+\s+R")
_HEADER = "Delivery ID Supplier Ref"


def is_delivery_summary(text: str) -> bool:
    return TITLE in (text or "")


def run_at(text: str) -> str | None:
    """When the market ran the report, which is the moment its figures are of."""
    m = _RUN.search(text or "")
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}" if m else None


def period(text: str) -> tuple[str | None, str | None]:
    m = _RANGE.search(text or "")
    return (f"{m[1]}-{m[2]}-{m[3]}", f"{m[4]}-{m[5]}-{m[6]}") if m else (None, None)


def _num(token: str) -> float:
    try:
        return float(token)
    except ValueError:
        return 0.0


def parse(text: str, filename: str = "") -> list[dict]:
    """One record per delivery and product.

    A delivery can carry the same product on two lines (2351306Z held two
    lots of white grapes); the report gives no consignment number to tell them
    apart, so they are summed, and compared with the app at the same grain.
    """
    when = run_at(text)
    lo, hi = period(text)
    # The app's PDF reader keeps the report's column spacing and blank lines;
    # collapsed, both readers give the same lines.
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").splitlines()]
    # A page break can fall between a product and its column headings, so
    # the page footers go before anything looks ahead.
    lines = [l for l in lines if l and not re.match(r"^Page \d+/\d+$", l)]
    agent = product = None
    out: dict[tuple, dict] = {}
    for i, line in enumerate(lines):
        if not line:
            continue
        if line.startswith(_HEADER):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.startswith(_HEADER):
            product = line
            continue
        m = _ROW.match(line)
        if m:
            if product is None:
                continue
            key = (int(m[1]), reconcile.normalise_product(product))
            rec = out.setdefault(key, {
                "delivery_id": key[0], "product": key[1], "product_name": product,
                "agent": agent, "supplier_ref": m[2], "date_sent": m[3],
                "qty_sent": 0, "sold": 0, "gross": 0.0, "paid": 0.0, "unpaid": 0.0,
                "run_at": when, "period_from": lo, "period_to": hi, "source_file": filename})
            rec["qty_sent"] += int(m[5] if m[5] is not None else m[4])
            rec["sold"] += int(m[6])
            rec["gross"] = round(rec["gross"] + _num(m[8]), 2)
            rec["paid"] = round(rec["paid"] + _num(m[9]), 2)
            rec["unpaid"] = round(rec["unpaid"] + _num(m[10]), 2)
            continue
        if (_TOTAL.match(line) or line.startswith(("Page ", "Report", "Delivery Reports",
                                                   "Market:", "Agent:", "Product:", "Date Range"))):
            continue
        # Anything else between blocks is the agent the next products are under.
        agent = line
    return list(out.values())


# --- the check -------------------------------------------------------------

VERDICTS = {
    "agrees": "Market agrees",
    "market_paid": "Market says paid",
    "owes_more": "Market says more is owed",
    "unchecked": "Not checked yet",
}


def _key_of(row: dict) -> tuple | None:
    delivery = reconcile.delivery_of(row.get("consignment_id"))
    if delivery is None:
        return None
    return (delivery, reconcile.normalise_product(row.get("product")))


def _month_name(month: str) -> str:
    return date.fromisoformat(month + "-01").strftime("%B %Y")


def check(sales: list[dict], payments: list[dict], summaries: list[dict],
          outstanding: list[dict]) -> dict:
    """Hold each delivery the market has summarised against the app, as of the
    moment the market ran the report, and say for every outstanding line what
    the market thinks of it.

    Compared as of the run: sales the book holds from after the market printed
    its figures cannot be in them, so they are left out of both sides.
    """
    filled = tracking.valued(sales)
    owed_by_row, *_ = tracking._allocate(filled, payments)

    rows_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in filled:
        if (k := _key_of(r)):
            rows_by_key[k].append(r)

    # The latest summary of each delivery and product wins.
    latest: dict[tuple, dict] = {}
    for s in summaries:
        k = (int(s["delivery_id"]), s["product"])
        if k not in latest or (s.get("run_at") or "") > (latest[k].get("run_at") or ""):
            latest[k] = s

    verdicts: dict[tuple, dict] = {}
    for k, s in latest.items():
        as_of = (s.get("run_at") or "")[:10] or None
        unpaid = round(float(s.get("unpaid") or 0), 2)
        gross = round(float(s.get("gross") or 0), 2)
        # The report was printed partway through its run day, so that day's
        # sales may or may not be in it. Both readings are tried, and the one
        # the market's own figures bear out is used.
        every = rows_by_key.get(k, [])
        readings = []
        for upto in ((lambda d: d <= as_of), (lambda d: d < as_of)) if as_of else ((lambda d: True),):
            rows = [r for r in every if upto(analytics.selling_day(r) or "")]
            readings.append((rows,
                             round(sum(reconcile._num(r.get("sales_total")) for r in rows), 2),
                             round(sum(owed_by_row[id(r)] for r in rows), 2)))
        rows, book_sold, app_owed = next(
            (rd for rd in readings if abs(rd[1] - gross) < 0.02 and abs(rd[2] - unpaid) < 0.02),
            next((rd for rd in readings if abs(rd[1] - gross) < 0.02), readings[0]))
        v = {"as_of": as_of, "market_unpaid": unpaid, "market_sold": gross,
             "market_paid": round(float(s.get("paid") or 0), 2),
             "app_owed": app_owed, "book_sold": book_sold,
             "delivery_id": k[0], "product": s.get("product_name") or k[1],
             "supplier_ref": s.get("supplier_ref"), "date_sent": s.get("date_sent")}
        if abs(app_owed - unpaid) < 0.02:
            v["verdict"] = "agrees"
        elif app_owed > unpaid:
            v["verdict"] = "market_paid"
            v["difference"] = round(app_owed - unpaid, 2)
            # The payment the book is missing was made after the oldest sale it
            # would have paid, and no later than the market's run. Not after
            # the last payment the book holds: PRE*BT*397227 was paid on 1
            # September and the book held one for that delivery from the 4th.
            owing = [d for r in rows if owed_by_row[id(r)] > 0.005
                     and (d := analytics.selling_day(r))]
            v["fetch_from"] = min(owing) if owing else None
            v["fetch_to"] = as_of
        else:
            v["verdict"] = "owes_more"
            v["difference"] = round(unpaid - app_owed, 2)
        # Where the book and the market disagree on what was SOLD, the sales
        # side is the gap, whatever the payments say.
        if abs(gross - book_sold) >= 0.02:
            v["sales_gap"] = round(gross - book_sold, 2)
        verdicts[k] = v

    # Every outstanding line gets the verdict for its delivery and product.
    first_sent: dict[tuple, str] = {}
    for k, rows in rows_by_key.items():
        days = [d for r in rows if (d := r.get("date_received") or analytics.selling_day(r))]
        if days:
            first_sent[k] = min(days)
    unchecked_months: dict[str, float] = defaultdict(float)
    # Deliveries the market has summarised at all, whatever product.
    summarised = {k[0] for k in latest}
    not_listed = 0.0
    counts = {name: 0 for name in VERDICTS}
    values = {name: 0.0 for name in VERDICTS}
    for line in outstanding:
        cid = line.get("consignment_id")
        delivery = reconcile.delivery_of(cid) if cid else None
        k = (delivery, reconcile.normalise_product(line.get("product"))) if delivery else None
        v = verdicts.get(k) if k else None
        if v is None:
            sent = first_sent.get(k) if k else None
            month = sent[:7] if sent else None
            if delivery in summarised:
                # The market summarised this delivery and left this product
                # off: 1185124Z listed its Sweet Celebration and not its Grapes
                # Class 2. Another month's summary will not add it.
                line["market_view"] = {"verdict": "unchecked", "reason": "not_listed"}
                not_listed += line.get("owed", 0.0)
            else:
                line["market_view"] = {"verdict": "unchecked", "reason": "no_summary",
                                  "month": month,
                                  "month_name": _month_name(month) if month else None}
                if month:
                    unchecked_months[month] += line.get("owed", 0.0)
        else:
            line["market_view"] = {kk: v[kk] for kk in (
                "verdict", "as_of", "market_unpaid", "app_owed", "difference",
                "fetch_from", "fetch_to", "sales_gap") if kk in v}
        name = line["market_view"]["verdict"]
        counts[name] += 1
        values[name] += line.get("owed", 0.0)

    disagree = [v for v in verdicts.values() if v["verdict"] != "agrees" or v.get("sales_gap")]
    disagree.sort(key=lambda v: -abs(v.get("difference") or v.get("sales_gap") or 0))
    return {
        "summaries": len(latest),
        "as_of": max((v["as_of"] for v in verdicts.values() if v["as_of"]), default=None),
        "agree": sum(1 for v in verdicts.values() if v["verdict"] == "agrees"
                     and not v.get("sales_gap")),
        "checked": len(verdicts),
        "lines": counts,
        "values": {k: round(v, 2) for k, v in values.items()},
        "disagree": disagree[:40],
        # Lines whose delivery the market summarised without this product.
        "not_listed": round(not_listed, 2),
        # The summaries to fetch, one per month of the deliveries still unchecked.
        "unchecked_months": [{"month": m, "month_name": _month_name(m), "owed": round(v, 2)}
                             for m, v in sorted(unchecked_months.items())],
    }
