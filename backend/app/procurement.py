"""The buy plan: what to take on next, how much of it, and where to send it.

This is the one screen that asks for a decision rather than reporting one. It
answers three things in the order a buyer asks them:

  1. What should I take on, most pressing first.
  2. How many cartons, given what is already sitting on the floor.
  3. Which market and agent to send it to when it arrives.

Three rules it works to, each learned the hard way on this book:

**Nothing here knows what fruit costs.** Zaco takes produce on consignment and
earns a commission on what the market returns, so there is no purchase price in
any report. Every figure below is what the MARKET is expected to return, never
a margin, and the screen has to say so.

**How much to take on is what is expected to sell, less what is already on the
floor.** A product with two hundred cartons sitting unsold at the market does
not need two hundred more, however well it normally does.

**A priority means the same thing every month.** The bands are fixed marks on
the score, not a ranking within the month, so a quiet month can have no
Critical lines at all and a strong one can be full of them. That is the point:
ranked bands always crowned somebody, and the top of a poor week read exactly
like the top of a good one. Thin history multiplies the score down rather than
capping the band, so a better-scoring line never appears below a worse one.
"""

from __future__ import annotations

from collections import defaultdict

from . import analytics, forecast, scorecard, tracking

# Weights on the four things that decide what to back again. Clearance leads:
# fruit that does not sell earns nothing and holds a slot on the floor.
#
# Every one of them is measured against something outside this month's plan.
# The money term used to be each line's rand back per carton over the best
# line's, which made it a ranking wearing a score's clothes: a plan with one
# line gave that line full marks, and the same product scored differently in a
# month with a stronger line in it. It is now what a carton fetched against
# what the market itself was paying for that commodity, where the report says.
WEIGHTS = {"clearance": 0.30, "price": 0.30, "speed": 0.20, "direction": 0.20}

# Fetching the market average scores full marks on price; half of it scores
# nothing. Where the report never printed a Market Avg there is nothing to
# compare against, and the line takes par rather than a guess either way.
AT_MARKET, PAR = 1.0, 0.5

# Days to clear that counts as fast. Beyond it, speed scores nothing.
SLOW_DAYS = 14.0

# Fixed marks on the score, the same every month:
#   0.85  nearly everything going for it: what is sent sells, it goes within a
#         couple of days, it fetches about what the market is paying, and it is
#         not falling away
#   0.75  strong on most of those, one of them middling
#   0.60  it trades well enough; take it when it is offered, do not chase it
# Measured against this book, whose scores run 0.51 to 1.00: 7 lines of 56 in
# the top band, 15 in the next, 29 steady, 5 on hold. A weak month puts nothing
# in the top band at all, which is what fixed marks are for.
CRITICAL_AT, HIGH_AT, STEADY_AT = 0.85, 0.75, 0.60

# Thin evidence multiplies the score down: one month traded is not one third of
# a reason, but it is not a full one either.
THIN_BASE, THIN_STEP = 0.6, 0.2


def _direction(basis: dict[str, float]) -> tuple[str, float]:
    """Which way a product is going, from the months it actually traded."""
    months = sorted(basis)
    if len(months) < 2:
        return "new", 0.6
    last, before = basis[months[-1]], basis[months[-2]]
    if before <= 0:
        return "steady", 0.6
    change = (last - before) / before
    if change >= 0.15:
        return "rising", 1.0
    if change <= -0.15:
        return "falling", 0.2
    return "steady", 0.6


def on_hand_by_product(rows: list[dict]) -> dict[str, float]:
    """Cartons of each product still on the floor, counted once per consignment."""
    out: dict[str, float] = defaultdict(float)
    for group in analytics.group_consignments(rows):
        sent, left = tracking.on_hand(group)
        if sent > 0 and left > 0:
            out[analytics.product_label(group[0])] += left
    return dict(out)


def band(score: float) -> str:
    """Which band a score falls in. The same score is the same band in any
    month, so the plan can be compared with last month's."""
    if score >= CRITICAL_AT:
        return "critical"
    if score >= HIGH_AT:
        return "high"
    if score >= STEADY_AT:
        return "steady"
    return "hold"


def _reasons(line: dict) -> list[str]:
    """Why this line sits where it does, in the operator's own figures."""
    out = []
    if line["sell_through"] is not None:
        out.append(f"{line['sell_through'] * 100:.0f}% of what was sent sold")
    if line["days_to_clear"] is not None:
        out.append("clears the same day" if line["days_to_clear"] < 1
                   else f"clears in {line['days_to_clear']:.0f} days")
    if line["vs_market"] is not None:
        out.append(f"fetches {line['vs_market'] * 100:.0f}% of the market average")
    if line["back_per_carton"] is not None:
        out.append(f"R {line['back_per_carton']:,.2f} back a carton"
                   .replace(",", " ").replace(".", ","))
    out.append({"rising": "up on last month", "falling": "down on last month",
                "steady": "level with last month",
                "new": "only one month traded"}[line["direction"]])
    if line["on_hand"]:
        out.append(f"{line['on_hand']:.0f} already on the floor")
    if not line["take_on"] and line["on_hand"]:
        out.append("covered by what is already there")
    return out


def build(rows: list[dict], payments: list[dict], months: int = scorecard.DEFAULT_MONTHS,
          today=None) -> dict:
    """The plan: every product worth taking on, ranked, with its destination."""
    projection = forecast.project(rows, today)
    card = scorecard.build(rows, payments, months)
    velocity = {v["product"]: v for v in forecast.velocity(rows)}
    floor = on_hand_by_product(rows)

    where: dict[str, dict] = {}
    for fruit in card.get("fruits", []):
        for p in fruit["products"]:
            where[p["product"]] = p

    lines: list[dict] = []
    for p in projection["products"]:
        product = p["product"]
        v = velocity.get(product, {})
        placed = where.get(product, {})
        verdict = placed.get("verdict", {})
        best = next((d for d in placed.get("destinations", [])
                     if d["market"] == verdict.get("market")), None)
        on_floor = round(floor.get(product, 0.0))
        expected = p["cartons_estimate"]
        direction, direction_score = _direction(p["basis"])
        lines.append({
            "product": product,
            "fruit": analytics.product_type(product),
            # What the market is expected to return next month, not a margin.
            "expected_cartons": expected,
            "expected_value": p["estimate"],
            "expected_low": p["low"],
            "expected_high": p["high"],
            "confidence": p["confidence"],
            "months_used": p["months_used"],
            "on_hand": on_floor,
            "take_on": max(round(expected - on_floor), 0),
            "sell_through": v.get("sell_through"),
            "days_to_clear": v.get("days_to_clear"),
            "direction": direction,
            "_direction_score": direction_score,
            "market": verdict.get("market"),
            "market_agent": verdict.get("market_agent"),
            "where_kind": verdict.get("kind", "unknown"),
            "lead_per_carton": verdict.get("lead_per_carton"),
            "back_per_carton": verdict.get("back_per_carton"),
            "vs_market": (best or {}).get("vs_market"),
            "destinations": placed.get("destinations", []),
        })

    if lines:
        for line in lines:
            clearance = line["sell_through"] if line["sell_through"] is not None else PAR
            days = line["days_to_clear"]
            speed = 1 - min(days / SLOW_DAYS, 1.0) if days is not None else PAR
            vs = line["vs_market"]
            price = PAR if vs is None else max(min((vs - 0.5) / (AT_MARKET - 0.5), 1.0), 0.0)
            raw = (WEIGHTS["clearance"] * min(clearance, 1.0)
                   + WEIGHTS["price"] * price
                   + WEIGHTS["speed"] * speed
                   + WEIGHTS["direction"] * line["_direction_score"])
            thin = min(THIN_BASE + THIN_STEP * len(line["months_used"]), 1.0)
            line["score"] = round(raw * thin, 4)
            line.pop("_direction_score")
        lines.sort(key=lambda l: -l["score"])
        for line in lines:
            line["priority"] = band(line["score"])
            line["reasons"] = _reasons(line)

    # Grouped for the screen's dropdowns: priority first, fruit within it.
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        groups[line["priority"]].append(line)
    # A line already covered by stock on the floor keeps its priority -- it is
    # still a good line -- but there is nothing to act on, so it sits under the
    # ones there is.
    for in_band in groups.values():
        in_band.sort(key=lambda l: (l["take_on"] == 0, -l["score"]))

    take = [l for l in lines if l["take_on"] > 0]
    return {
        "month": projection["month"],
        "window": card.get("window"),
        "lines": lines,
        "priorities": [{"key": k, "lines": groups[k],
                        "cartons": round(sum(l["take_on"] for l in groups[k])),
                        "expected_value": round(sum(l["expected_value"] for l in groups[k]), 2)}
                       for k in ("critical", "high", "steady", "hold") if groups[k]],
        "totals": {
            "lines": len(lines),
            "to_take_on": len(take),
            "cartons": round(sum(l["take_on"] for l in take)),
            "expected_value": round(sum(l["expected_value"] for l in lines), 2),
            "on_hand": round(sum(l["on_hand"] for l in lines)),
            "no_destination": sum(1 for l in lines if l["where_kind"] in ("unknown", "thin")),
            "untested": sum(1 for l in lines if l["where_kind"] == "only"),
        },
        "bands": {"critical": CRITICAL_AT, "high": HIGH_AT, "steady": STEADY_AT},
        "caveats": projection["caveats"] + [
            "Priorities are fixed marks on the score, not a ranking within the month, "
            "so a quiet month can have nothing in the top band and a strong one can be "
            "full of it.",
            "Nothing here knows what the fruit costs: Zaco takes it on consignment and "
            "earns a commission on what the market returns. Every rand figure is what "
            "the market is expected to return, not a margin.",
            "How much to take on is what is expected to sell next month less what is "
            "still on the floor now.",
        ] + card.get("caveats", []),
        "agent_cut": card.get("agent_cut", {}),
        # How long each agent takes to pay, so a plan can be read as cash.
        "payment_lag": forecast.payment_lag(rows, payments),
    }
