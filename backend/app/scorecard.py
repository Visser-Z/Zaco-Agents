"""Where to send it: every market and agent a product has gone to, compared.

The decision this serves is made every week: which floor, through which agent,
gets this load. The honest comparison is not the price a carton fetched. A
high price on a handful of cartons, at an agent that keeps more, on fruit that
half sat unsold, puts less in the bank than a fair price on a full load that
cleared. So each destination is scored on one figure the operator can act on:

    rand back per carton sent = price per carton sold
                                x (1 - the agent's cut)
                                x the share of what was sent that sold

Everything behind it is shown beside it, so the ranking can be checked rather
than trusted, and a destination with too little history says so instead of
being ranked on luck.

**The arithmetic is here, never in the model.** The written recommendation on
the Intelligence tab reads these figures; it does not produce any of them.

Pure functions over the saved sales rows and payment records.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from . import analytics, integrity, payment_details, reconcile

# A destination needs this many consignments of a product before its figures
# are a comparison rather than an anecdote.
MIN_CONSIGNMENTS = 2

# Too small a product to be worth a verdict: a few cartons in the period.
MIN_CARTONS = 10

# Sell-through is read only off consignments that have had time to sell.
# One that arrived last week is half on the floor by nature, and counting it
# would mark down whichever destination was used most recently.
SETTLED_AFTER_DAYS = 14

# The default window. Produce runs in seasons; older months describe a
# different crop and a different floor.
DEFAULT_MONTHS = 3


def _window(rows: list[dict], months: int) -> tuple[date | None, date | None]:
    """The period compared: the last `months` months of trading in the book.

    Counted back from the latest sale on file rather than from today, so a book
    that has not had a report loaded for a fortnight still compares a full
    window instead of a thinning one.
    """
    days = [d for r in rows if (d := analytics.row_date(r))]
    if not days:
        return None, None
    last = max(days)
    if months <= 0:
        return min(days), last
    return last - timedelta(days=round(months * 30.4)), last


def agent_cut(payments: list[dict], lo: date | None, hi: date | None) -> dict[str, dict]:
    """What each market's agent kept, as a share of gross, over the period.

    Read off the payments alone: gross less nett over gross, per account sale,
    with its median taken so one odd statement cannot move it. Keyed by market
    through the AccSale prefix, because one agency sells at more than one
    market on different terms.
    """
    rates: dict[str, list[float]] = defaultdict(list)
    for rec in payments:
        gross = reconcile._num(rec.get("gross"))
        if gross <= 0:
            continue
        paid = analytics._parse_date(rec.get("date"))
        if paid and ((lo and paid < lo) or (hi and paid > hi + timedelta(days=21))):
            continue
        market = rec.get("market") or payment_details.destination(rec.get("accsale"))["market"]
        if market:
            rates[market].append((gross - reconcile._num(rec.get("nett"))) / gross)
    return {m: {"rate": round(median(v), 4), "payments": len(v)} for m, v in rates.items()}


def _sell_through(groups: list[list[dict]], last: date) -> tuple[float | None, int]:
    """Share of what was booked in that sold, over consignments old enough.

    A consignment counts once, at what the market booked in (Qty Amended To)
    or else what was sent, against everything it sold.
    """
    sent = sold = 0.0
    counted = 0
    for group in groups:
        first = min((d for r in group
                     if (d := analytics._parse_date(r.get("date_received")) or analytics.row_date(r))),
                    default=None)
        if first is None or (last - first).days < SETTLED_AFTER_DAYS:
            continue
        booked = max((int(r["qty_amended"]) for r in group if r.get("qty_amended") is not None),
                     default=None)
        base = float(booked) if booked is not None else analytics.cartons_sent(group)
        if base <= 0:
            continue
        sent += base
        sold += sum(analytics.row_cartons(r) for r in group)
        counted += 1
    if not sent:
        return None, 0
    return round(min(sold / sent, 1.0), 4), counted


def _destination(product_rows: list[dict], history: dict, cut: dict, typical_cut: float | None,
                 last: date) -> dict:
    """The figures for one product at one market and agent."""
    cartons = sum(analytics.row_cartons(r) for r in product_rows)
    value = sum(analytics.row_value(r) for r in product_rows)
    first = product_rows[0]
    market = (first.get("market") or "").strip() or None
    consignments = {analytics.consignment_key(r) for r in product_rows}
    # Sell-through and speed are about the whole consignment, so they are read
    # off every row it ever had, not only the ones inside the window.
    groups = [history[k] for k in consignments if k in history]
    through, through_from = _sell_through(groups, last)
    spans = [d for g in groups if (d := analytics.consignment_days_to_sell(g)) is not None]

    weighted = weight = 0.0
    for r in product_rows:
        if (avg := integrity.market_avg(r)) is not None and analytics.row_cartons(r) > 0:
            weighted += avg * analytics.row_cartons(r)
            weight += analytics.row_cartons(r)
    price = value / cartons if cartons else 0.0
    market_avg = weighted / weight if weight else None

    known_cut = cut.get(market or "")
    rate = known_cut["rate"] if known_cut else typical_cut
    nett_per_carton = price * (1 - rate) if rate is not None else None
    back = (nett_per_carton * through if nett_per_carton is not None and through is not None
            else nett_per_carton)
    return {
        "market": market,
        "market_agent": (first.get("market_agent") or "").strip() or None,
        "cartons": round(cartons, 2),
        "value": round(value, 2),
        "price": round(price, 2),
        "market_avg": round(market_avg, 2) if market_avg is not None else None,
        "vs_market": round(price / market_avg, 4) if market_avg else None,
        "sell_through": through,
        "sell_through_from": through_from,
        "days_to_sell": round(median(spans), 1) if spans else None,
        "agent_cut": rate,
        "agent_cut_known": known_cut is not None,
        "nett_per_carton": round(nett_per_carton, 2) if nett_per_carton is not None else None,
        "back_per_carton": round(back, 2) if back is not None else None,
        "consignments": len(consignments),
        "enough": len(consignments) >= MIN_CONSIGNMENTS,
    }


def _verdict(destinations: list[dict]) -> dict:
    """What the comparison says for one product, in one of four kinds.

    ``best``: two or more destinations with enough history, and one returns
    more per carton sent. ``only``: sent to one place only, so there is nothing
    to compare yet. ``thin``: more than one place, but not enough history at
    them to call it. ``unknown``: the figures needed are missing.
    """
    scored = [d for d in destinations if d["back_per_carton"] is not None]
    if len(destinations) == 1:
        d = destinations[0]
        return {"kind": "only", "market": d["market"], "market_agent": d["market_agent"],
                "back_per_carton": d["back_per_carton"]}
    solid = sorted((d for d in scored if d["enough"]), key=lambda d: -d["back_per_carton"])
    if len(solid) >= 2:
        best, runner = solid[0], solid[1]
        return {"kind": "best", "market": best["market"], "market_agent": best["market_agent"],
                "back_per_carton": best["back_per_carton"],
                "lead_per_carton": round(best["back_per_carton"] - runner["back_per_carton"], 2),
                "runner_up": runner["market"], "runner_up_agent": runner["market_agent"]}
    if not scored:
        return {"kind": "unknown"}
    lead = max(scored, key=lambda d: d["back_per_carton"])
    return {"kind": "thin", "market": lead["market"], "market_agent": lead["market_agent"],
            "back_per_carton": lead["back_per_carton"]}


def build(rows: list[dict], payments: list[dict], months: int = DEFAULT_MONTHS) -> dict:
    """The whole comparison, grouped by fruit, biggest product first."""
    lo, hi = _window(rows, months)
    if hi is None:
        return {"window": None, "fruits": [], "caveats": []}
    scoped = [r for r in rows if (d := analytics.row_date(r)) and lo <= d <= hi]
    history: dict = defaultdict(list)
    for r in rows:
        history[analytics.consignment_key(r)].append(r)

    cut = agent_cut(payments, lo, hi)
    all_rates = [c["rate"] for c in cut.values()]
    typical = round(median(all_rates), 4) if all_rates else None

    by_product: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in scoped:
        where_to = ((r.get("market") or "").strip(), (r.get("market_agent") or "").strip())
        by_product[analytics.product_label(r)][where_to].append(r)

    fruits: dict[str, list[dict]] = defaultdict(list)
    for product, places in by_product.items():
        dests = [_destination(v, history, cut, typical, hi) for v in places.values()]
        dests = [d for d in dests if d["cartons"] > 0]
        cartons = sum(d["cartons"] for d in dests)
        if cartons < MIN_CARTONS:
            continue
        dests.sort(key=lambda d: (not d["enough"], -(d["back_per_carton"] or 0)))
        fruits[analytics.product_type(product)].append({
            "product": product,
            "value": round(sum(d["value"] for d in dests), 2),
            "cartons": round(cartons, 2),
            "verdict": _verdict(dests),
            "destinations": dests,
        })

    out = []
    for label, products in fruits.items():
        products.sort(key=lambda p: -p["value"])
        out.append({"label": label, "value": round(sum(p["value"] for p in products), 2),
                    "products": products,
                    "clear": sum(1 for p in products if p["verdict"]["kind"] == "best")})
    out.sort(key=lambda f: -f["value"])

    caveats = [
        "Each product is compared only against itself: the same market name, class and "
        "pack at every destination, so a better grade elsewhere is not mistaken for a "
        "better market.",
        f"How much sold counts only consignments at least {SETTLED_AFTER_DAYS} days old, "
        "so fruit still on the floor does not count against where it was sent.",
    ]
    if any(not c for c in (cut.get(d["market"] or "") for f in out for p in f["products"]
                           for d in p["destinations"])):
        caveats.append("Where a market has no payment in the period, the agent's cut is "
                       "taken as the book's usual rate and marked as such.")
    return {
        "window": {"from": lo.isoformat(), "to": hi.isoformat(), "months": months},
        "fruits": out,
        "agent_cut": cut,
        "typical_cut": typical,
        "caveats": caveats,
        "rules": {"min_consignments": MIN_CONSIGNMENTS, "settled_after_days": SETTLED_AFTER_DAYS},
    }
