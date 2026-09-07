"""The Tracking view: what is paid, what is still owed, and what is not moving.

Three questions the owner asks, answered from saved data alone so they hold up
across the whole period rather than only in the moment a file is dropped:

  * Payments  -- of everything sold, what has the agent paid for, and how much
                 is still to come.
  * Sales     -- how much sold each day, per product.
  * Slow      -- what is taking too long to clear off the floor.

Pure functions over plain row dicts (sales) and payment-record dicts, the same
shapes ``analytics`` and ``reconcile`` already use, so this unit-tests without a
database. Every figure that belongs to a delivery is counted once per
consignment, never once per row -- the rule the rest of the app lives by.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import analytics, integrity, reconcile

# --- payments -------------------------------------------------------------

def item_ref(row: dict) -> str:
    """A stable name for one Tracking line, so it can be closed and stay closed.

    The lists are recomputed from the history on every load and carry no id of
    their own, so the name has to come from what the line *is*: the account
    sale where the match knew one, otherwise the delivery note and the
    commodity, normalised the same way the matcher normalises it.
    """
    if row.get("reference"):
        return f"ref:{row['reference']}"
    return f"{row.get('dn')}:{reconcile.normalise_product(row.get('product'))}"


# A closed item is named by its kind AND its ref, exactly as the dismissals
# table keys it. The same consignment can appear on both lists, so without the
# kind, closing the slow line would close the outstanding one with it.
def closed_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


def payment_status(sales: list[dict], payments: list[dict],
                   closed: set[str] | frozenset[str] = frozenset(),
                   lo: str | None = None, hi: str | None = None) -> dict:
    """What has been paid, and what is still owed.

    Reconciliation is the same match the payment panel does -- on the account
    sale each docket names where the export gives it, otherwise on supplier ref
    plus product -- run over ALL saved sales and ALL saved payments, so the
    answer is the whole period's position rather than one file's.

    "Still to come" is the sold value not yet covered by a payment: an unpaid
    group in full, and the shortfall on a group only partly paid. A group paid
    for more than sold contributes nothing to it (that is the agent's side to
    explain, surfaced separately as an over-payment).
    """
    # Reconcile matches on each row's sold VALUE. The history read does not carry
    # the exact docket total, so fill it with the gross the app trusts
    # everywhere else -- cartons sold times price -- which differs only by
    # rounding and is the figure being compared against on the payment side.
    sales = [{**r, "sales_total": r.get("sales_total")
              if r.get("sales_total") is not None else analytics.row_value(r)}
             for r in sales]

    # Row by row, never one strategy for the whole history: a single row
    # carrying a payment reference used to send every PDF-sourced row down the
    # reference path, where it has nothing to match on and reports as unsold.
    rows = reconcile.reconcile_any(sales, payments)

    # reconcile's statuses, in this view's terms:
    #   matched      sold and paid agree -- paid in full.
    #   unpaid       a payment run has not reached this sale yet -- all owed.
    #   over         sold MORE than the payment covered -- the shortfall is owed.
    #   outstanding  the agent paid for MORE than sold -- nothing owed, and worth
    #                a glance (a prepayment, or a sale not captured).
    #   no_sales     paid, with nothing sold to match -- an exception to chase.
    # What the agent actually paid in this window, read straight off the
    # payments rather than off the match: scoping the match to a period would
    # make an August sale settled in September look unpaid, which is the
    # opposite of true. Unscoped, this is every payment on record.
    in_window = [
        p for p in payments
        if not ((lo and str(p.get("date") or "")[:10] < lo)
                or (hi and str(p.get("date") or "")[:10] > hi))
    ] if (lo or hi) else payments
    paid_in_window = round(sum(float(p.get("nett") or 0) for p in in_window), 2)

    still_to_come = 0.0
    matched = outstanding = 0
    outstanding_rows: list[dict] = []
    overpaid_rows: list[dict] = []
    closed_rows: list[dict] = []
    for r in rows:
        sold, gross, nett, status = (
            r["daily_total"], r["payment_gross"], r["payment_nett"], r["status"])
        if status in ("unpaid", "over"):
            owed = sold if status == "unpaid" else round(sold - gross, 2)
            if owed > 0:
                row = {**r, "owed": round(owed, 2), "ref": item_ref(r)}
                # A closed line stays visible on its own list with its value,
                # so closing can never quietly shrink the exposure.
                if closed_key("owed", row["ref"]) in closed:
                    closed_rows.append(row)
                else:
                    outstanding += 1
                    still_to_come += owed
                    outstanding_rows.append(row)
        elif status == "matched":
            matched += 1
        elif status == "outstanding":
            overpaid_rows.append({**r, "overpaid": round(gross - sold, 2)})

    # Payments the sales side cannot account for. Never dropped -- either a sale
    # not imported yet, or a mismatch to chase.
    unmatched = [r for r in rows if r["status"] == "no_sales"]
    unattributed = reconcile.unattributed(payments)

    outstanding_rows.sort(key=lambda r: r["owed"], reverse=True)
    # How old the oldest unpaid sale is. The reference match carries its own
    # date; the PDF match (dn + product) does not, so there it comes from the
    # sold rows themselves -- the earliest day anything still owed was sold.
    own = [d for r in outstanding_rows if (d := analytics._parse_date(r.get("date")))]
    owed_keys = {(r.get("dn"), reconcile.normalise_product(r.get("product")))
                 for r in outstanding_rows}
    from_sales = [
        d for row in sales
        if (row.get("dn"), reconcile.normalise_product(row.get("product"))) in owed_keys
        and (d := analytics._parse_date(row.get("group_date"))) is not None
    ]
    dates = own or from_sales
    oldest = min(dates).isoformat() if dates else None

    return {
        "total_paid": paid_in_window,
        "payments_in_window": len(in_window),
        "still_to_come": round(still_to_come, 2),
        "batches_paid": matched,
        "batches_outstanding": outstanding,
        "oldest_outstanding": oldest,
        # Not truncated: this is the list the operator prints and works down.
        "outstanding": outstanding_rows,
        "overpaid": sorted(overpaid_rows, key=lambda r: r["overpaid"], reverse=True)[:20],
        "unmatched": [
            {
                "dn": r.get("dn"),
                "product": r.get("product"),
                "paid": r.get("payment_gross") or r.get("payment_nett") or 0.0,
                "reason": "paid, nothing sold matches",
            }
            for r in unmatched
        ],
        "unattributed": unattributed,
        "payments_recorded": len(payments),
        # Closed lines and what they were worth, reported rather than dropped.
        "closed": sorted(closed_rows, key=lambda r: r["owed"], reverse=True),
        "closed_value": round(sum(r["owed"] for r in closed_rows), 2),
    }


# --- sales per day, per product ------------------------------------------

def report_name(row: dict) -> str:
    """The file a row was read from, without the part identifying the row.

    A statement's source is recorded per block -- "the-export.pdf · consignment 4",
    "· product 2 of 3" -- which names the row rather than the report. Grouping on
    that gives one source per row and answers nothing, so the suffix comes off.
    """
    name = str(row.get("source_file") or "").split(" · ")[0].strip()
    return name or "Not recorded"


def sales_by_day(sales: list[dict], start: str | None = None,
                 end: str | None = None) -> dict:
    """How much sold each trading day, broken down by product.

    Net of returns, as every carton figure in the app is: a day that sold ten
    and had two come back moved eight. Days come back newest first, which is the
    order the owner reads them in.
    """
    by_day: dict[str, dict] = {}
    for row in sales:
        day = selling_day(row)
        if not day:
            continue
        # ISO dates compare correctly as strings, so no parsing is needed.
        if (start and day < start) or (end and day > end):
            continue
        product = analytics.product_label(row)
        d = by_day.setdefault(day, {"date": day, "cartons": 0.0, "returned": 0.0,
                                    "value": 0.0, "sources": defaultdict(
                                        lambda: {"rows": 0, "cartons": 0.0}),
                                    "products": defaultdict(
                                        lambda: {"cartons": 0.0, "value": 0.0, "returned": 0.0,
                                                 "agents": set(), "market_avg": [], "weight": 0.0})})
        d["cartons"] += analytics.row_cartons(row)
        d["returned"] += analytics.row_returned(row)
        d["value"] += analytics.row_value(row)
        # Which report this row was read from. A day that turns up where no
        # trading happened is answered by naming the file it came out of, rather
        # than by trusting or doubting the date on its own.
        src = d["sources"][report_name(row)]
        src["rows"] += 1
        src["cartons"] += analytics.row_cartons(row)
        p = d["products"][product]
        cartons = analytics.row_cartons(row)
        p["cartons"] += cartons
        p["value"] += analytics.row_value(row)
        p["returned"] += analytics.row_returned(row)
        if row.get("market_agent"):
            p["agents"].add(row["market_agent"])
        # The market's own average for this commodity that day, weighted by
        # cartons so a one-carton line cannot outvote a hundred-carton one.
        if (avg := integrity.market_avg(row)) is not None and cartons > 0:
            p["market_avg"].append(avg * cartons)
            p["weight"] += cartons

    days = []
    for day in sorted(by_day, reverse=True):
        d = by_day[day]
        products = sorted(
            ({
                "product": name,
                "cartons": round(v["cartons"], 2),
                "returned": round(v["returned"], 2),
                "value": round(v["value"], 2),
                # What a carton actually fetched that day.
                "price": round(v["value"] / v["cartons"], 2) if v["cartons"] else 0.0,
                # And what the market was paying for it, where the report says.
                "market_avg": round(sum(v["market_avg"]) / v["weight"], 2) if v["weight"] else None,
                "agents": sorted(v["agents"]),
             } for name, v in d["products"].items()),
            key=lambda x: x["value"], reverse=True,
        )
        days.append({
            "date": day,
            "cartons": round(d["cartons"], 2),
            "returned": round(d["returned"], 2),
            "value": round(d["value"], 2),
            "products": products,
            "sources": sorted(
                ({"file": name, "rows": v["rows"], "cartons": round(v["cartons"], 2)}
                 for name, v in d["sources"].items()),
                key=lambda s: (-s["rows"], s["file"]),
            ),
        })
    return {"days": days}


# --- slow to sell ---------------------------------------------------------

def selling_day(row: dict) -> str | None:
    """The day a row actually sold on.

    ``group_date`` is the consignment's date -- the earliest across its
    delivery-note group, i.e. the day the load was sent -- so several selling
    days share one value. Dropping a week of reports at once collapsed 46 of 60
    rows onto a single day and reported R192 965 as one day's trade. The row
    carries its own sale date in ``last_sale``; use it, and keep ``group_date``
    as the fallback for history recorded before it was captured.
    """
    day = row.get("last_sale") or row.get("group_date")
    return str(day)[:10] if day else None


def _clearance_days(sales: list[dict], today: date) -> list[int]:
    """Days each cleared consignment took, arrival to its last sale.

    One figure per consignment, not per row. This is the sample the thresholds
    are read from, so it must be what actually happened, not what is still open.
    """
    spans: list[int] = []
    for group in analytics.group_consignments(sales):
        d = analytics.consignment_days_to_sell(group)
        if d is not None:
            spans.append(d)
    return spans


def slow_bands(sales: list[dict], today: date | None = None) -> dict:
    """Where "too long to sell" begins, read from the history itself.

    Most produce clears fast, so the median says little; it is the slow tail
    that matters. The Watch line is the 75th percentile of how long things have
    actually taken (a quarter of everything took at least this long), Slow the
    90th, Dead the 95th, each floored so a run of quick months cannot set the
    bar at two days. With too little history to have a tail, it falls back to
    the plain 5 / 10 / 15 trading days from the brief.
    """
    spans = sorted(_clearance_days(sales, today or date.today()))
    if len(spans) < 8:
        return {"watch": 5, "slow": 10, "dead": 15, "from": "default (too little history)"}

    def pct(p: float) -> int:
        return spans[min(len(spans) - 1, int(len(spans) * p))]

    watch = max(pct(0.75), 4)
    slow = max(pct(0.90), watch + 3)
    dead = max(pct(0.95), slow + 3)
    return {"watch": watch, "slow": slow, "dead": dead,
            "from": f"{len(spans)} cleared consignments"}


def slow_stock(sales: list[dict], today: date | None = None,
               closed: set[str] | frozenset[str] = frozenset(),
               lo: str | None = None, hi: str | None = None) -> dict:
    """Consignments still on the floor, and how long they have been there.

    Aging is measured from the delivery date to today, over the cartons that
    have NOT sold -- counted once per consignment, so a delivery settled across
    several days is one ageing position, not several. Produce that has fully
    cleared is not here; this is only what is still sitting.
    """
    today = today or date.today()
    bands = slow_bands(sales, today)
    out: list[dict] = []
    for group in analytics.group_consignments(sales):
        sent = analytics.cartons_sent(group)
        sold = sum(analytics.row_cartons(r) for r in group)
        left = sent - sold
        if sent <= 0 or left <= 0:
            continue
        arrived = min((d for r in group if (d := analytics._parse_date(r.get("date_received")))), default=None)
        if arrived is None:
            continue
        days = (today - arrived).days
        if days < bands["watch"]:
            continue
        tier = "dead" if days >= bands["dead"] else "slow" if days >= bands["slow"] else "watch"
        first = group[0]
        last_moved = max((d for r in group if (d := analytics._parse_date(r.get("last_sale")))), default=None)
        out.append({
            "ref": item_ref({"dn": first.get("dn"),
                             "product": analytics.product_label(first)}),
            "product": analytics.product_label(first),
            "dn": first.get("dn"),
            "market_agent": first.get("market_agent"),
            "cartons_left": int(left),
            "cartons_sent": int(sent),
            "days_on_floor": days,
            "arrived": arrived.isoformat(),
            "last_moved": last_moved.isoformat() if last_moved else None,
            "tier": tier,
        })
    order = {"dead": 0, "slow": 1, "watch": 2}
    out.sort(key=lambda r: (order[r["tier"]], -r["days_on_floor"], -r["cartons_left"]))
    # Scoped by when the stock arrived, which is what "August's slow stock"
    # means. The bands themselves still come from the whole history: a
    # threshold read off one month would move every time the month changed.
    if lo or hi:
        out = [r for r in out
               if not (lo and r["arrived"] < lo) and not (hi and r["arrived"] > hi)]
    shut = [r for r in out if closed_key("slow", r["ref"]) in closed]
    out = [r for r in out if closed_key("slow", r["ref"]) not in closed]
    counts = {t: sum(1 for r in out if r["tier"] == t) for t in ("watch", "slow", "dead")}
    return {"bands": bands, "counts": counts, "items": out[:50], "flagged": len(out),
            "closed": shut, "closed_count": len(shut)}


# --- the whole payload ----------------------------------------------------

def date_span(sales: list[dict]) -> dict:
    """The first and last day anything sold, so the pickers can bound themselves."""
    days = sorted({d for r in sales if (d := selling_day(r))})
    return {"first": days[0] if days else None, "last": days[-1] if days else None}


def available_periods(sales: list[dict]) -> dict:
    """The months and weeks that actually have sales, for the period picker.

    Built from the selling day, not from ``analytics.row_date``: that one reads
    the consignment's send date first, so the picker would offer months the
    trading never happened in and miss the ones it did.
    """
    months: set[str] = set()
    weeks: set[str] = set()
    by_month: dict[str, set[str]] = defaultdict(set)
    for row in sales:
        day = selling_day(row)
        if not day:
            continue
        d = analytics._parse_date(day)
        if d is None:
            continue
        iso = d.isocalendar()
        m, w = d.strftime("%Y-%m"), f"{iso[0]}-W{iso[1]:02d}"
        months.add(m)
        weeks.add(w)
        by_month[m].add(w)
    return {"months": sorted(months), "weeks": sorted(weeks),
            "weeks_by_month": {m: sorted(ws) for m, ws in by_month.items()}}


def in_period(sales: list[dict], lo: str | None, hi: str | None) -> list[dict]:
    """Rows that sold inside the window. Undated rows cannot be placed, so they
    drop out of a scoped view and stay in the all-time one."""
    if not lo and not hi:
        return list(sales)
    out = []
    for r in sales:
        day = selling_day(r)
        if day is None or (lo and day < lo) or (hi and day > hi):
            continue
        out.append(r)
    return out


def compute(sales: list[dict], payments: list[dict], today: date | None = None,
            start: str | None = None, end: str | None = None,
            closed: set[str] | frozenset[str] = frozenset(),
            month: str | None = None, week: str | None = None) -> dict:
    """Everything the Tracking tab renders.

    ``start`` and ``end`` narrow the per-day sales list only. What is owed is a
    running position rather than a period figure -- money owed from March is
    still owed in August -- so filtering it to a date window would quietly
    understate the exposure. Slow stock is likewise about what is sitting on the
    floor right now.
    """
    lo, hi = analytics.period_bounds(month, week)
    # The explicit day pickers are finer than a period, so they win where both
    # are set; the period fills them in otherwise.
    d_start, d_end = (start or lo), (end or hi)
    # Owed money is scoped by when it was SOLD, against every payment ever
    # recorded -- an August sale settled in September is paid, not outstanding.
    scoped = in_period(sales, lo, hi)
    status = payment_status(scoped, payments, closed, lo, hi)
    # Owed is a running position, not a period figure: narrowing the sales
    # changes what each group is matched against, so the periods do not add up
    # to the whole. Carry the all-time figure alongside rather than let a
    # scoped view quietly read as the total exposure.
    if lo or hi:
        status["still_to_come_all_time"] = payment_status(
            sales, payments, closed)["still_to_come"]
    return {
        "payments": status,
        "sales_by_day": sales_by_day(sales, d_start, d_end),
        "slow_stock": slow_stock(sales, today, closed, lo, hi),
        "span": date_span(sales),
        "periods": available_periods(sales),
        "filter": {"from": d_start, "to": d_end, "month": month, "week": week},
    }
