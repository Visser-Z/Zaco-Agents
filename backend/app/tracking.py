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


def settle(sales: list[dict], payments: list[dict]) -> tuple[dict, dict, dict]:
    """Work out what each individual sale still has owing on it.

    A payment is for a consignment, not for a month. One delivery sells down
    over weeks and the agent pays as it goes, so a payment settles the sales
    that had already happened when it was made. Each consignment's payments are
    therefore applied to that consignment's own sales oldest first, and what a
    sale still has owing is its own value less the share that reached it.

    That is what lets a month answer for itself. Comparing a month's sales
    against a consignment's entire payment history made a July payment cancel a
    September sale of the same delivery: September read as over-paid while the
    delivery was, across the whole book, tens of thousands short. Settled this
    way the months add up to the all-time figure instead of fighting it.

    A sale that names its payment reference is settled here the same way as any
    other, because that payment is already in its consignment's pool.

    Returns (owed per sale by id, leftover credit per consignment, a label for
    each consignment) -- keyed the way ``reconcile`` matches, so a consignment
    means the same thing here as it does on the payment panel.
    """
    pool = {k: v["gross"] for k, v in reconcile.aggregate_payment(payments).items()}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    label: dict[tuple, dict] = {}
    for row in sales:
        key = reconcile._key(row.get("dn"), row.get("product"))
        groups[key].append(row)
        label.setdefault(key, {"dn": row.get("dn"), "product": row.get("product")})

    owed: dict[int, float] = {}
    for key, rows in groups.items():
        left = pool.get(key, 0.0)
        # Oldest first. A sale with no date cannot be placed in the order, so it
        # settles last rather than taking credit from a sale known to be older.
        for row in sorted(rows, key=lambda r: (selling_day(r) is None, selling_day(r) or "")):
            value = reconcile._num(row.get("sales_total"))
            take = min(left, value) if value > 0 and left > 0 else 0.0
            left -= take
            owed[id(row)] = round(value - take, 2)
        pool[key] = left

    credit = {k: round(v, 2) for k, v in pool.items() if v > 0.01}
    for key, rec in reconcile.aggregate_payment(payments).items():
        label.setdefault(key, {"dn": key[0], "product": key[1]})
    return owed, credit, label


def payment_status(sales: list[dict], payments: list[dict],
                   closed: set[str] | frozenset[str] = frozenset(),
                   lo: str | None = None, hi: str | None = None) -> dict:
    """What has been paid, and what is still owed.

    Every sale on the book is settled against its consignment's payments (see
    ``settle``); the window then decides which sales are reported, never how
    they were settled. So a month shows what is still owed on that month's own
    sales, and the months sum to the whole.
    """
    # Reconcile matches on each row's sold VALUE. The history read does not carry
    # the exact docket total, so fill it with the gross the app trusts
    # everywhere else -- cartons sold times price -- which differs only by
    # rounding and is the figure being compared against on the payment side.
    sales = [{**r, "sales_total": r.get("sales_total")
              if r.get("sales_total") is not None else analytics.row_value(r)}
             for r in sales]

    owed_by_row, credit, label = settle(sales, payments)
    paid = reconcile.aggregate_payment(payments)

    # What the agent actually paid in this window, read straight off the
    # payments rather than off the match: a payment settles sales that happened
    # earlier, so this is money received, not money accounted for.
    in_window = [
        p for p in payments
        if not ((lo and str(p.get("date") or "")[:10] < lo)
                or (hi and str(p.get("date") or "")[:10] > hi))
    ] if (lo or hi) else payments
    paid_in_window = round(sum(float(p.get("nett") or 0) for p in in_window), 2)

    # The sales this view reports, grouped the way payments match them.
    scoped: dict[tuple, list[dict]] = defaultdict(list)
    for row in sales:
        day = selling_day(row)
        if (lo or hi) and day is None:
            continue
        if (lo and day < lo) or (hi and day > hi):
            continue
        scoped[reconcile._key(row.get("dn"), row.get("product"))].append(row)

    still_to_come = 0.0
    matched = outstanding = 0
    outstanding_rows: list[dict] = []
    closed_rows: list[dict] = []
    for key, rows in scoped.items():
        owed = round(sum(owed_by_row[id(r)] for r in rows), 2)
        days = [d for r in rows if (d := selling_day(r))]
        entry = {
            "dn": label[key]["dn"],
            "product": label[key]["product"],
            "daily_total": round(sum(reconcile._num(r.get("sales_total")) for r in rows), 2),
            "payment_gross": round(paid.get(key, {}).get("gross", 0.0), 2),
            "payment_nett": round(paid.get(key, {}).get("nett", 0.0), 2),
            "date": min(days) if days else None,
        }
        entry["ref"] = item_ref(entry)
        if owed <= 0:
            matched += 1
            continue
        entry["owed"] = owed
        entry["status"] = "over" if entry["payment_gross"] else "unpaid"
        # A closed line stays visible on its own list with its value, so closing
        # can never quietly shrink the exposure.
        if closed_key("owed", entry["ref"]) in closed:
            closed_rows.append(entry)
        else:
            outstanding += 1
            still_to_come += owed
            outstanding_rows.append(entry)

    # Paid for more than the whole book ever sold on that consignment. A
    # position across all of it, not a property of any one month, so it is
    # reported whichever window is open.
    overpaid_rows = [
        {"dn": label[k]["dn"], "product": label[k]["product"], "overpaid": v}
        for k, v in credit.items() if k in label
    ]

    # Payments the sales side cannot account for. Never dropped -- either a sale
    # not imported yet, or a mismatch to chase.
    sold_keys = {reconcile._key(r.get("dn"), r.get("product")) for r in sales}
    unmatched = [
        {
            "dn": label[k]["dn"],
            "product": label[k]["product"],
            "paid": round(v["gross"], 2),
            "reason": "paid, nothing sold matches",
        }
        for k, v in paid.items() if k not in sold_keys
    ]
    unattributed = reconcile.unattributed(payments)

    outstanding_rows.sort(key=lambda r: r["owed"], reverse=True)
    dates = [r["date"] for r in outstanding_rows if r["date"]]
    oldest = min(dates) if dates else None

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
        "unmatched": unmatched,
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
                                    "dated": defaultdict(int),
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
        if (basis := dated_by(row)):
            d["dated"][basis] += 1
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
            # How many rows here were dated by a real sale date, and how many
            # only by the day their load was sent.
            "dated": {"sold": d["dated"].get("sold", 0),
                      "delivered": d["dated"].get("delivered", 0)},
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


def dated_by(row: dict) -> str | None:
    """Which field gave this row its selling day.

    ``last_sale`` is the day the report says it sold. ``group_date`` is the
    consignment's date -- the day the load was sent -- and standing in for a
    sale date it is an inference, not a reading. A day built on it can look
    exactly like a day built on real sale dates, which is how a day with no
    trading behind it becomes impossible to argue with.
    """
    if row.get("last_sale"):
        return "sold"
    if row.get("group_date"):
        return "delivered"
    return None


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

    A month answers for its own sales. Every sale on the book is settled
    against its consignment's payments oldest first, and the window then only
    decides which sales are reported, so the months add up to the whole instead
    of one month's payment cancelling another month's sale. The all-time figure
    rides alongside regardless. Slow stock is about what is sitting on the floor
    right now, so no window applies to it.
    """
    lo, hi = analytics.period_bounds(month, week)
    # The explicit day pickers are finer than a period, so they win where both
    # are set; the period fills them in otherwise.
    d_start, d_end = (start or lo), (end or hi)
    # The whole book is settled, then the window decides which sales are
    # reported. Handing it only the scoped sales made a consignment's entire
    # payment history land on one month of its sales, so a July payment cancelled
    # a September sale and September read as over-paid.
    status = payment_status(sales, payments, closed, lo, hi)
    # The all-time figure rides alongside so a month is never mistaken for the
    # total exposure. The months do now add up to it.
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
