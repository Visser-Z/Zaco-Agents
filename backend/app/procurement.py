"""What to take on next: which lines are worth accepting from suppliers.

Zaco does not buy stock, so this is not a shopping list and there is no budget
to divide. Produce is taken on consignment and placed with a market agent; Zaco
earns a commission percentage of the Nett that comes back. What is scarce is
not cash, it is the market slots, the handling, and the supplier relationships
spent on produce that then fails to move.

So the question this answers is: **of everything a supplier could offer, which
lines actually clear, and which of them earn Zaco the most commission per
carton handled.**

**The levels are computed, not written by a model.** Same history in, same
levels out, every time, and every score can be traced to the figures beside it.

What it can and cannot say
--------------------------
Without agreed terms it ranks on how well produce SELLS: how much of what was
sent actually sold, how fast it moved, what a carton fetched, how much it
brought in, and whether it is still in season. Those are proxies. Once a
commission has been recorded against a line, the ranking uses what Zaco
ACTUALLY EARNED on it instead. Every payload says which of the two it is; see
``basis``.

Note on clearance: unsold stock is the SUPPLIER's loss on consignment, not
Zaco's, so it is not a cash risk here. It still ranks heavily, because produce
that does not move earns no commission and burns a market slot that something
else could have used.
"""

from __future__ import annotations

from datetime import date

from . import analytics, consignment

# How the signals are weighted before any commission has been agreed. Clearance
# first: produce that does not move earns nothing and ties up a market slot.
# These are all PROXIES for what Zaco will earn.
WEIGHTS = {
    "clearance": 0.30,   # share of what was sent that actually sold
    "speed": 0.25,       # how quickly it moved
    "price": 0.20,       # what a carton fetched
    "earnings": 0.15,    # how much it brought in overall
    "recency": 0.10,     # is it still selling, or is its season over
}

# Once terms exist, commission per carton is not a proxy for Zaco's earnings --
# it IS the earnings, and it already contains the sale price and the agreed rate
# together. So it leads and the proxies drop back to a supporting role.
WEIGHTS_WITH_TERMS = {
    "commission": 0.45,  # what Zaco actually earned per carton sold
    "clearance": 0.15,
    "speed": 0.15,
    "price": 0.05,
    "earnings": 0.10,
    "recency": 0.10,
}

# A consignment taking this long or longer scores nothing for speed.
SLOW_DAYS = 14
# Sales more recent than this count as current.
CURRENT_DAYS = 45
# Below this many consignments the evidence is thin (most lines start here).
MIN_CONSIGNMENTS = 2

CRITICAL, HIGH, LOW = "critical", "high", "low"

# Bands are assigned by rank, not by an absolute score. Scores across a real
# season sit in a narrow range (0.3 to 0.7 on three months of this business),
# so fixed cut-offs put nearly everything in one band and the list stops being
# a priority list. Shares are of the products with any history.
CRITICAL_SHARE = 0.15
HIGH_SHARE = 0.35
# Floors so a weak season cannot promote a bad line just for being least bad.
CRITICAL_FLOOR = 0.50
HIGH_FLOOR = 0.38


def _confidence(consignments: int) -> float:
    """One consignment is a data point, not a pattern.

    Applied to the score rather than as a band cap, so the ranking and the
    bands never disagree -- a higher-scoring line sitting in a lower band reads
    as a bug even when the reason is sound.
    """
    return min(1.0, 0.6 + 0.2 * consignments)


def _assign_bands(scored: list[dict]) -> None:
    """Band by rank, subject to a floor. `scored` must be sorted, best first."""
    n = len(scored)
    critical_cut = max(1, round(n * CRITICAL_SHARE))
    high_cut = critical_cut + max(1, round(n * HIGH_SHARE))
    for i, item in enumerate(scored):
        if i < critical_cut and item["score"] >= CRITICAL_FLOOR:
            item["level"] = CRITICAL
        elif i < high_cut and item["score"] >= HIGH_FLOOR:
            item["level"] = HIGH
        else:
            item["level"] = LOW


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _latest_sale(rows: list[dict]) -> date | None:
    dates = [d for r in rows if (d := analytics.row_date(r)) is not None]
    return max(dates) if dates else None


def recommend(
    rows: list[dict],
    today: date | None = None,
    deals: dict[tuple, dict] | None = None,
) -> dict:
    """Rank products by whether they are worth taking on.

    `deals` maps (supplier_ref, PRODUCT) to the agreed consignment terms. Supply
    it and the ranking uses commission actually earned rather than a proxy for
    it. There is no budget parameter: Zaco pays nothing to acquire stock, so
    there is no sum to divide.
    """
    today = today or date.today()
    deals = deals or {}
    if not rows:
        return {
            "recommendations": [],
            "earnings": {"commission": 0.0, "owed_to_suppliers": 0.0, "source": "no history yet"},
            "basis": _BASIS,
            "termed": {"products": 0, "of": 0, "ranked_on_commission": False},
            "history": {"months": 0, "consignments": 0},
        }

    perf = analytics.product_performance(rows)
    by_product: dict[str, list[dict]] = {}
    for row in rows:
        by_product.setdefault(analytics.product_label(row), []).append(row)

    # Scales for the relative signals. Using the observed maximum keeps the
    # comparison inside this business rather than against an outside benchmark.
    top_price = max((p["avg_price"] for p in perf), default=0.0) or 1.0
    top_value = max((p["value"] for p in perf), default=0.0) or 1.0

    # Commission has to be scaled against the best-earning line, so it needs a
    # pass of its own before anything can be scored against it.
    earned_by_product = {
        p["label"]: consignment.expected_commission(by_product.get(p["label"], []), deals, p["label"])
        for p in perf
    }
    top_commission = max(
        (e["per_carton"] for e in earned_by_product.values() if e), default=0.0
    ) or 1.0

    scored: list[dict] = []
    for p in perf:
        product_rows = by_product.get(p["label"], [])
        last = _latest_sale(product_rows)

        clearance = _clamp(p["sell_through"]) if p["sell_through"] is not None else 0.5
        if p["avg_days_to_sell"] is None:
            speed = 0.5                                  # unknown, not assumed good
        else:
            speed = _clamp(1.0 - (p["avg_days_to_sell"] / SLOW_DAYS))
        price = _clamp(p["avg_price"] / top_price)
        earnings = _clamp(p["value"] / top_value)
        if last is None:
            recency = 0.0
        else:
            age = (today - last).days
            recency = _clamp(1.0 - (age / CURRENT_DAYS))

        parts = {
            "clearance": clearance, "speed": speed, "price": price,
            "earnings": earnings, "recency": recency,
        }

        # What Zaco actually earned, where terms have been agreed. Scaled
        # against the best earner so the comparison stays inside this business.
        earning = earned_by_product.get(p["label"])
        if earning is not None:
            parts["commission"] = _clamp(earning["per_carton"] / top_commission)
            weights = WEIGHTS_WITH_TERMS
        else:
            weights = WEIGHTS

        # Discounted by how much evidence stands behind it.
        score = sum(weights[k] * v for k, v in parts.items()) * _confidence(p["consignments"])

        scored.append(
            {
                "product": p["label"],
                "level": LOW,            # replaced by _assign_bands once ranked
                "score": round(score, 3),
                "earning": earning,
                "ranked_on_commission": earning is not None,
                "signals": {k: round(v, 3) for k, v in parts.items()},
                "reasons": _reasons(p, parts, last, today, earning),
                "sell_through": p["sell_through"],
                "avg_days_to_sell": p["avg_days_to_sell"],
                "slowest_days": p["slowest_days"],
                "avg_price": p["avg_price"],
                "value": p["value"],
                "cartons": p["cartons"],
                "consignments": p["consignments"],
                "months_seen": p["months_seen"],
                "last_sold": last.isoformat() if last else None,
                "thin_evidence": p["consignments"] < MIN_CONSIGNMENTS,
                "best_market": _best(product_rows, "market"),
                "best_agent": _best(product_rows, "market_agent"),
                "destination": _destination(product_rows),
            }
        )

    scored.sort(key=lambda d: d["score"], reverse=True)
    _assign_bands(scored)
    _allocate(scored)

    months = sorted({m for p in perf for m in p["months_seen"]})
    termed = sum(1 for s in scored if s["ranked_on_commission"])
    settlement = consignment.settle(rows, deals)
    return {
        "recommendations": scored,
        "earnings": {
            "commission": settlement["commission_earned"],
            "owed_to_suppliers": settlement["owed_to_suppliers"],
            "nett_received": settlement["nett_received"],
            "source": (f"{len(settlement['lines'])} consignment(s) with agreed terms"
                       if settlement["lines"] else "no consignment terms recorded yet"),
        },
        "basis": _BASIS_WITH_TERMS if termed else _BASIS,
        "termed": {
            "products": termed,
            "of": len(scored),
            "ranked_on_commission": termed > 0,
        },
        "history": {
            "months": len(months),
            # Rows are account sales; a consignment usually spans several, so
            # counting rows here would overstate how much history there is.
            "consignments": len(analytics.group_consignments(rows)),
            "from": months[0] if months else None,
            "to": months[-1] if months else None,
        },
    }


_BASIS = (
    "Ranked on how well each line sells: how much of what was sent actually "
    "sold, how fast it moved, what a carton fetched, what it brought in, and "
    "whether it is still in season. No commission has been agreed on these "
    "lines yet, so this cannot rank by what you EARN — record the terms on a "
    "consignment and that line switches to real commission."
)

_BASIS_WITH_TERMS = (
    "Ranked on the commission you actually earned, where the terms are on "
    "record: your percentage of the Nett that came back after the market "
    "agent's deductions. Lines with no agreed commission are still ranked on "
    "sales performance alone and are marked as such."
)


def _reasons(p: dict, parts: dict, last: date | None, today: date,
             earning: dict | None = None) -> list[str]:
    """Plain statements of why a line scored as it did, in weight order."""
    out: list[str] = []
    if earning is not None:
        per = earning["per_carton"]
        out.append(
            f"earned you R {per:,.2f} a carton in commission".replace(",", " ")
        )
    if p["sell_through"] is not None:
        pct = f"{p['sell_through']:.0%}"
        out.append(
            f"{pct} of what was sent sold" if parts["clearance"] >= 0.8
            else f"only {pct} of what was sent sold"
        )
    if p["avg_days_to_sell"] is not None:
        if p["avg_days_to_sell"] == 0:
            out.append("clears the same day")
        elif p["avg_days_to_sell"] <= 2:
            out.append(f"sells in about {p['avg_days_to_sell']:g} day(s)")
        else:
            out.append(f"takes about {p['avg_days_to_sell']:g} days to sell")
    if p["slowest_days"] and p["slowest_days"] >= SLOW_DAYS:
        out.append(f"slowest consignment took {p['slowest_days']} days")
    out.append(f"averaged R {p['avg_price']:,.2f} a carton".replace(",", " "))
    if last is not None:
        age = (today - last).days
        if age > CURRENT_DAYS:
            out.append(f"nothing sold in {age} days — season may be over")
    if p["consignments"] < MIN_CONSIGNMENTS:
        out.append("only one consignment so far, so treat this as provisional")
    return out


def _by_place(rows: list[dict], field: str) -> list[dict]:
    """Realised price per carton for each place this product went, best first."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = r.get(field)
        if key:
            groups.setdefault(key, []).append(r)
    out = []
    for name, items in groups.items():
        cartons = sum(analytics.row_cartons(r) for r in items)
        if not cartons:
            continue
        price = sum(analytics.row_value(r) for r in items) / cartons
        out.append({"name": name, "avg_price": round(price, 2), "cartons": cartons})
    out.sort(key=lambda d: d["avg_price"], reverse=True)
    return out


def _best(rows: list[dict], field: str) -> dict | None:
    """Where this product fetched the best price, only when there was a choice."""
    places = _by_place(rows, field)
    return places[0] if len(places) > 1 else None


def _destination(rows: list[dict]) -> dict | None:
    """Where this product goes, and what it realised there.

    On consignment the destination belongs in the buy decision: it says which
    load the produce goes on, and the same carton earns differently depending
    on who sells it. So it is reported even when there is only ONE destination,
    because "this goes to Subtropico" is still an instruction.

    That distinction is not academic here. On this business's real history every
    single product has exactly one destination -- nothing is split across two
    markets or two agents -- so a "best market" comparison is empty on every
    line. Reporting only the best would have shown nothing at all, forever.
    `compared` says which of the two this is.
    """
    agents = _by_place(rows, "market_agent")
    markets = _by_place(rows, "market")
    if not agents and not markets:
        return None
    top_agent = agents[0] if agents else None
    top_market = markets[0] if markets else None
    return {
        "agent": top_agent["name"] if top_agent else None,
        "market": top_market["name"] if top_market else None,
        "avg_price": (top_agent or top_market)["avg_price"],
        # True only when there was more than one to pick from, so the UI never
        # implies a choice was made where none existed.
        "compared": len(agents) > 1 or len(markets) > 1,
        "options": max(len(agents), len(markets)),
    }


def _allocate(scored: list[dict]) -> None:
    """Mark how much of Zaco's earnings each line is worth chasing.

    There is no budget to divide: nothing is paid to acquire stock. What this
    apportions instead is ATTENTION -- the market slots, handling and supplier
    calls that are genuinely finite. Weighted by how far each line sits ABOVE
    the bar rather than by its raw score, because raw scores across a season sit
    in a narrow band and splitting on them hands every product a near-identical
    share, which is not a recommendation.

    Only Critical and High get a share: pushing effort into a Low line is the
    thing this list exists to prevent.
    """
    backable = [s for s in scored if s["level"] in (CRITICAL, HIGH)]
    weights = {id(s): max(s["score"] - HIGH_FLOOR, 0.01) ** 2 for s in backable}
    total = sum(weights.values())
    for s in scored:
        share = (weights[id(s)] / total) if (total and id(s) in weights) else 0.0
        s["share"] = round(share, 4)
