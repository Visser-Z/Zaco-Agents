"""Sales analytics over the recorded `statements` history.

Pure functions over a list of plain row dicts (exactly what PostgREST returns),
so they can be unit-tested without a database and carry no pandas/numpy weight
into the serverless bundle. The aggregation is small-N by nature -- one internal
team's statements -- so plain Python is more than fast enough.

Key money rule for this business: the Daily Sales report leaves Nett blank (the
operator computes the deduction by hand), so the only sale figure we can trust
across both report formats is the GROSS value, cartons_sold x unit price. Every
"value" here is that gross figure, in ZAR.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Iterable

UNKNOWN = "Unknown"


# --- per-row helpers ------------------------------------------------------

def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def row_value(row: dict) -> float:
    """Gross sale value for a row: cartons sold x unit price."""
    return _num(row.get("cartons_sold")) * _num(row.get("price"))


def row_cartons(row: dict) -> float:
    """Cartons sold NET of anything returned -- what the workbook column holds."""
    return _num(row.get("cartons_sold"))


def row_returned(row: dict) -> float:
    """Cartons that came back. Held positive by the parsers."""
    return _num(row.get("cartons_returned"))


def row_returns_value(row: dict) -> float:
    """What those returned cartons had been sold for, positive."""
    return _num(row.get("returns_total"))


def row_cartons_gross(row: dict) -> float:
    """Cartons that actually sold, before the returns came off."""
    return row_cartons(row) + row_returned(row)


def row_value_gross(row: dict) -> float:
    """Gross takings before returns: what the sales rang up."""
    return row_value(row) + row_returns_value(row)


def product_label(row: dict) -> str:
    """The product, by its real name.

    This used to prefer the operator's short code, which is the right thing for
    the Excel column (that is what the sheet is written in) and the wrong thing
    for everything else. Codes are assigned by hand from a lookup table, so a
    product with no code yet fell back to its raw name and appeared as a second,
    unrelated line -- the same fruit ranked twice in Insights, split across two
    rows of the buy list, and matched by neither buy chip. The raw name is what
    every report actually prints and what the consignment deals are keyed on
    (``consignment.row_key``), so grouping on it makes those three agree.

    The short code has not gone anywhere: ``description`` still carries it and
    the workbook is still written from it.
    """
    return (row.get("product") or row.get("description") or UNKNOWN) or UNKNOWN


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value[: len(fmt) + 6], fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def row_date(row: dict) -> date | None:
    """The date a row is bucketed under for trends: the delivery/group date if
    present, else the invoice date, else when it was received."""
    for key in ("group_date", "invoice_date", "date_received", "created_at"):
        if (d := _parse_date(row.get(key))) is not None:
            return d
    return None


# --- consignments ---------------------------------------------------------
# A row is one account sale. A consignment is usually settled over several of
# them, and the figures that belong to the *delivery* -- what was sent, how long
# it took to clear -- must be counted once for the consignment, not once per row.
# Money and cartons sold are per row and are summed as they always were.

def consignment_key(row: dict, fallback: object = None):
    """What identifies the delivery a row came from.

    Rows recorded before the per-statement split have no consignment id and were
    one row per consignment already, so each falls in a group of its own rather
    than being pooled with unrelated rows that share a blank id.
    """
    cid = row.get("consignment_id")
    if cid:
        return ("c", cid)
    return ("row", id(row) if fallback is None else fallback)


def group_consignments(rows: Iterable[dict]) -> list[list[dict]]:
    """The rows, grouped into the consignments they were settled under."""
    groups: dict[object, list[dict]] = defaultdict(list)
    for row in rows:
        groups[consignment_key(row)].append(row)
    return list(groups.values())


def cartons_sent(rows: Iterable[dict]) -> float:
    """Cartons sent to market, counting each consignment's delivery once."""
    total = 0.0
    for group in group_consignments(rows):
        total += max(_num(r.get("qty_received")) for r in group)
    return total


# --- aggregations ---------------------------------------------------------

def _totals_by(rows: Iterable[dict], key) -> list[dict]:
    """Sum value and cartons grouped by ``key(row)``, sorted by value desc."""
    value: dict[str, float] = defaultdict(float)
    cartons: dict[str, float] = defaultdict(float)
    lines: dict[str, int] = defaultdict(int)
    for row in rows:
        label = key(row) or UNKNOWN
        value[label] += row_value(row)
        cartons[label] += row_cartons(row)
        lines[label] += 1
    out = [
        {
            "label": label,
            "value": round(value[label], 2),
            "cartons": round(cartons[label], 2),
            "lines": lines[label],
        }
        for label in value
    ]
    out.sort(key=lambda d: d["value"], reverse=True)
    return out


def _with_share(items: list[dict], total: float) -> list[dict]:
    """Add each item's share of the total and the running cumulative share."""
    running = 0.0
    for item in items:
        item["share"] = round(item["value"] / total, 4) if total else 0.0
        running += item["value"]
        item["cumulative_share"] = round(running / total, 4) if total else 0.0
    return items


def _abc_class(cumulative_before: float) -> str:
    """Pareto/ABC banding by the cumulative value share *reached before* this
    item: the vital few (A) up to 80%, the next 15% (B), the long tail (C).

    Banding on the share before the item -- not including it -- keeps the single
    biggest seller always in class A even when it alone clears 80% of value.
    """
    if cumulative_before < 0.80:
        return "A"
    if cumulative_before < 0.95:
        return "B"
    return "C"


def best_sellers(rows: list[dict]) -> list[dict]:
    """Products ranked by gross value, each tagged with its Pareto share/class."""
    items = _totals_by(rows, product_label)
    total = sum(i["value"] for i in items)
    items = _with_share(items, total)
    before = 0.0
    for item in items:
        item["abc"] = _abc_class(before)
        before = item["cumulative_share"]
    return items


def _period_key(d: date, period: str) -> str:
    if period == "day":
        return d.isoformat()          # e.g. 2026-07-27
    if period == "month":
        return d.strftime("%Y-%m")    # e.g. 2026-07
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"  # e.g. 2026-W31


def available_periods(rows: list[dict]) -> dict:
    """The months and weeks that actually have data, for the dashboard's month
    and week pickers. Weeks are also grouped per month so the week picker can
    narrow to the selected month."""
    from collections import defaultdict as _dd

    months: set[str] = set()
    weeks: set[str] = set()
    weeks_by_month: dict[str, set[str]] = _dd(set)
    for row in rows:
        d = row_date(row)
        if d is None:
            continue
        m = _period_key(d, "month")
        w = _period_key(d, "week")
        months.add(m)
        weeks.add(w)
        weeks_by_month[m].add(w)
    return {
        "months": sorted(months),
        "weeks": sorted(weeks),
        "weeks_by_month": {m: sorted(ws) for m, ws in weeks_by_month.items()},
    }


def filter_rows(rows: list[dict], month: str | None = None, week: str | None = None) -> list[dict]:
    """Rows falling in a given month ("YYYY-MM") and/or ISO week ("YYYY-Www").

    Week is the finer scope and wins when both are given. Undated rows can't be
    placed in a period, so they drop out of any filtered view (but remain in the
    unfiltered all-time view)."""
    if not month and not week:
        return list(rows)
    out: list[dict] = []
    for row in rows:
        d = row_date(row)
        if d is None:
            continue
        if week and _period_key(d, "week") != week:
            continue
        if month and not week and _period_key(d, "month") != month:
            continue
        out.append(row)
    return out


def period_bounds(month: str | None = None, week: str | None = None) -> tuple[str | None, str | None]:
    """First and last calendar date (ISO) of a month ("YYYY-MM") or ISO week
    ("YYYY-Www"). Used to scope a delete of a period's history. Week wins."""
    import calendar

    if week:
        year, wk = week.split("-W")
        monday = date.fromisocalendar(int(year), int(wk), 1)
        sunday = date.fromisocalendar(int(year), int(wk), 7)
        return monday.isoformat(), sunday.isoformat()
    if month:
        y, m = (int(x) for x in month.split("-"))
        return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    return None, None


def trend_granularity(month: str | None = None, week: str | None = None) -> str:
    """Zoom the trend to match the chosen scope: a single week charts by day, a
    single month by week, and the all-time view by month."""
    if week:
        return "day"
    if month:
        return "week"
    return "month"


def trend(rows: list[dict], period: str = "week") -> list[dict]:
    """Value and cartons over time, bucketed by ISO week or calendar month."""
    value: dict[str, float] = defaultdict(float)
    cartons: dict[str, float] = defaultdict(float)
    for row in rows:
        d = row_date(row)
        if d is None:
            continue
        key = _period_key(d, period)
        value[key] += row_value(row)
        cartons[key] += row_cartons(row)
    return [
        {"period": key, "value": round(value[key], 2), "cartons": round(cartons[key], 2)}
        for key in sorted(value)
    ]


def kpis(rows: list[dict]) -> dict:
    """Headline totals for the dashboard's top strip.

    Cartons and takings are reported three ways, because one figure cannot say
    it: what SOLD, what came back, and the NET difference. The net is what the
    workbook holds and what every other figure here is built on, so it keeps the
    established names (``total_cartons``, ``total_value``); the other two are
    additions beside it. A month with a heavy return run reads as a good month
    on the net alone, which is exactly the case worth seeing.
    """
    dates = [d for row in rows if (d := row_date(row)) is not None]
    sold_cartons = sum(row_cartons_gross(r) for r in rows)
    returned_cartons = sum(row_returned(r) for r in rows)
    returns_value = sum(row_returns_value(r) for r in rows)
    return {
        "total_value": round(sum(row_value(r) for r in rows), 2),
        "total_cartons": round(sum(row_cartons(r) for r in rows), 2),
        # Before returns came off.
        "gross_value": round(sum(row_value_gross(r) for r in rows), 2),
        "cartons_sold": round(sold_cartons, 2),
        # What came back, and what share of what sold that was. The rate is
        # measured against what sold, not against the net: 280 back out of 3,448
        # sold is 8.1%, and dividing by the 3,168 that stuck would overstate it.
        "cartons_returned": round(returned_cartons, 2),
        "returns_value": round(returns_value, 2),
        "return_rate": round(returned_cartons / sold_cartons, 4) if sold_cartons else 0.0,
        "statement_count": len(rows),
        "product_count": len({product_label(r) for r in rows}),
        "market_count": len({(r.get("market") or UNKNOWN) for r in rows}),
        "agent_count": len({(r.get("market_agent") or UNKNOWN) for r in rows}),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
    }


def days_to_sell(row: dict) -> int | None:
    """How many days this run took, from arrival to its last sale.

    0 means it cleared the same day. None when the report didn't carry both
    dates (the account-sales format, or rows recorded before `last_sale`)."""
    first = _parse_date(row.get("date_received"))
    last = _parse_date(row.get("last_sale"))
    if first is None or last is None or last < first:
        return None
    return (last - first).days


def consignment_days_to_sell(group: list[dict]) -> int | None:
    """How long the whole delivery took to clear.

    A consignment settled over three account sales has three rows, each ending
    at its own last sale. Averaging those understates how long the fruit
    actually sat there, so the span runs to the last sale of the *latest* row.
    """
    spans = [d for r in group if (d := days_to_sell(r)) is not None]
    return max(spans) if spans else None


def product_performance(rows: list[dict]) -> list[dict]:
    """Per product, the signals that actually bear on what to buy next.

    Beyond takings: what price a carton fetched, how much of what was sent
    actually sold, and how long it took to move. All arithmetic is done here so
    the assistant never has to derive it."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[product_label(row)].append(row)

    out: list[dict] = []
    for label, items in groups.items():
        value = sum(row_value(r) for r in items)
        cartons = sum(row_cartons(r) for r in items)
        # Sell-through only compares consignments that report BOTH figures.
        # Mixing in rows that sold but never recorded a sent quantity pushes the
        # ratio above 100%, which is nonsense the assistant would then repeat.
        #
        # It also has to count the delivery ONCE. A consignment settled over
        # three account sales is three rows all carrying the same Qty Sent, so
        # summing the column would treble what was sent and report a third of
        # the real sell-through.
        consignments = group_consignments(items)
        paired = [g for g in consignments if max(_num(r.get("qty_received")) for r in g) > 0]
        sent = cartons_sent(r for g in paired for r in g)
        sold_of_sent = sum(row_cartons(r) for g in paired for r in g)
        spans = [d for g in consignments if (d := consignment_days_to_sell(g)) is not None]
        months = sorted({_period_key(d, "month") for r in items if (d := row_date(r))})
        out.append(
            {
                "label": label,
                "value": round(value, 2),
                "cartons": round(cartons, 2),
                "consignments": len(consignments),
                "avg_price": round(value / cartons, 2) if cartons else 0.0,
                # Share of what was sent to market that actually sold.
                "sell_through": round(sold_of_sent / sent, 4) if sent else None,
                "avg_days_to_sell": round(sum(spans) / len(spans), 1) if spans else None,
                "slowest_days": max(spans) if spans else None,
                "months_seen": months,
            }
        )
    out.sort(key=lambda d: d["value"], reverse=True)
    return out


def row_earned(row: dict) -> float:
    """What actually came back. Nett where the payment has landed, otherwise
    the gross it sold for."""
    nett = row.get("nett_total")
    return float(nett) if nett is not None else row_value(row)


def commission(rows: list[dict], deals: dict) -> dict:
    """What Zaco earned, and what it owes, over the rows with agreed terms.

    Replaces the old profit figure, which subtracted a purchase price that does
    not exist: nothing is bought, so there is no spend to net off. What Zaco
    keeps is its percentage of the Nett; the rest is money held on the
    supplier's behalf, not income.

    Deliberately reports its own coverage: commission over a fifth of the
    business is a useful number only if you know it is a fifth.
    """
    from . import consignment

    earned = owed = nett = 0.0
    covered = 0
    for row in rows:
        line = consignment.settle_row(row, deals.get(consignment.row_key(row)))
        if line is None:
            continue
        earned += line["commission"]
        owed += 0.0 if line["settled"] else line["owed_to_supplier"]
        nett += line["nett"]
        covered += 1
    if not covered:
        return {"known": False, "covered": 0, "of": len(rows)}
    return {
        "known": True,
        "covered": covered,
        "of": len(rows),
        "nett": round(nett, 2),
        "commission": round(earned, 2),
        "owed_to_suppliers": round(owed, 2),
        "rate": round(earned / nett, 4) if nett else None,
    }


def compute(rows: list[dict], period: str = "week", deals: dict | None = None) -> dict:
    """The full analytics payload the frontend renders."""
    return {
        "kpis": kpis(rows),
        "best_sellers": best_sellers(rows),
        "by_market": _totals_by(rows, lambda r: r.get("market")),
        "by_agent": _totals_by(rows, lambda r: r.get("market_agent")),
        "trend": trend(rows, period),
        "period": period,
        # Turnover answers "how much moved". This answers "did we make money",
        # which is the question the rest of the dashboard cannot.
        "commission": commission(rows, deals or {}),
    }
