"""Is Zaco being treated fairly by the agent selling on its behalf?

The agent takes the produce, sells it on the floor over days, and remits Gross
less deductions = Nett. Zaco is not there. So the only leverage is what the two
reports say, and it is worth being exact about which questions they can answer.

**Answerable, and normally clean.** The deduction rate, ``1 - nett/gross``. Over
real June data the median is 15.0% and 120 of 161 account sales sit between 14%
and 16%, so a line well outside that band is a real question with a number
attached. This needs nothing but the two figures the reports already carry.

**Not answerable from the reports.** Whether the price recorded is the price the
fruit actually made. 98% of docket prices are exact multiples of R10, only 2 of
485 dockets carry any cents, and one month uses 31 distinct prices -- floor
negotiation does not produce that. The same commodity on the same day also spans
up to 5.6x, and genuine end-of-day clearance is indistinguishable from
under-reporting the good sales. The report has a ``Market Avg`` column that would
settle it and the export leaves it 0.00 in every line.

So the flags below are **questions, not accusations**: each carries the figures
that raised it, and a high deduction has innocent explanations (a commodity levy,
a return, a part-paid run). Nothing here is called fraud, because nothing here can
prove it.
"""

from __future__ import annotations

from statistics import median

from . import analytics

# A rate this far in percentage points above the going rate is worth a question.
OVER_MEDIAN_PP = 0.05
# Above this, the agent kept more than it remitted, whatever the median says.
SEVERE_RATE = 0.50
# Fewer than this many priced lines and there is no "going rate" to compare to.
MIN_FOR_MEDIAN = 8
# A default only used to describe a lone line when there is no history yet.
TYPICAL_RATE = 0.15
# Sold this share or less of what went out. Applies to ONE consignment.
UNSOLD_MAJORITY = 0.50
# A product whose overall unsold share is worth a glance beside its turnover.
# A quarter of everything sent never selling is notable; the real June figures
# run 7% (oranges, unremarkable) to 51% (nectarines, worth asking about).
PRODUCT_UNSOLD_NOTABLE = 0.25
# Rows smaller than this are left out of the price range. A single carton dumped
# at R1 is real, and it belongs in the unsold figure -- in a price spread it
# reports "107x" for strawberries and drowns the comparison it was meant to make.
MIN_CARTONS_FOR_SPREAD = 3


def row_gross(row: dict) -> float:
    return analytics.row_value(row)


def deduction_rate(row: dict) -> float | None:
    """Share of the sale the agent kept. None when either figure is missing.

    Works per row even though the Nett arrives per statement, because a
    statement's Nett is apportioned across its rows by the same gross this
    divides by, so the ratio survives the split.
    """
    gross = row_gross(row)
    nett = row.get("nett_total")
    if nett is None or gross <= 0:
        return None
    return 1.0 - (float(nett) / gross)


def going_rate(rows: list[dict]) -> float | None:
    """The rate this business normally runs at, from its own history."""
    rates = [r for row in rows if (r := deduction_rate(row)) is not None]
    return median(rates) if len(rates) >= MIN_FOR_MEDIAN else None


def rate_concerns(rows: list[dict]) -> list[dict]:
    """Rows whose deduction is out of line, worst first.

    Compared against this business's own median rather than a figure of mine, so
    it stays right if the agent's terms change.
    """
    rate = going_rate(rows)
    baseline = rate if rate is not None else TYPICAL_RATE
    out: list[dict] = []
    for row in rows:
        r = deduction_rate(row)
        if r is None:
            continue
        gross, nett = row_gross(row), float(row["nett_total"])
        if nett <= 0 < gross:
            severity, why = "severe", "Nothing came back at all on this sale."
        elif r >= SEVERE_RATE:
            severity, why = "severe", f"The agent kept more than it paid over ({r:.0%})."
        elif r > baseline + OVER_MEDIAN_PP:
            severity, why = "watch", (
                f"{r:.0%} deducted, against the {baseline:.0%} you normally pay.")
        else:
            continue
        out.append({
            "stm_no": row.get("stm_no"),
            "dn": row.get("dn"),
            "product": analytics.product_label(row),
            "market_agent": row.get("market_agent"),
            "date": row.get("group_date") or row.get("invoice_date"),
            "gross": round(gross, 2),
            "nett": round(nett, 2),
            "kept": round(gross - nett, 2),
            "rate": round(r, 4),
            "baseline": round(baseline, 4),
            "severity": severity,
            "why": why,
        })
    out.sort(key=lambda d: (d["severity"] != "severe", -d["kept"]))
    return out


def unsold_concerns(rows: list[dict]) -> list[dict]:
    """Consignments where most of what was sent never sold, worst first.

    On consignment this loss lands on the supplier, not Zaco. It is here because
    the agent decides what moves off the floor, and produce reported as unsold is
    the one thing the reports can never contradict.
    """
    out: list[dict] = []
    for group in analytics.group_consignments(rows):
        sent = max(analytics._num(r.get("qty_received")) for r in group)
        sold = sum(analytics.row_cartons(r) for r in group)
        if sent <= 0 or sold > sent * UNSOLD_MAJORITY:
            continue
        first = group[0]
        out.append({
            "consignment_id": first.get("consignment_id"),
            "dn": first.get("dn"),
            "product": analytics.product_label(first),
            "market_agent": first.get("market_agent"),
            "date": first.get("group_date") or first.get("date_received"),
            "cartons_sent": int(sent),
            "cartons_sold": int(sold),
            "cartons_left": int(sent - sold),
            "share_sold": round(sold / sent, 4),
            "runs": len(group),
        })
    out.sort(key=lambda d: -d["cartons_left"])
    return out


def unsold_total(rows: list[dict]) -> dict:
    """Cartons sent, sold and left, over everything.

    Counted per consignment, taking each delivery's Qty Sent ONCE. A consignment
    settled over three account sales is three rows all repeating the same Qty
    Sent, so summing the column would treble it: on real June data that turns
    4 487 cartons sent into 13 060 and reports 76% unsold instead of 30%.
    """
    sent = sum(
        max(analytics._num(r.get("qty_received")) for r in group)
        for group in analytics.group_consignments(rows)
    ) if rows else 0.0
    sold = sum(analytics.row_cartons(r) for r in rows)
    return {
        "cartons_sent": int(sent),
        "cartons_sold": int(sold),
        "cartons_left": int(max(sent - sold, 0)),
        "share_unsold": round(max(sent - sold, 0) / sent, 4) if sent else None,
    }


def by_product(rows: list[dict]) -> dict[str, dict]:
    """Per product, the share of everything sent that never sold.

    This is the figure that belongs beside a product's turnover. The
    consignment-level entries in ``unsold_concerns`` are a different claim: a
    chip built from those said "85% not sold" against oranges whose real figure
    was 7%, because one bad consignment was being read as the whole product.
    """
    out: dict[str, dict] = {}
    for p in analytics.product_performance(rows):
        if p["sell_through"] is None:
            continue                       # nothing recorded as sent; no ratio
        share = max(1.0 - p["sell_through"], 0.0)
        if share < PRODUCT_UNSOLD_NOTABLE:
            continue
        out[p["label"]] = {
            "product": p["label"],
            "share_unsold": round(share, 4),
            "consignments": p["consignments"],
            "notable": True,
        }
    return out


def price_spread(rows: list[dict]) -> list[dict]:
    """Per product, the range of prices its rows realised.

    A wide spread is not wrongdoing -- the floor pays what it pays, and a late
    run clears cheap. It is here because it is the shape under-reporting would
    take, and because the operator is the only one who knows whether a 3x spread
    on one commodity in one month is normal for it.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if (row.get("price") or 0) > 0 and analytics.row_cartons(row) >= MIN_CARTONS_FOR_SPREAD:
            groups.setdefault(analytics.product_label(row), []).append(row)

    out: list[dict] = []
    for label, items in groups.items():
        prices = sorted(float(r["price"]) for r in items)
        if len(prices) < 3 or prices[0] <= 0:
            continue
        out.append({
            "product": label,
            "low": round(prices[0], 2),
            "high": round(prices[-1], 2),
            "median": round(median(prices), 2),
            "ratio": round(prices[-1] / prices[0], 2),
            "rows": len(prices),
        })
    out.sort(key=lambda d: -d["ratio"])
    return out


def summary(rows: list[dict]) -> dict:
    """Everything the flags rest on, plus what cannot be checked and why.

    ``price_unverifiable`` is deliberately part of the payload. A panel that
    only ever reports what it CAN check reads as a clean bill of health on the
    one thing it is blind to.
    """
    rate = going_rate(rows)
    concerns = rate_concerns(rows)
    unsold = unsold_concerns(rows)
    spread = price_spread(rows)
    priced = [r for r in rows if deduction_rate(r) is not None]
    return {
        "going_rate": round(rate, 4) if rate is not None else None,
        "rate_known_from": len(priced),
        "rate_concerns": concerns,
        "kept_above_normal": round(
            sum(c["kept"] - c["gross"] * c["baseline"] for c in concerns), 2),
        # The flagged consignments, and separately the REAL total. These were one
        # field before: the headline said "sent but never sold" while summing only
        # consignments that sold half or less, understating June by 75 cartons
        # (1 268 against 1 343) while reading as the whole figure.
        "unsold": unsold[:12],
        "unsold_worst": len(unsold),
        "totals": unsold_total(rows),
        "unsold_cartons": unsold_total(rows)["cartons_left"],
        "by_product": by_product(rows),
        "price_spread": spread[:8],
        # Stated in the payload so the UI cannot imply the price was checked.
        "price_unverifiable": (
            "Whether each carton fetched what the report says cannot be checked "
            "from these exports. The Daily Sales report has a Market Avg column "
            "that would settle it and the export leaves it empty — ask "
            "TechnoFresh to fill it in."
        ),
        "flagged": len(concerns) + len(unsold),
    }
