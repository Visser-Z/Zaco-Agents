"""A report for any date range the operator asks for.

The workbook answered "what did August look like" by being a sheet you opened.
The history answers it by being queried, so the question needs a shape: a first
and last date, and everything that falls between them.

Deliberately a period *statement* rather than a dashboard. Insights is for
looking around; this is for printing, filing and handing to someone. So it is
flat, totals every column it shows, and says plainly what it could not account
for rather than leaving the reader to notice a gap.

Pure functions over row dicts, no database, so it unit-tests without one.
"""

from __future__ import annotations

from collections import defaultdict

from . import analytics, integrity, reconcile


def _day(value) -> str | None:
    return str(value)[:10] if value else None


def in_range(rows: list[dict], key: str, start: str | None, end: str | None) -> list[dict]:
    """Rows whose `key` date falls within the range, inclusive at both ends."""
    out = []
    for row in rows:
        day = _day(row.get(key))
        if day is None:
            continue
        if (start and day < start) or (end and day > end):
            continue
        out.append(row)
    return out


def by_product(sales: list[dict]) -> list[dict]:
    """What each commodity did over the range, biggest earner first."""
    agg: dict[str, dict] = {}
    for row in sales:
        name = analytics.product_label(row)
        d = agg.setdefault(name, {"product": name, "cartons": 0.0, "returned": 0.0,
                                  "value": 0.0, "days": set()})
        d["cartons"] += analytics.row_cartons(row)
        d["returned"] += analytics.row_returned(row)
        d["value"] += analytics.row_value(row)
        if (day := _day(row.get("group_date"))):
            d["days"].add(day)
    out = []
    for d in agg.values():
        cartons = round(d["cartons"], 2)
        value = round(d["value"], 2)
        out.append({
            "product": d["product"],
            "cartons": cartons,
            "returned": round(d["returned"], 2),
            "value": value,
            "days_sold": len(d["days"]),
            # The average this commodity actually fetched over the range.
            "price": round(value / cartons, 2) if cartons > 0 else 0.0,
        })
    return sorted(out, key=lambda r: r["value"], reverse=True)


def by_day(sales: list[dict]) -> list[dict]:
    """Every day in the range that sold something, oldest first."""
    agg: dict[str, dict] = {}
    for row in sales:
        day = _day(row.get("group_date"))
        if day is None:
            continue
        d = agg.setdefault(day, {"date": day, "cartons": 0.0, "returned": 0.0, "value": 0.0})
        d["cartons"] += analytics.row_cartons(row)
        d["returned"] += analytics.row_returned(row)
        d["value"] += analytics.row_value(row)
    return [{"date": d["date"], "cartons": round(d["cartons"], 2),
             "returned": round(d["returned"], 2), "value": round(d["value"], 2)}
            for d in sorted(agg.values(), key=lambda x: x["date"])]


def by_agent(sales: list[dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"cartons": 0.0, "value": 0.0})
    for row in sales:
        name = row.get("market_agent") or "Unattributed"
        agg[name]["cartons"] += analytics.row_cartons(row)
        agg[name]["value"] += analytics.row_value(row)
    return sorted(
        ({"agent": k, "cartons": round(v["cartons"], 2), "value": round(v["value"], 2)}
         for k, v in agg.items()),
        key=lambda r: r["value"], reverse=True)


def money(sales: list[dict], payments: list[dict]) -> dict:
    """What was sold against what was actually paid for it.

    The payments counted here are the ones *received* in the range. They will
    not tie to the sales figure and are not meant to: a payment settles sales
    that happened earlier, sometimes weeks earlier. Both are reported, and the
    difference is left visible rather than netted into a single number that
    would mean neither thing.
    """
    sold = round(sum(analytics.row_value(r) for r in sales), 2)
    received = round(sum(reconcile._num(p.get("gross")) for p in payments), 2)
    nett = round(sum(reconcile._num(p.get("nett")) for p in payments), 2)
    matched = [r for r in reconcile.reconcile_any(sales, payments) if r["status"] == "matched"]
    return {
        "sold": sold,
        "payments_received": received,
        "payments_nett": nett,
        "deductions": round(received - nett, 2),
        "deduction_rate": round((received - nett) / received, 4) if received > 0 else None,
        "reconciled": round(sum(r["daily_total"] for r in matched), 2),
        "reconciled_lines": len(matched),
        "payment_count": len(payments),
    }


def build(sales: list[dict], payments: list[dict],
          start: str | None = None, end: str | None = None) -> dict:
    """The whole report for one date range.

    Sales are selected on the day they sold; payments on the day they were
    received. Two different questions, each asked of the column that answers it.
    """
    scoped_sales = in_range(sales, "group_date", start, end)
    scoped_pay = in_range(payments, "date", start, end)

    cartons = round(sum(analytics.row_cartons(r) for r in scoped_sales), 2)
    returned = round(sum(analytics.row_returned(r) for r in scoped_sales), 2)
    days = by_day(scoped_sales)

    return {
        "range": {"from": start, "to": end,
                  "first_sale": days[0]["date"] if days else None,
                  "last_sale": days[-1]["date"] if days else None},
        "totals": {
            "statements": len(scoped_sales),
            "consignments": len({analytics.consignment_key(r, i)
                                 for i, r in enumerate(scoped_sales)}),
            "products": len({analytics.product_label(r) for r in scoped_sales}),
            "days_traded": len(days),
            "cartons_sold": cartons,
            "cartons_returned": returned,
            "cartons_gross": round(cartons + returned, 2),
            "return_rate": round(returned / (cartons + returned), 4) if cartons + returned > 0 else 0.0,
        },
        "money": money(scoped_sales, scoped_pay),
        "by_day": days,
        "by_product": by_product(scoped_sales),
        "by_agent": by_agent(scoped_sales),
        "integrity": integrity.summary(scoped_sales),
    }
