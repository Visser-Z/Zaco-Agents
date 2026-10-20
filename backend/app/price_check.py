"""Did each agent sell Zaco's fruit at the market's going price?

The Daily Sales Detail prints, on every docket, what the market averaged for
that commodity that day beside what Zaco's carton fetched. So "did this sell at
the going rate" has a figure behind it, per agent, per day.

A sale under the average is not always a bad sale. Fruit that has stood on the
floor for a week is sold under the average on purpose, to clear it before it
spoils: that is the agent doing the job. So the check only counts the first
days a consignment is on the market, ``fresh_days``, which is a setting (five
unless an admin changes it). Under the average inside that window is flagged,
with the money it fell short by. After it, the sale is clearance and is shown
but never flagged.

Day 1 is the first day the consignment sold, unless a CSV row gave the market's
own delivery date, which wins. The Daily Sales PDF prints no arrival date, so
fruit that stood a day or two before its first sale has its window start late.
That errs towards flagging, never away from it, and the screen says so.

The shortfall is reported at nett: the agent's deduction comes off first, and
what is left is the money Zaco and the grower would have shared. Zaco's own
share of it rides alongside. Both the cut and the share are assumptions written
into the answer rather than hidden in a figure.

Shown on Insights' sales per day, product by product (``by_day``); ``check``
sums the same sales per agent.
"""

from __future__ import annotations

from datetime import date

from . import analytics, integrity

DEFAULT_FRESH_DAYS = 5
MIN_FRESH_DAYS, MAX_FRESH_DAYS = 1, 30
# Zaco keeps this share of the nett; the grower gets the rest. 40% per the
# user on 20 October 2026, who was not certain whether it is 30% or 40%.
ZACO_SHARE = 0.40
# The agent's cut when the period's own paid sales cannot say what it is.
# Median of real June 2026 account sales was 15.0%.
DEFAULT_DEDUCTION = integrity.TYPICAL_RATE
# How many flagged sales the screen lists, biggest money first.
LINES_SHOWN = 25


def fresh_days_from(value) -> int:
    """A stored or posted setting, held inside the range the screen offers."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_FRESH_DAYS
    return min(max(days, MIN_FRESH_DAYS), MAX_FRESH_DAYS)


def _sold_on(row: dict) -> date | None:
    return analytics._parse_date(analytics.selling_day(row))


def _from_csv(row: dict) -> bool:
    return bool(row.get("payment_refs")) or ".csv" in str(row.get("source_file") or "").lower()


def _key(row: dict):
    """One consignment. Rows without a consignment id fall back to DN and product."""
    if row.get("consignment_id"):
        return ("c", int(row["consignment_id"]))
    return ("d", row.get("dn"), analytics.product_label(row))


def first_days(rows: list[dict]) -> dict:
    """Day 1 for every consignment in ``rows``.

    Read over the whole history, not the period on screen: a consignment that
    first sold on 29 September and sold again on 3 October is on its fifth
    day on the 3rd, whichever month is being looked at.
    """
    out: dict = {}
    delivered: dict = {}
    for row in rows:
        k = _key(row)
        if (d := _sold_on(row)) is not None and (k not in out or d < out[k]):
            out[k] = d
        if _from_csv(row) and (d := analytics._parse_date(row.get("date_received"))) is not None:
            if k not in delivered or d < delivered[k]:
                delivered[k] = d
    out.update(delivered)
    return out


def _deduction_by_agent(rows: list[dict]) -> dict[str, float]:
    """Each agent's usual cut, from its own paid sales in the rows given."""
    rates: dict[str, list[float]] = {}
    for row in rows:
        if (r := integrity.deduction_rate(row)) is not None and 0 <= r < integrity.SEVERE_RATE:
            rates.setdefault(row.get("market_agent") or "", []).append(r)
    out = {}
    for agent, values in rates.items():
        if len(values) >= integrity.MIN_FOR_MEDIAN:
            values.sort()
            out[agent] = values[len(values) // 2]
    return out


def _assess(history: list[dict], scoped: list[dict], fresh_days: int):
    """Each sale in ``scoped`` with its day on the floor and what it fell short by.

    Status is "flagged" (under the average inside the window), "clearance"
    (after it), "ok" (at or above the average in it) or "unpriced" (no average
    printed). ``short`` is rand of gross; ``nett`` is the same after the
    agent's cut, the money Zaco and the grower would have shared; ``zaco`` is
    Zaco's share of that.
    """
    starts = first_days(history)
    cuts = _deduction_by_agent(history)
    for row in scoped:
        cartons = analytics.row_cartons(row)
        if cartons <= 0:
            continue
        agent = row.get("market_agent") or "Unknown agent"
        cut = cuts.get(agent, DEFAULT_DEDUCTION)
        on = _sold_on(row)
        base = {"row": row, "agent": agent, "cartons": cartons, "deduction": cut,
                "deduction_basis": "own" if agent in cuts else "typical", "date": on}
        avg, realised = integrity.market_avg(row), integrity.realised_price(row)
        if avg is None or realised is None or realised <= 0:
            yield {**base, "status": "unpriced"}
            continue
        sold, at_market = analytics.row_value(row), cartons * avg
        day1 = starts.get(_key(row))
        day = (on - day1).days + 1 if day1 and on else 1
        short = max(at_market - sold, 0.0)
        status = "clearance" if day > fresh_days else "ok" if realised >= avg else "flagged"
        yield {**base, "status": status, "day": day, "sold": sold, "at_market": at_market,
               "avg": avg, "realised": realised, "short": short,
               "nett": short * (1 - cut), "zaco": short * (1 - cut) * ZACO_SHARE}


def _assumptions() -> dict:
    return {"zaco_share": ZACO_SHARE, "typical_deduction": DEFAULT_DEDUCTION,
            "day_one": "first day sold, or the market's delivery date where a CSV gave it"}


def check(history: list[dict], scoped: list[dict], fresh_days: int = DEFAULT_FRESH_DAYS) -> dict:
    """Every agent's prices against the market over ``scoped``.

    ``history`` is the whole book (for day 1 and the agents' usual cut);
    ``scoped`` is the period asked about.
    """
    fresh_days = fresh_days_from(fresh_days)
    agents: dict[str, dict] = {}
    lines: list[dict] = []
    priced = unpriced = 0
    for s in _assess(history, scoped, fresh_days):
        a = agents.setdefault(s["agent"], {
            "agent": s["agent"], "sold": 0.0, "at_market": 0.0, "cartons": 0.0,
            "fresh_sold": 0.0, "fresh_at_market": 0.0, "flagged": 0, "missed": 0.0,
            "missed_nett": 0.0, "zaco_missed": 0.0, "clearance_sold": 0.0,
            "clearance_under": 0.0, "unpriced": 0, "deduction": s["deduction"],
            "deduction_basis": s["deduction_basis"]})
        if s["status"] == "unpriced":
            a["unpriced"] += 1
            unpriced += 1
            continue
        priced += 1
        a["sold"] += s["sold"]
        a["at_market"] += s["at_market"]
        a["cartons"] += s["cartons"]
        if s["status"] == "clearance":
            a["clearance_sold"] += s["sold"]
            a["clearance_under"] += s["short"]
            continue
        a["fresh_sold"] += s["sold"]
        a["fresh_at_market"] += s["at_market"]
        if s["status"] != "flagged":
            continue
        a["flagged"] += 1
        a["missed"] += s["short"]
        a["missed_nett"] += s["nett"]
        a["zaco_missed"] += s["zaco"]
        row = s["row"]
        lines.append({
            "agent": s["agent"], "dn": row.get("dn"), "consignment_id": row.get("consignment_id"),
            "product": analytics.product_label(row), "market": row.get("market"),
            "date": s["date"].isoformat() if s["date"] else None, "day": s["day"],
            "cartons": int(s["cartons"]), "realised": round(s["realised"], 2),
            "market_avg": round(s["avg"], 2), "ratio": round(s["realised"] / s["avg"], 4),
            "missed": round(s["short"], 2), "missed_nett": round(s["nett"], 2),
        })

    out_agents = [{
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in a.items()},
        "share_of_market": round(a["sold"] / a["at_market"], 4) if a["at_market"] else None,
        "fresh_share_of_market": (round(a["fresh_sold"] / a["fresh_at_market"], 4)
                                  if a["fresh_at_market"] else None),
    } for a in agents.values()]
    out_agents.sort(key=lambda a: (-a["missed"], -a["sold"]))
    lines.sort(key=lambda line: -line["missed"])
    total = lambda k: round(sum(a[k] for a in out_agents), 2)  # noqa: E731
    return {
        "known": priced > 0,
        "fresh_days": fresh_days,
        "priced": priced, "unpriced": unpriced,
        "agents": out_agents,
        "lines": lines[:LINES_SHOWN],
        "flagged": len(lines),
        "totals": {k: total(k) for k in ("sold", "at_market", "fresh_sold", "fresh_at_market",
                                         "missed", "missed_nett", "zaco_missed",
                                         "clearance_sold", "clearance_under")},
        "assumptions": _assumptions(),
    }


def by_day(history: list[dict], fresh_days: int = DEFAULT_FRESH_DAYS) -> dict:
    """The same check laid out the way Sales per day reads: per day, per product.

    Keyed by the day sold and the product's market name, the two things that
    list groups on. One product line on one day can hold several consignments.

    Every line with a market average gets how it did against it: ``pct`` over
    (positive) or under (negative), and the rand difference before and after
    the agent's cut. That is the whole line, first days and clearance alike.
    The flag is narrower: ``short``/``nett``/``zaco`` and ``cartons`` cover only
    the sales under the average inside the first days. A line is "flagged" if
    any of its sales were, "clearance" if every sale on it was past the first
    days, else "ok".
    """
    fresh_days = fresh_days_from(fresh_days)
    days: dict[str, dict] = {}
    for s in _assess(history, history, fresh_days):
        if s["status"] == "unpriced" or s["date"] is None:
            continue
        d = days.setdefault(s["date"].isoformat(), {
            "products": {}, "short": 0.0, "nett": 0.0, "zaco": 0.0, "flagged": 0})
        p = d["products"].setdefault(analytics.product_label(s["row"]), {
            "sold": 0.0, "at_market": 0.0, "priced_cartons": 0.0, "diff_nett": 0.0,
            "short": 0.0, "nett": 0.0, "zaco": 0.0, "cartons": 0, "flagged": 0,
            "clearance": 0, "first_days": 0, "day": s["day"], "flag_day": None, "agents": []})
        p["sold"] += s["sold"]
        p["at_market"] += s["at_market"]
        p["priced_cartons"] += s["cartons"]
        p["diff_nett"] += (s["sold"] - s["at_market"]) * (1 - s["deduction"])
        p["day"] = min(p["day"], s["day"])
        if s["agent"] not in p["agents"]:
            p["agents"].append(s["agent"])
        if s["status"] == "clearance":
            p["clearance"] += 1
            continue
        p["first_days"] += 1
        if s["status"] == "flagged":
            for k in ("short", "nett", "zaco"):
                p[k] += s[k]
                d[k] += s[k]
            p["cartons"] += int(s["cartons"])
            p["flagged"] += 1
            p["flag_day"] = s["day"] if p["flag_day"] is None else min(p["flag_day"], s["day"])
            d["flagged"] += 1
    for d in days.values():
        for k in ("short", "nett", "zaco"):
            d[k] = round(d[k], 2)
        for p in d["products"].values():
            p["status"] = ("flagged" if p["flagged"] else
                           "clearance" if p["clearance"] and not p["first_days"] else "ok")
            p["market_avg"] = (round(p["at_market"] / p["priced_cartons"], 2)
                               if p["priced_cartons"] else None)
            p["pct"] = round(p["sold"] / p["at_market"] - 1, 4) if p["at_market"] else None
            p["diff"] = round(p["sold"] - p["at_market"], 2)
            for k in ("sold", "at_market", "diff_nett", "short", "nett", "zaco"):
                p[k] = round(p[k], 2)
            p["priced_cartons"] = round(p["priced_cartons"], 2)
    return {"fresh_days": fresh_days, "days": days, "assumptions": _assumptions()}


def lay_on_days(out: dict, checked: dict) -> dict:
    """Put ``by_day`` onto a Sales per day payload (``tracking.daily``).

    Each product line gets ``market_check``, each day and month and the whole
    range ``under_market``. Months and the range add up the days shown, the
    way their sales figures do.
    """
    flagged = checked["days"]
    sbd = out.get("sales_by_day") or {}
    shown = sbd.get("days") or []
    for d in shown:
        mine = flagged.get(d["date"]) or {}
        d["under_market"] = ({k: mine[k] for k in ("short", "nett", "zaco", "flagged")}
                             if mine.get("flagged") else None)
        for x in d.get("products") or []:
            x["market_check"] = mine.get("products", {}).get(x["product"])

    def total(days: list[dict]) -> dict:
        hit = [d["under_market"] for d in days if d.get("under_market")]
        sums = {k: round(sum(h[k] for h in hit), 2) for k in ("short", "nett", "zaco")}
        return {**sums, "flagged": sum(h["flagged"] for h in hit)}

    for m in sbd.get("months") or []:
        m["under_market"] = total([d for d in shown if d["date"].startswith(m["month"])])
    if "totals" in sbd:
        sbd["totals"]["under_market"] = total(shown)
    out["price_check"] = {"fresh_days": checked["fresh_days"], "assumptions": checked["assumptions"]}
    return out
