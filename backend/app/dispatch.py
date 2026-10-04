"""Where each carton of the order goes: the plan split by market and agent.

The plan says how much of a product to take on and names the market that
returns it best. That market can only take so much before the fruit sits and
the price goes, so one name per product was the right answer only for products
that ever went to one place. This splits each product's order across the
markets it has sold at, filling the one that returns most per carton first, up
to what that market has actually taken of it in the time being ordered for,
then the next. Whatever is left beyond what any of them has taken goes to the
best of them and is marked as more than it usually takes, so the person
loading the truck can see the stretch rather than discover it.

Each market also carries how it pays: how long its agent takes from sale to
money, and what it owes now and for how long. A market sitting on old money is
still a good market for fruit, but the person sending it should know before
another load goes.

As in the rest of the plan, every rand is what the market is expected to
return. Nothing here knows what fruit costs.
"""

from __future__ import annotations

from datetime import date

from . import analytics, forecast, tracking

# A market that sold less than this share of what it was sent is not one to
# split an order into: the fruit sat there.
MIN_THROUGH = 0.6
# A market that sold out and cleared within this many days was asking for
# more; it is given this much room above what it took.
ASKED_FOR_MORE_DAYS = 3
ASKED_FOR_MORE = 1.2
# A share smaller than this many cartons is not worth a separate load; it
# goes with the rest to the best market.
MIN_SPLIT = 5
# Cartons over what a market has taken that are only rounding, not a stretch.
OVER_SLACK = 2
# Money owed longer than this is worth saying before another load goes.
OLD_DEBT_DAYS = 30
SLOW_PAYER_DAYS = 14


def _rand(value: float) -> str:
    return "R " + f"{value:,.2f}".replace(",", " ").replace(".", ",")


def monthly_take(rows: list[dict], window: dict | None) -> dict[tuple, float]:
    """The most cartons of each product each market has taken in one month.

    The month a market proved it could take, not its average: the order is
    sized from the months the product is selling now, and an average across a
    quiet month or two called nearly every line more than its market takes.
    """
    lo = (window or {}).get("from")
    hi = (window or {}).get("to")
    by: dict[tuple, dict[str, float]] = {}
    for r in rows:
        day = analytics.selling_day(r)
        if not day or (lo and day < lo) or (hi and day > hi):
            continue
        key = (r.get("product"), (r.get("market") or "").strip() or None,
               (r.get("market_agent") or "").strip() or None)
        months = by.setdefault(key, {})
        months[day[:7]] = months.get(day[:7], 0.0) + analytics.row_cartons(r)
    return {k: max(v.values()) for k, v in by.items() if v}


def _window_days(window: dict | None, months: int) -> int:
    try:
        lo = date.fromisoformat(window["from"])
        hi = date.fromisoformat(window["to"])
        return max((hi - lo).days + 1, 1)
    except (TypeError, KeyError, ValueError):
        return max(months, 1) * 30


def _why(d: dict, takes: float, days: int, asked: bool) -> str:
    """Why this market, in the words the person loading the truck needs: does
    it sell what it gets, how fast, and how much it has shown it can take."""
    bits = []
    if d.get("sell_through") is not None:
        bits.append(f"sold {d['sell_through'] * 100:.0f}% of what it got")
    if d.get("days_to_sell") is not None:
        bits.append(f"clears in {d['days_to_sell']:.0f} day{'' if d['days_to_sell'] == 1 else 's'}")
    bits.append(f"takes up to {round(takes)} cartons in {days} day{'' if days == 1 else 's'}"
                + (", and asked for more" if asked else ""))
    return ", ".join(bits)


def split(line: dict, window_days: int, horizon_days: int, horizon: str,
          peak: dict[tuple, float] | None = None) -> list[dict]:
    """One product's order across the markets that sell it."""
    want = int(line.get("take_on") or 0)
    if want <= 0:
        return []
    scale = horizon_days / window_days
    month_scale = horizon_days / 30
    fit = []
    for d in line.get("destinations") or []:
        if d.get("back_per_carton") is None or not d.get("cartons") or d["cartons"] <= 0:
            continue
        if d.get("sell_through") is not None and d["sell_through"] < MIN_THROUGH:
            continue
        asked = ((d.get("sell_through") or 0) >= 0.98
                 and d.get("days_to_sell") is not None
                 and d["days_to_sell"] <= ASKED_FOR_MORE_DAYS)
        best_month = (peak or {}).get((line["product"], d["market"], d.get("market_agent")))
        base = best_month * month_scale if best_month else d["cartons"] * scale
        takes = base * (ASKED_FOR_MORE if asked else 1.0)
        fit.append((d, takes, asked))
    fit.sort(key=lambda f: -f[0]["back_per_carton"])

    if not fit:
        # Nothing to split by: it goes where the plan already says, as one load.
        return [{"market": line.get("market") or "No destination on record",
                 "market_agent": line.get("market_agent"), "cartons": want, "over": 0,
                 "price": None, "back_per_carton": line.get("back_per_carton"),
                 "value": None,
                 "why": ("the only market it has been to" if line.get("market")
                         else "no market on record has sold it well enough to split by")}]

    out = []
    left = want
    for d, takes, asked in fit:
        if left <= 0:
            break
        n = min(left, int(takes))
        # Too small to be a load of its own: it rides with the best market,
        # unless it is the whole order.
        if n < MIN_SPLIT and (out or n < left):
            continue
        if n <= 0:
            continue
        out.append({"market": d["market"], "market_agent": d.get("market_agent"),
                    "cartons": n, "over": 0, "price": d.get("price"),
                    "back_per_carton": d["back_per_carton"],
                    "why": _why(d, takes, horizon_days, asked), "_takes": takes})
        left -= n
    if left > 0:
        # More than any of them has taken: the rest goes with the best market,
        # and the sheet says it is beyond what that market usually takes.
        best = out[0] if out else None
        if best is None:
            d, takes, asked = fit[0]
            best = {"market": d["market"], "market_agent": d.get("market_agent"),
                    "cartons": 0, "over": 0, "price": d.get("price"),
                    "back_per_carton": d["back_per_carton"],
                    "why": _why(d, takes, horizon_days, asked), "_takes": takes}
            out.append(best)
        best["cartons"] += left
    for a in out:
        # Beyond what this market has taken in the time: said, not hidden. A
        # carton or two over is the horizon's rounding, not a stretch.
        takes = a.pop("_takes")
        excess = a["cartons"] - int(takes)
        a["over"] = excess if excess > max(OVER_SLACK, takes * 0.1) else 0
        a["value"] = round(a["cartons"] * a["price"], 2) if a.get("price") else None
    return out


def market_risk(rows: list[dict], payments: list[dict]) -> dict[tuple, dict]:
    """How each market and agent pays: days from sale to money, what it owes
    now and how old the oldest of it is."""
    lag = {r["market_agent"]: r for r in forecast.payment_lag(rows, payments)}
    status = tracking.payment_status(rows, payments)
    out: dict[tuple, dict] = {}
    for m in status.get("outstanding_markets") or []:
        for line in m.get("lines") or []:
            key = (line.get("market"), line.get("market_agent"))
            r = out.setdefault(key, {"owed": 0.0, "oldest": None})
            r["owed"] += line.get("owed", 0.0)
            since = line.get("unpaid_since") or line.get("date")
            if since and (r["oldest"] is None or since < r["oldest"]):
                r["oldest"] = since
    agents = {(r.get("market"), r.get("market_agent")) for r in rows}
    today = date.today()
    result = {}
    for key in agents | set(out):
        owed = out.get(key, {})
        oldest = owed.get("oldest")
        days_old = (today - date.fromisoformat(oldest[:10])).days if oldest else None
        pays = lag.get(key[1] or "")
        note = []
        if days_old is not None and days_old > OLD_DEBT_DAYS:
            note.append(f"{_rand(owed['owed'])} owed, the oldest {days_old} days: chase it "
                        f"before sending more")
        if pays and pays["days_to_pay"] > SLOW_PAYER_DAYS:
            note.append(f"slow to pay: {pays['days_to_pay']:.0f} days from sale to money")
        result[key] = {
            "owed": round(owed.get("owed", 0.0), 2),
            "oldest_days": days_old,
            "days_to_pay": pays["days_to_pay"] if pays else None,
            "note": "; ".join(note) or None,
        }
    return result


def build(plan: dict, rows: list[dict], payments: list[dict], months: int) -> dict:
    """Split every line of `plan` and group the result by market and agent.

    Puts each line's split on it as `send`, and returns the sheet: one entry
    per market and agent, with what goes there and how that market pays.
    """
    window_days = _window_days(plan.get("window"), months)
    horizon = plan.get("horizon") or {}
    days = int(horizon.get("days") or 30)
    label = horizon.get("label") or f"{days} days"
    risk = market_risk(rows, payments)
    peak = monthly_take(rows, plan.get("window"))

    groups: dict[tuple, dict] = {}

    def group(market, agent):
        key = (market, agent)
        if key not in groups:
            r = risk.get(key, {})
            groups[key] = {"market": market, "market_agent": agent, "lines": [],
                           "cartons": 0, "value": 0.0, "over": 0, **{
                               k: r.get(k) for k in ("owed", "oldest_days", "days_to_pay", "note")}}
        return groups[key]

    for line in plan.get("lines") or []:
        line["send"] = split(line, window_days, days, label, peak)
        for a in line["send"]:
            g = group(a["market"], a.get("market_agent"))
            g["lines"].append({"product": line["product"], "fruit": line.get("fruit"),
                               "priority": line.get("priority"), "kind": "order", **a})
            g["cartons"] += a["cartons"]
            g["value"] += a.get("value") or 0.0
            g["over"] += a.get("over") or 0
        # Room to grow and a test load are optional, and listed as such.
        if (h := line.get("headroom")) and h.get("market"):
            g = group(h["market"], h.get("market_agent"))
            g["lines"].append({"product": line["product"], "fruit": line.get("fruit"),
                               "priority": line.get("priority"), "kind": "stretch",
                               "cartons": h["cartons"], "over": 0, "value": h.get("worth"),
                               "price": None, "back_per_carton": line.get("back_per_carton"),
                               "why": ", ".join(h.get("why") or [])})
        if (t := line.get("trial")) and t.get("market"):
            g = group(t["market"], t.get("market_agent"))
            g["lines"].append({"product": line["product"], "fruit": line.get("fruit"),
                               "priority": line.get("priority"), "kind": "test",
                               "cartons": t["cartons"], "over": 0, "value": None,
                               "price": None, "back_per_carton": None,
                               # What a test is worth is the gain over where the
                               # product goes now, not what it returns, so it is
                               # said in words rather than in the money column.
                               "worth": t.get("worth"),
                               "why": (t.get("why") or "") + (
                                   f"; about {_rand(t['worth'])} more than sending these "
                                   f"cartons to {line.get('market')}"
                                   if t.get("worth") and line.get("market") else "")})

    order = {"critical": 0, "high": 1, "steady": 2, "hold": 3}
    kinds = {"order": 0, "stretch": 1, "test": 2}
    sheet = []
    for g in groups.values():
        if not g["lines"]:
            continue
        g["lines"].sort(key=lambda l: (kinds[l["kind"]], order.get(l.get("priority"), 9),
                                       -l["cartons"]))
        g["value"] = round(g["value"], 2)
        g["optional"] = sum(l["cartons"] for l in g["lines"] if l["kind"] != "order")
        sheet.append(g)
    sheet.sort(key=lambda g: -g["cartons"])
    return {
        "markets": sheet,
        "cartons": sum(g["cartons"] for g in sheet),
        "optional": sum(g["optional"] for g in sheet),
        "split_lines": sum(1 for l in plan.get("lines") or [] if len(l.get("send") or []) > 1),
        "window_days": window_days,
    }
