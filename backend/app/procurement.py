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

**A priority is a ranking, not a score with a meaning of its own.** Real scores
bunch together, so the bands are rank-based with a floor under them: the top of
a poor week is not automatically Critical. Thin history multiplies the score
down rather than capping the band, so a better-scoring line never appears below
a worse one.
"""

from __future__ import annotations

from collections import defaultdict

from . import analytics, forecast, scorecard, tracking

# Weights on the four things that decide what to back again. Clearance leads:
# fruit that does not sell earns nothing and holds a slot on the floor.
WEIGHTS = {"clearance": 0.30, "money": 0.30, "speed": 0.20, "direction": 0.20}

# Days to clear that counts as fast. Beyond it, speed scores nothing.
SLOW_DAYS = 14.0

# Rank bands, and the score a line must reach to hold each of the top two.
# Without the floors, the best of a bad week reads as Critical.
CRITICAL_SHARE, HIGH_SHARE, STEADY_SHARE = 0.15, 0.50, 0.80
CRITICAL_FLOOR, HIGH_FLOOR = 0.45, 0.33

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


def _band(rank: int, count: int, score: float) -> str:
    share = (rank + 1) / count
    # The leader is always eligible for the top band, so a short plan is not
    # capped at High purely because one line cannot be 15% of two.
    if (rank == 0 or share <= CRITICAL_SHARE) and score >= CRITICAL_FLOOR:
        return "critical"
    if share <= HIGH_SHARE and score >= HIGH_FLOOR:
        return "high"
    if share <= STEADY_SHARE:
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
        most = max((l["back_per_carton"] or 0) for l in lines) or 1.0
        for line in lines:
            clearance = line["sell_through"] if line["sell_through"] is not None else 0.5
            days = line["days_to_clear"]
            speed = 1 - min(days / SLOW_DAYS, 1.0) if days is not None else 0.5
            money = (line["back_per_carton"] or 0) / most
            raw = (WEIGHTS["clearance"] * min(clearance, 1.0)
                   + WEIGHTS["money"] * money
                   + WEIGHTS["speed"] * speed
                   + WEIGHTS["direction"] * line["_direction_score"])
            thin = min(THIN_BASE + THIN_STEP * len(line["months_used"]), 1.0)
            line["score"] = round(raw * thin, 4)
            line.pop("_direction_score")
        lines.sort(key=lambda l: -l["score"])
        for rank, line in enumerate(lines):
            line["priority"] = _band(rank, len(lines), line["score"])
            line["reasons"] = _reasons(line)

    # Grouped for the screen's dropdowns: priority first, fruit within it.
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        groups[line["priority"]].append(line)
    # A line already covered by stock on the floor keeps its priority -- it is
    # still a good line -- but there is nothing to act on, so it sits under the
    # ones there is.
    for band in groups.values():
        band.sort(key=lambda l: (l["take_on"] == 0, -l["score"]))

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
        "caveats": projection["caveats"] + [
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
