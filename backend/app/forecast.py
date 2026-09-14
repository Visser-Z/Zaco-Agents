"""What the book says about next month.

Everything so far looks backwards: what sold, what is owed, what is sitting.
This looks forward, and it is the part where a tool can most easily mislead the
person using it, so the rules it works to are written down here.

**The arithmetic is here, not in the model.** Every figure the assistant reads
out is computed in this module from the recorded rows. Claude's job is to say
which of them matter and what to do about them. A language model asked to
project a number will produce one that reads well, and there is no way to tell
from the answer whether it came from the book.

**A projection is a range with its working attached.** Produce does not trade in
a straight line: a month is a handful of consignments, one of which can be half
the month. So a product's projection is the median of the months it actually
traded, bounded by the best and worst of them, and it carries the months it was
built from. A single number would be a claim the data cannot support.

**Season is the thing this cannot see.** Grapes, plums and nectarines have a
season, and separating season from trend needs more than one turn of the year.
Until the book holds twelve months, every projection here is "what this product
has been doing lately", not "what this product does in October". That limit is
returned as data (``caveats``) rather than left in a docstring, so the screen
and the prompt both have to carry it.

**Absence is a signal.** A product that stopped trading two months ago is
usually out of season, not underperforming, so it is projected at nothing and
listed apart rather than being averaged back to life.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import median

from . import analytics, reconcile

# How many months back a projection looks. Produce turns over fast and the older
# months are a different season; beyond this the median stops describing now.
RECENT_MONTHS = 3

# A product silent for this many trading months is treated as out of season and
# projected at nothing.
STALE_MONTHS = 2

# Below this many months of history, the book cannot see a season at all.
SEASON_MONTHS = 12


def _month(d: date) -> str:
    return d.strftime("%Y-%m")


def _next_month(d: date) -> str:
    return f"{d.year + (d.month == 12)}-{(d.month % 12) + 1:02d}"


def _month_range(months: list[str], last: str, back: int) -> list[str]:
    """The `back` most recent months in `months`, up to and including `last`."""
    return [m for m in months if m <= last][-back:]


def months_covered(rows: list[dict]) -> list[str]:
    """Every month the book has a sale in, oldest first."""
    return sorted({_month(d) for r in rows if (d := analytics.row_date(r))})


def monthly_by_product(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Per product, per month: what it earned, moved and arrived as."""
    out: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"value": 0.0, "cartons": 0.0, "consignments": set()}))
    for row in rows:
        day = analytics.row_date(row)
        if day is None:
            continue
        cell = out[analytics.product_label(row)][_month(day)]
        cell["value"] += analytics.row_value(row)
        cell["cartons"] += analytics.row_cartons(row)
        cell["consignments"].add(analytics.consignment_key(row))
    return {
        product: {
            month: {"value": round(c["value"], 2),
                    "cartons": round(c["cartons"], 2),
                    "consignments": len(c["consignments"])}
            for month, c in months.items()
        }
        for product, months in out.items()
    }


def velocity(rows: list[dict]) -> list[dict]:
    """How fast each product clears once it is on the floor.

    Measured per consignment and then taken as a median, because the mean is at
    the mercy of one load that sat for a month. ``cartons_per_day`` is what
    decides how much to send: a product that clears 40 a day will absorb a
    bigger load than one that clears 4, whatever either of them is worth.

    A consignment that cleared the same day it arrived spans zero days; it is
    counted as a day's trade rather than dropped, since dropping it would make
    the fastest movers look like they had no record at all.
    """
    out: list[dict] = []
    for product, items in _by_product(rows).items():
        spans, rates, sold_shares = [], [], []
        for group in analytics.group_consignments(items):
            days = analytics.consignment_days_to_sell(group)
            sold = sum(analytics.row_cartons(r) for r in group)
            if days is None or sold <= 0:
                continue
            spans.append(days)
            rates.append(sold / max(days, 1))
            sent = max(analytics._num(r.get("qty_received")) for r in group)
            if sent > 0:
                sold_shares.append(min(sold / sent, 1.0))
        if not spans:
            continue
        out.append({
            "product": product,
            "days_to_clear": round(float(median(spans)), 1),
            "slowest_days": max(spans),
            "cartons_per_day": round(float(median(rates)), 1),
            "sell_through": round(float(median(sold_shares)), 3) if sold_shares else None,
            "consignments": len(spans),
        })
    out.sort(key=lambda r: r["cartons_per_day"], reverse=True)
    return out


def outlets(rows: list[dict]) -> list[dict]:
    """Per product, what each market and agent actually achieved for it.

    This is the "where do I send it" question, and the honest answer is a price
    per carton beside the volume it was achieved on. One carton at a record
    price is not a better outlet than four hundred at a fair one, so the count
    travels with the price and the caller is expected to show both.
    """
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"value": 0.0, "cartons": 0.0, "consignments": set(), "months": set()})
    for row in rows:
        key = (analytics.product_label(row),
               (row.get("market") or "").strip() or None,
               (row.get("market_agent") or "").strip() or None)
        cell = groups[key]
        cell["value"] += analytics.row_value(row)
        cell["cartons"] += analytics.row_cartons(row)
        cell["consignments"].add(analytics.consignment_key(row))
        if (d := analytics.row_date(row)):
            cell["months"].add(_month(d))

    out = []
    for (product, market, agent), cell in groups.items():
        cartons = cell["cartons"]
        if cartons <= 0:
            continue
        out.append({
            "product": product, "market": market, "market_agent": agent,
            "cartons": round(cartons, 2), "value": round(cell["value"], 2),
            "price": round(cell["value"] / cartons, 2),
            "consignments": len(cell["consignments"]),
            "months": len(cell["months"]),
        })
    out.sort(key=lambda r: (r["product"], -r["price"]))
    return out


def payment_lag(rows: list[dict], payments: list[dict]) -> list[dict]:
    """How long each agent takes to pay, from the sale to the money.

    Measured from the last sale on a consignment to the payment against it, so
    it answers the question the operator actually has: money is owed on this,
    when does it usually arrive. A median again, because one statement settled
    months late would otherwise set the expectation for the whole agent.
    """
    last_sale: dict[tuple, date] = {}
    agent_of: dict[tuple, str] = {}
    for row in rows:
        key = reconcile._key(row.get("dn"), row.get("product"))
        day = analytics._parse_date(row.get("last_sale")) or analytics.row_date(row)
        if day is None:
            continue
        if key not in last_sale or day > last_sale[key]:
            last_sale[key] = day
        agent_of.setdefault(key, (row.get("market_agent") or "").strip() or "Unknown")

    lags: dict[str, list[int]] = defaultdict(list)
    for pay in payments:
        paid_on = analytics._parse_date(pay.get("date"))
        if paid_on is None:
            continue
        for line in pay.get("lines") or []:
            key = reconcile._key(pay.get("dn"), line.get("product"))
            sold_on = last_sale.get(key)
            if sold_on is None or paid_on < sold_on:
                continue
            lags[agent_of.get(key, "Unknown")].append((paid_on - sold_on).days)

    out = [{"market_agent": agent, "days_to_pay": round(float(median(v)), 1),
            "slowest_days": max(v), "payments": len(v)}
           for agent, v in lags.items() if v]
    out.sort(key=lambda r: r["days_to_pay"])
    return out


def _by_product(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[analytics.product_label(row)].append(row)
    return groups


def project(rows: list[dict], today: date | None = None) -> dict:
    """What next month looks like, product by product, with the working shown.

    The estimate for a product is the median of the months it traded in the
    recent window, and the range is the worst and best of those months. Two
    months give a range that is really just the two figures; one month gives no
    range at all and says so. Nothing here is extrapolated past what the book
    has actually seen.
    """
    today = today or date.today()
    target = _next_month(today)
    months = months_covered(rows)
    if not months:
        return {"month": target, "months_covered": [], "products": [], "resting": [],
                "total": {"low": 0.0, "estimate": 0.0, "high": 0.0},
                "caveats": ["Nothing is recorded yet, so there is nothing to project."]}

    last = months[-1]
    window = _month_range(months, last, RECENT_MONTHS)
    # "Recently traded" is measured against the book's own last month, not
    # against today. A book that stops in June is stale, and that is said in the
    # caveats -- but silently calling every product out of season because the
    # calendar moved on would be a different and less useful answer.
    live_window = _month_range(months, last, STALE_MONTHS)

    by_product = monthly_by_product(rows)
    vel = {v["product"]: v for v in velocity(rows)}

    projected, resting = [], []
    for product, series in by_product.items():
        traded = [m for m in window if m in series]
        recent = [m for m in live_window if m in series]
        seen = sorted(series)
        if not recent:
            resting.append({
                "product": product,
                "last_traded": seen[-1],
                "months_quiet": len([m for m in months if m > seen[-1]]),
                "reason": "nothing sold in the last "
                          f"{len(live_window)} month{'s' if len(live_window) != 1 else ''} on the book",
            })
            continue

        values = [series[m]["value"] for m in traded]
        cartons = [series[m]["cartons"] for m in traded]
        v = vel.get(product, {})
        projected.append({
            "product": product,
            "estimate": round(median(values), 2),
            "low": round(min(values), 2),
            "high": round(max(values), 2),
            "cartons_estimate": round(median(cartons), 1),
            "months_used": traded,
            "basis": {m: series[m]["value"] for m in traded},
            # One month is a reading, not a range. Saying so is the point.
            "confidence": "fair" if len(traded) >= 3 else "low" if len(traded) == 2 else "single month",
            "days_to_clear": v.get("days_to_clear"),
            "cartons_per_day": v.get("cartons_per_day"),
            "sell_through": v.get("sell_through"),
        })

    projected.sort(key=lambda r: r["estimate"], reverse=True)
    resting.sort(key=lambda r: r["last_traded"], reverse=True)

    total = {
        "low": round(sum(p["low"] for p in projected), 2),
        "estimate": round(sum(p["estimate"] for p in projected), 2),
        "high": round(sum(p["high"] for p in projected), 2),
    }

    return {
        "month": target,
        "months_covered": months,
        "window": window,
        "products": projected,
        "resting": resting,
        "total": total,
        "caveats": caveats(rows, months, today),
    }


def caveats(rows: list[dict], months: list[str], today: date) -> list[str]:
    """What the reader has to know before trusting any of the above.

    Returned as data, not left to the prose, because a limit that lives only in
    a docstring is one the screen and the prompt can both quietly drop.
    """
    out: list[str] = []
    if len(months) < SEASON_MONTHS:
        out.append(
            f"The book holds {len(months)} month{'s' if len(months) != 1 else ''} of trade. "
            "Season and trend cannot be told apart under a full year, so these are "
            "figures for what each product has been doing lately, not for what it "
            "does at this time of year.")
    if months and months[-1] < _month(today):
        out.append(
            f"The last recorded sale is in {months[-1]} and it is now {_month(today)}. "
            "The projection is built on the months the book has, so anything traded "
            "since is missing from it.")
    thin = [m for m in months[-RECENT_MONTHS:]
            if len({analytics.consignment_key(r) for r in rows
                    if (d := analytics.row_date(r)) and _month(d) == m}) < 5]
    if thin:
        out.append(
            f"{', '.join(thin)} rest{'s' if len(thin) == 1 else ''} on fewer than five "
            "consignments, so one load moves the whole month.")
    gaps = _gaps(months)
    if gaps:
        out.append(
            f"No sales are recorded for {', '.join(gaps)}. If those months traded, the "
            "history is incomplete and every figure here is low.")
    return out


def _gaps(months: list[str]) -> list[str]:
    """Calendar months between the first and last with nothing recorded."""
    if len(months) < 2:
        return []
    out, (y, m) = [], (int(months[0][:4]), int(months[0][5:]))
    while (cur := f"{y}-{m:02d}") < months[-1]:
        if cur not in months:
            out.append(cur)
        y, m = y + (m == 12), (m % 12) + 1
    return out


def build(rows: list[dict], payments: list[dict], today: date | None = None) -> dict:
    """The whole forward-looking picture, computed once for screen and prompt."""
    return {
        "projection": project(rows, today),
        "velocity": velocity(rows),
        "outlets": outlets(rows),
        "payment_lag": payment_lag(rows, payments),
    }
