"""The Tracking view: what is paid, what is still owed, and what is not moving.

Three questions the owner asks, answered from saved data alone so they hold up
across the whole period rather than only in the moment a file is dropped:

  * Payments  -- of everything sold, what has the agent paid for, and how much
                 is still to come.
  * Sales     -- how much sold each day, per product.
  * Slow      -- what is taking too long to clear off the floor.

Pure functions over plain row dicts (sales) and payment-record dicts, the same
shapes ``analytics`` and ``reconcile`` already use, so this unit-tests without a
database. Every figure that belongs to a delivery is counted once per
consignment, never once per row -- the rule the rest of the app lives by.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations

from . import analytics, integrity, payment_details, reconcile

# --- payments -------------------------------------------------------------

def item_ref(row: dict) -> str:
    """A stable name for one Tracking line, so it can be closed and stay closed.

    The lists are recomputed from the history on every load and carry no id of
    their own, so the name has to come from what the line *is*: the account
    sale where the match knew one, otherwise the delivery note and the
    commodity, normalised the same way the matcher normalises it.
    """
    if row.get("reference"):
        return f"ref:{row['reference']}"
    return f"{row.get('dn')}:{reconcile.normalise_product(row.get('product'))}"


def line_ref(entry: dict) -> str:
    """The name a line is closed under: its consignment where it has one.

    Two consignments can share a delivery note and commodity, and closing one
    must not close the other, so the consignment is the name. Lines closed
    before this still match through ``is_closed``.
    """
    cid = entry.get("consignment_id")
    return f"c:{cid}" if cid else item_ref(entry)


def is_closed(kind: str, entry: dict, closed) -> bool:
    """Whether a line is closed, under its name now or the one it had before.

    Lines used to be named by delivery note and commodity. A line closed then
    stays closed rather than reappearing because its name changed.
    """
    return (closed_key(kind, line_ref(entry)) in closed
            or closed_key(kind, item_ref(entry)) in closed)


# A closed item is named by its kind AND its ref, exactly as the dismissals
# table keys it. The same consignment can appear on both lists, so without the
# kind, closing the slow line would close the outstanding one with it.
def closed_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


def where(rows: list[dict]) -> dict:
    """Where a line is sitting: the market, and the agent selling it there.

    Both are needed. The market says which floor to look at, the agent says who
    to phone, and they are not the same thing -- one agency sells the same
    commodity at more than one market.

    Collected across the line's rows rather than read off the first of them. A
    consignment is one delivery to one agent at one market, so this is normally
    a single name each, and a group that somehow spans two then says so instead
    of quietly showing whichever row happened to sort first.
    """
    def names(field: str) -> str | None:
        seen = sorted({n for r in rows if (n := str(r.get(field) or "").strip())})
        return " · ".join(seen) or None

    return {"market": names("market"), "market_agent": names("market_agent")}


# How long after a sale's last day its payment may land and still be taken as
# the payment for exactly that sale. Agents pay within days; three weeks covers
# a slow week without letting a coincidence a month later claim the sale.
EXACT_WINDOW_DAYS = 21

# The most payment lines one sale may be settled by and still count as an exact
# match. A run is normally paid in one or two account sales; searching wider
# than three starts finding sums that only add up by chance.
EXACT_MAX_LINES = 3


def _started(row: dict) -> str | None:
    """The first day a sales row could have been paid for.

    A row covers a run of days, from the first day it sold (``date_received``)
    to ``last_sale``. The agent pays part way through a run, so a payment dated
    inside the run can still be the payment for it.
    """
    days = [d for d in (str(row.get("date_received") or "")[:10], selling_day(row)) if d]
    return min(days) if days else None


def _after(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _by_date(item: dict) -> tuple:
    return (item["date"] is None, item["date"] or "")


def _placed(payments: list[dict]) -> list[dict]:
    """Payments carrying the market their AccSale prefix names.

    Saved payments hold only the agent's name, and two markets share the
    agent Subtropico, so the market is read off the AccSale Number again here.
    """
    out = []
    for rec in payments:
        if not rec.get("market") and rec.get("accsale"):
            where_to = payment_details.destination(rec["accsale"])
            rec = {**rec, "market": where_to["market"],
                   "market_code": where_to["market_code"], "agent_code": where_to["agent_code"]}
        out.append(rec)
    return out


def is_reversal(rec: dict) -> bool:
    """An account sale worth less than nothing: the agent clawing money back.

    PRE*BT*400352 is one: gross minus R7 090, nett nil. It is not a payment,
    and netting it into what was paid would quietly mark sales as owed that the
    operator has in fact been paid for, then clawed back from. It is kept apart
    and reported on its own.
    """
    return reconcile._num(rec.get("gross")) < 0


def _payment_lines(payments: list[dict], sales: list[dict]):
    """Every commodity line of every payment, keyed to what it paid for.

    Where the payment's FMS id is tied to its delivery (see
    ``reconcile.bind_deliveries``) a line is keyed to its exact consignment.
    Otherwise it falls back to the delivery note and commodity, the match used
    before the FMS id was read -- which is also what every payment saved before
    then still gets, until its report is dropped again.

    Each line carries its share of the payment's Nett, split by value exactly
    as ``reconcile.aggregate_payment`` splits it, so what settles a sale can be
    reported as the money that actually reached Zaco.

    A negative line (a credit the agent booked) cannot pay for anything, so it
    comes off the latest line of the same key paid on or before it. What each
    was paid in total is unchanged; it is only placed. A whole account sale
    that is negative is a reversal, not a credit line, and is returned apart.
    """
    payments = _placed(payments)
    bound = reconcile.bind_deliveries(sales, payments)
    products_of: dict[int, dict[int, str]] = defaultdict(dict)
    for row in sales:
        if (delivery := reconcile.delivery_of(row.get("consignment_id"))) is not None:
            products_of[delivery][int(row["consignment_id"]) % 100] = \
                reconcile.normalise_product(row.get("product"))

    lines: dict[tuple, list[dict]] = defaultdict(list)
    credits: list[tuple[tuple, dict]] = []
    reversals: list[dict] = []
    for rec in payments:
        if is_reversal(rec):
            reversals.append(rec)
            continue
        rec_lines = rec.get("lines") or []
        base = (sum(reconcile._num(l.get("sales_total")) for l in rec_lines)
                or reconcile._num(rec.get("gross")))
        nett = reconcile._num(rec.get("nett"))
        day = str(rec.get("date") or "")[:10] or None
        for l in rec_lines:
            gross = reconcile._num(l.get("sales_total"))
            if not gross:
                continue
            cid = reconcile.line_consignment(rec, l, bound, products_of)
            key = (("c", cid) if cid is not None
                   else ("k", reconcile._key(rec.get("dn"), l.get("product"))))
            line = {"gross": gross, "left": gross, "date": day,
                    "rate": nett / base if base else 0.0,
                    "dn": rec.get("dn"), "product": l.get("product")}
            if gross < 0:
                credits.append((key, line))
            else:
                lines[key].append(line)
    for key, credit in credits:
        pool = sorted(lines.get(key, []), key=_by_date)
        before = [l for l in pool
                  if l["date"] and credit["date"] and l["date"] <= credit["date"]]
        for target in reversed(before or pool):
            take = min(target["left"], -credit["left"])
            target["left"] -= take
            target["gross"] -= take
            credit["left"] += take
            if credit["left"] >= -0.005:
                break
    return lines, reversals, bound


def line_key(row: dict) -> tuple:
    """What a sale is grouped and reported under: its consignment where it has
    one, otherwise the delivery note and commodity it always was."""
    cid = row.get("consignment_id")
    if cid:
        return ("c", int(cid))
    return ("k", reconcile._key(row.get("dn"), row.get("product")))


def _allocate(sales: list[dict], payments: list[dict]):
    """Settle each payment against the sale it was actually for.

    A payment line tied to its consignment through the FMS id settles that
    consignment's own sales. The rest settle the sales of the delivery note and
    commodity, as they always did, from whatever those sales still have owing.
    Either way the money is placed on individual sales in three passes:

      1. Exact. One to three payment lines that add up to one sale's value to
         the cent, paid during that sale's run or within three weeks after it,
         are the payment for that sale.
      2. By date. What is left of each payment, in the order it was paid, goes
         to the sales that had already started selling when it was paid, oldest
         first. A payment cannot be for fruit that had not sold yet.
      3. Anything still left goes to any unpaid sale, oldest first, so a date
         that is out by a day never turns a real payment into credit.

    Value never decides WHETHER a line matches -- the two reports legitimately
    disagree on what a consignment sold -- only how much of a sale it settles.

    Returns (owed per sale, Nett paid per sale, leftover credit per key, a label
    per key, and a dict of what else was found: the reversals, the Gross paid
    per sale, the FMS bindings and which keys had sales behind them).
    """
    lines, reversals, bound = _payment_lines(payments, sales)
    by_consignment: dict[tuple, list[dict]] = defaultdict(list)
    by_ref: dict[tuple, list[dict]] = defaultdict(list)
    label: dict[tuple, dict] = {}
    for row in sales:
        who = {"dn": row.get("dn"), "product": row.get("product")}
        if row.get("consignment_id"):
            key = ("c", int(row["consignment_id"]))
            by_consignment[key].append(row)
            label.setdefault(key, who)
        key = ("k", reconcile._key(row.get("dn"), row.get("product")))
        by_ref[key].append(row)
        label.setdefault(key, who)

    # Returns are worth less than nothing and are owed as they stand.
    left = {id(r): max(reconcile._num(r.get("sales_total")), 0.0) for r in sales}
    nett = {id(r): 0.0 for r in sales}
    gross_paid = {id(r): 0.0 for r in sales}

    def pay(row: dict, line: dict, amount: float) -> None:
        line["left"] -= amount
        left[id(row)] -= amount
        nett[id(row)] += amount * line["rate"]
        gross_paid[id(row)] += amount

    def settle_pool(rows: list[dict], pool: list[dict]) -> None:
        # A sale with no date cannot be placed in the order, so it settles last
        # rather than taking credit from a sale known to be older.
        rows = sorted(rows, key=lambda r: (selling_day(r) is None, selling_day(r) or ""))
        pool = sorted(pool, key=_by_date)
        for row in rows:
            start, end = _started(row), selling_day(row)
            if left[id(row)] <= 0 or not start or not end:
                continue
            latest = _after(end, EXACT_WINDOW_DAYS)
            untouched = [l for l in pool
                         if l["left"] > 0 and abs(l["left"] - l["gross"]) < 0.005
                         and l["date"] and start <= l["date"] <= latest][:12]
            match = next((combo for n in range(1, EXACT_MAX_LINES + 1)
                          for combo in combinations(untouched, n)
                          if abs(sum(l["left"] for l in combo) - left[id(row)]) < 0.005), ())
            for line in match:
                pay(row, line, line["left"])
        for dated_only in (True, False):
            for line in pool:
                for row in rows:
                    if line["left"] <= 0.005:
                        break
                    if left[id(row)] <= 0.005:
                        continue
                    start = _started(row)
                    if dated_only and not (line["date"] and start and start <= line["date"]):
                        continue
                    pay(row, line, min(line["left"], left[id(row)]))

    # The exact ties first, then the fallback on what is still owing.
    for key, rows in by_consignment.items():
        if key in lines:
            settle_pool(rows, lines[key])
    for key, rows in by_ref.items():
        if key in lines:
            settle_pool(rows, lines[key])

    owed: dict[int, float] = {}
    paid_nett: dict[int, float] = {}
    for row in sales:
        value = reconcile._num(row.get("sales_total"))
        owed[id(row)] = round(left[id(row)] if value > 0 else value, 2)
        paid_nett[id(row)] = round(nett[id(row)], 2)

    credit: dict[tuple, float] = {}
    for key, pool in lines.items():
        if key not in label:
            first = pool[0] if pool else {}
            label[key] = {"dn": first.get("dn"), "product": first.get("product")}
        if (spare := round(sum(l["left"] for l in pool), 2)) > 0.01:
            credit[key] = spare
    extra = {
        "reversals": reversals,
        "gross_paid": {k: round(v, 2) for k, v in gross_paid.items()},
        "bound": bound,
        "sold_keys": set(by_consignment) | set(by_ref),
    }
    return owed, paid_nett, credit, label, extra


def settle(sales: list[dict], payments: list[dict]) -> tuple[dict, dict, dict]:
    """What each individual sale still has owing on it.

    See ``_allocate`` for how a payment finds its sale. Returns (owed per sale
    by id, leftover credit per key, a label for each key).
    """
    owed, _, credit, label, _ = _allocate(sales, payments)
    return owed, credit, label


def owed_by_market(rows: list[dict]) -> list[dict]:
    """Outstanding lines grouped by the market they are owed from.

    Chasing money is done market by market: one call to that floor covers
    every line on it. Markets come biggest debt first, and within a market the
    biggest line first, which is the order they are worth chasing in.
    """
    markets: dict[str, dict] = {}
    for r in rows:
        name = r.get("market") or UNPLACED
        m = markets.setdefault(name, {"market": name, "agents": set(), "lines": [],
                                      "owed": 0.0})
        m["lines"].append(r)
        m["owed"] += r.get("owed", 0.0)
        if r.get("market_agent"):
            m["agents"].add(r["market_agent"])
    out = []
    for m in markets.values():
        m["lines"].sort(key=lambda r: r.get("owed", 0.0), reverse=True)
        days = [d for r in m["lines"] if (d := r.get("date"))]
        out.append({**m, "agents": " · ".join(sorted(m["agents"])) or None,
                    "owed": round(m["owed"], 2), "items": len(m["lines"]),
                    "oldest": min(days) if days else None})
    out.sort(key=lambda m: -m["owed"])
    return out


def valued(sales: list[dict]) -> list[dict]:
    """The sales, each carrying the value payments are matched against.

    Reconcile matches on each row's sold VALUE. Older history does not carry
    the exact docket total, so it is filled with the gross the app trusts
    everywhere else -- cartons sold times price -- which differs only by
    rounding and is the figure being compared against on the payment side.
    """
    return [{**r, "sales_total": r.get("sales_total")
             if r.get("sales_total") is not None else analytics.row_value(r)}
            for r in sales]


def payment_status(sales: list[dict], payments: list[dict],
                   closed: set[str] | frozenset[str] = frozenset(),
                   lo: str | None = None, hi: str | None = None) -> dict:
    """What has been paid, and what is still owed.

    Every sale on the book is settled against its consignment's payments (see
    ``settle``); the window then decides which sales are reported, never how
    they were settled. So a month shows what is still owed on that month's own
    sales, and the months sum to the whole.
    """
    sales = valued(sales)

    owed_by_row, paid_by_row, credit, label, extra = _allocate(sales, payments)

    # What the agent paid in this window, by the day it arrived. Not the Paid
    # figure: a sale on the 31st is paid on the 5th, so counting by arrival
    # put that money in the wrong month. Kept beside it as a note.
    in_window = [
        p for p in payments
        if not ((lo and str(p.get("date") or "")[:10] < lo)
                or (hi and str(p.get("date") or "")[:10] > hi))
    ] if (lo or hi) else payments
    paid_in_window = round(sum(float(p.get("nett") or 0) for p in in_window), 2)

    # The sales this view reports, one line per consignment: the thing a
    # payment settles and the thing the operator chases.
    scoped: dict[tuple, list[dict]] = defaultdict(list)
    for row in sales:
        day = selling_day(row)
        if (lo or hi) and day is None:
            continue
        if (lo and day < lo) or (hi and day > hi):
            continue
        scoped[line_key(row)].append(row)

    # Paid means paid FOR this window's sales, whenever the money came in, so
    # the month the fruit sold in is the month that shows it paid.
    paid_for_window = round(sum(paid_by_row[id(r)] for rows in scoped.values() for r in rows), 2)

    still_to_come = 0.0
    matched = outstanding = 0
    outstanding_rows: list[dict] = []
    closed_rows: list[dict] = []
    credit_rows: list[dict] = []
    for key, rows in scoped.items():
        owed = round(sum(owed_by_row[id(r)] for r in rows), 2)
        days = [d for r in rows if (d := selling_day(r))]
        entry = {
            "dn": label[key]["dn"],
            "product": label[key]["product"],
            "consignment_id": key[1] if key[0] == "c" else None,
            "daily_total": round(sum(reconcile._num(r.get("sales_total")) for r in rows), 2),
            "payment_gross": round(sum(extra["gross_paid"][id(r)] for r in rows), 2),
            "payment_nett": round(sum(paid_by_row[id(r)] for r in rows), 2),
            "date": min(days) if days else None,
            **where(rows),
        }
        entry["ref"] = line_ref(entry)
        if owed == 0:
            matched += 1
            continue
        entry["owed"] = owed
        entry["status"] = ("credit" if owed < 0
                           else "over" if entry["payment_gross"] else "unpaid")
        # A closed line stays visible on its own list with its value, so closing
        # can never quietly shrink the exposure.
        if is_closed("owed", entry, closed):
            closed_rows.append(entry)
            continue
        # A month can come out negative on a consignment: a return booked in
        # August reverses sales made in July, so August's own rows are worth
        # less than nothing. Treating that as settled and dropping it lost the
        # credit from every month view -- the months then came to more than the
        # book. It is not a line to chase, so it is listed apart from the ones
        # that are, but it counts towards the total either way.
        if owed < 0:
            credit_rows.append(entry)
            still_to_come += owed
            continue
        outstanding += 1
        still_to_come += owed
        outstanding_rows.append(entry)

    # Paid for more than the whole book ever sold on that consignment. A
    # position across all of it, not a property of any one month, so it is
    # reported whichever window is open.
    overpaid_rows = [
        {"dn": label[k]["dn"], "product": label[k]["product"], "overpaid": v}
        for k, v in credit.items() if k in extra["sold_keys"]
    ]

    # Payments the sales side cannot account for. Never dropped -- either a sale
    # not imported yet, or a mismatch to chase.
    unmatched = [
        {
            "dn": label[k]["dn"],
            "product": label[k]["product"],
            "consignment_id": k[1] if k[0] == "c" else None,
            "paid": v,
            "reason": "paid, nothing sold matches",
        }
        for k, v in credit.items() if k not in extra["sold_keys"]
    ]

    # Money the agent clawed back. Reported on its own rather than netted into
    # what was paid.
    # TODO(product decision): whether a reversal adds to what is outstanding.
    # Ref 14587 reads R14 080 sold and R7 090 clawed back, so its exposure is
    # either R14 080 (the default here) or R21 170 with the claw-back added.
    # Both figures are in this payload; the dashboard shows the first.
    reversals = [{
        "accsale": r.get("accsale"), "dn": r.get("dn"), "date": r.get("date"),
        "gross": round(reconcile._num(r.get("gross")), 2),
        "nett": round(reconcile._num(r.get("nett")), 2),
        "products": [l.get("product") for l in r.get("lines") or []],
    } for r in extra["reversals"]
        if not ((lo and str(r.get("date") or "")[:10] < lo)
                or (hi and str(r.get("date") or "")[:10] > hi))]
    unattributed = reconcile.unattributed(payments)

    outstanding_rows.sort(key=lambda r: r["owed"], reverse=True)
    dates = [r["date"] for r in outstanding_rows if r["date"]]
    oldest = min(dates) if dates else None

    return {
        "total_paid": paid_for_window,
        "received_in_window": paid_in_window,
        "payments_in_window": len(in_window),
        "still_to_come": round(still_to_come, 2),
        "batches_paid": matched,
        "batches_outstanding": outstanding,
        "oldest_outstanding": oldest,
        # Not truncated: this is the list the operator prints and works down.
        "outstanding": outstanding_rows,
        # The same lines grouped by the market that owes them, which is who
        # gets phoned about them.
        "outstanding_markets": owed_by_market(outstanding_rows),
        "overpaid": sorted(overpaid_rows, key=lambda r: r["overpaid"], reverse=True)[:20],
        "unmatched": unmatched,
        "unattributed": unattributed,
        "payments_recorded": len(payments),
        # Returns that reversed sales made earlier than this window. Counted in
        # the total, listed on their own because there is nothing to chase.
        "credits": sorted(credit_rows, key=lambda r: r["owed"]),
        "credit_value": round(sum(r["owed"] for r in credit_rows), 2),
        # Closed lines and what they were worth, reported rather than dropped.
        "closed": sorted(closed_rows, key=lambda r: r["owed"], reverse=True),
        "closed_value": round(sum(r["owed"] for r in closed_rows), 2),
        "reversals": reversals,
        "reversal_value": round(sum(r["gross"] for r in reversals), 2),
        # Outstanding with the claw-backs added, for the product decision above.
        "still_to_come_with_reversals": round(
            still_to_come - sum(r["gross"] for r in reversals), 2),
        # How many payments are tied to their delivery by FMS id, so the screen
        # can say how much of the matching is exact.
        "fms_bound": len(extra["bound"]),
    }


# --- sales per day, per product ------------------------------------------

def report_name(row: dict) -> str:
    """The file a row was read from, without the part identifying the row.

    A statement's source is recorded per block -- "the-export.pdf · consignment 4",
    "· product 2 of 3" -- which names the row rather than the report. Grouping on
    that gives one source per row and answers nothing, so the suffix comes off.
    """
    name = str(row.get("source_file") or "").split(" · ")[0].strip()
    return name or "Not recorded"


def sales_by_day(sales: list[dict], start: str | None = None,
                 end: str | None = None,
                 settled: tuple[dict, dict] | None = None) -> dict:
    """How much sold each trading day, and where.

    Every figure is net of returns, as every carton figure in the app is: a day
    that sold ten and had two come back moved eight. What came back is reported
    beside the net rather than folded into it -- both the cartons and the money,
    per product, because "Back: 2" on its own says nothing about what it cost.

    Days come back newest first, which is the order the owner reads them in.
    Nothing is truncated here: the caller is expected to show the lot.
    """
    by_day: dict[str, dict] = {}
    for row in sales:
        day = selling_day(row)
        if not day:
            continue
        # ISO dates compare correctly as strings, so no parsing is needed.
        if (start and day < start) or (end and day > end):
            continue
        product = analytics.product_label(row)
        market = (row.get("market") or "").strip() or UNPLACED
        d = by_day.setdefault(day, {"date": day, "cartons": 0.0, "returned": 0.0,
                                    "value": 0.0, "returns_value": 0.0,
                                    "paid": 0.0, "owed": 0.0,
                                    "sources": defaultdict(
                                        lambda: {"rows": 0, "cartons": 0.0}),
                                    "dated": defaultdict(int),
                                    "markets": defaultdict(
                                        lambda: {"cartons": 0.0, "value": 0.0,
                                                 "returned": 0.0, "returns_value": 0.0}),
                                    "products": defaultdict(
                                        lambda: {"cartons": 0.0, "value": 0.0, "returned": 0.0,
                                                 "returns_value": 0.0, "group": None,
                                                 "agents": set(), "market_avg": [], "weight": 0.0,
                                                 "markets": defaultdict(
                                                     lambda: {"cartons": 0.0, "value": 0.0})})})
        cartons = analytics.row_cartons(row)
        value = analytics.row_value(row)
        back = analytics.row_returned(row)
        back_value = analytics.row_returns_value(row)

        d["cartons"] += cartons
        d["returned"] += back
        d["value"] += value
        d["returns_value"] += back_value
        # What came back for this day's sales, whenever the money arrived, and
        # what of them is still unpaid. Settled exactly as Outstanding is.
        if settled is not None:
            owed, paid = settled
            d["paid"] += paid.get(id(row), 0.0)
            d["owed"] += owed.get(id(row), 0.0)

        m = d["markets"][market]
        m["cartons"] += cartons
        m["value"] += value
        m["returned"] += back
        m["returns_value"] += back_value

        # Which report this row was read from. A day that turns up where no
        # trading happened is answered by naming the file it came out of, rather
        # than by trusting or doubting the date on its own.
        src = d["sources"][report_name(row)]
        src["rows"] += 1
        src["cartons"] += cartons
        if (basis := dated_by(row)):
            d["dated"][basis] += 1

        p = d["products"][product]
        p["cartons"] += cartons
        p["value"] += value
        p["returned"] += back
        p["returns_value"] += back_value
        # The operator's own short code for this commodity -- their "group".
        p["group"] = p["group"] or (row.get("description") or "").strip() or None
        if row.get("market_agent"):
            p["agents"].add(row["market_agent"])
        pm = p["markets"][market]
        pm["cartons"] += cartons
        pm["value"] += value
        # The market's own average for this commodity that day, weighted by
        # cartons so a one-carton line cannot outvote a hundred-carton one.
        if (avg := integrity.market_avg(row)) is not None and cartons > 0:
            p["market_avg"].append(avg * cartons)
            p["weight"] += cartons

    days = []
    for day in sorted(by_day, reverse=True):
        d = by_day[day]
        products = sorted(
            ({
                "product": name,
                "group": v["group"],
                "cartons": round(v["cartons"], 2),
                "returned": round(v["returned"], 2),
                "returns_value": round(v["returns_value"], 2),
                "value": round(v["value"], 2),
                # What a carton actually fetched that day.
                "price": round(v["value"] / v["cartons"], 2) if v["cartons"] else 0.0,
                # And what the market was paying for it, where the report says.
                "market_avg": round(sum(v["market_avg"]) / v["weight"], 2) if v["weight"] else None,
                "agents": sorted(v["agents"]),
                # Where this commodity sold that day, biggest first. The answer
                # to "this group sold what, at which market, today".
                "markets": sorted(
                    ({"market": mk, "cartons": round(mv["cartons"], 2),
                      "value": round(mv["value"], 2)}
                     for mk, mv in v["markets"].items()),
                    key=lambda x: x["value"], reverse=True),
             } for name, v in d["products"].items()),
            key=lambda x: x["value"], reverse=True,
        )
        days.append({
            "date": day,
            "cartons": round(d["cartons"], 2),
            "returned": round(d["returned"], 2),
            "returns_value": round(d["returns_value"], 2),
            "value": round(d["value"], 2),
            # Paid is the Nett that reached Zaco for this day's sales, after the
            # agent's deductions. Owed is the sale value still waiting on a
            # payment. So sold less paid is NOT owed: the difference between
            # them is the agent's cut on what has been paid.
            "paid": round(d["paid"], 2),
            "owed": round(d["owed"], 2),
            "products": products,
            "markets": _shares(d["markets"], d["value"]),
            "sources": sorted(
                ({"file": name, "rows": v["rows"], "cartons": round(v["cartons"], 2)}
                 for name, v in d["sources"].items()),
                key=lambda s: (-s["rows"], s["file"]),
            ),
            # How many rows here were dated by a real sale date, and how many
            # only by the day their load was sent.
            "dated": {"sold": d["dated"].get("sold", 0),
                      "delivered": d["dated"].get("delivered", 0)},
        })

    return {
        "days": days,
        # Month totals over the same scope, so the month-to-date figure sits
        # beside the days rather than behind a filter.
        "months": _month_totals(days),
        # Which market is carrying the period, as a share of its sales value.
        "markets": _period_markets(days),
        "totals": {
            "value": round(sum(x["value"] for x in days), 2),
            "cartons": round(sum(x["cartons"] for x in days), 2),
            "returned": round(sum(x["returned"] for x in days), 2),
            "returns_value": round(sum(x["returns_value"] for x in days), 2),
            "paid": round(sum(x["paid"] for x in days), 2),
            "owed": round(sum(x["owed"] for x in days), 2),
            "days": len(days),
        },
    }


# A row whose market the export never named. Given a name rather than left blank
# so it is visible in a share table instead of vanishing into a gap.
UNPLACED = "Market not recorded"


def _shares(markets: dict, total: float) -> list[dict]:
    """Market rows with their share of the value, biggest first.

    The share is of sales VALUE, not cartons: a market moving a lot of cheap
    fruit is not the one carrying the day.
    """
    out = [{"market": name,
            "cartons": round(v["cartons"], 2),
            "returned": round(v["returned"], 2),
            "returns_value": round(v["returns_value"], 2),
            "value": round(v["value"], 2),
            "share": round(v["value"] / total, 4) if total else 0.0}
           for name, v in markets.items()]
    out.sort(key=lambda x: x["value"], reverse=True)
    return out


def _month_totals(days: list[dict]) -> list[dict]:
    """Each month in scope, newest first, with what it has taken so far."""
    agg: dict[str, dict] = {}
    for d in days:
        m = agg.setdefault(d["date"][:7], {"month": d["date"][:7], "value": 0.0,
                                           "cartons": 0.0, "returned": 0.0,
                                           "returns_value": 0.0, "paid": 0.0,
                                           "owed": 0.0, "days": 0,
                                           "first": d["date"], "last": d["date"]})
        m["value"] += d["value"]
        m["paid"] += d.get("paid", 0.0)
        m["owed"] += d.get("owed", 0.0)
        m["cartons"] += d["cartons"]
        m["returned"] += d["returned"]
        m["returns_value"] += d["returns_value"]
        m["days"] += 1
        m["first"] = min(m["first"], d["date"])
        m["last"] = max(m["last"], d["date"])
    out = [{**m, "value": round(m["value"], 2), "cartons": round(m["cartons"], 2),
            "paid": round(m["paid"], 2), "owed": round(m["owed"], 2),
            "returned": round(m["returned"], 2),
            "returns_value": round(m["returns_value"], 2)}
           for m in agg.values()]
    out.sort(key=lambda x: x["month"], reverse=True)
    return out


def _period_markets(days: list[dict]) -> list[dict]:
    """Market shares across every day in scope, biggest first."""
    agg: dict[str, dict] = defaultdict(
        lambda: {"cartons": 0.0, "value": 0.0, "returned": 0.0, "returns_value": 0.0})
    total = 0.0
    for d in days:
        for m in d["markets"]:
            cell = agg[m["market"]]
            cell["cartons"] += m["cartons"]
            cell["value"] += m["value"]
            cell["returned"] += m["returned"]
            cell["returns_value"] += m["returns_value"]
            total += m["value"]
    return _shares(agg, total)


# --- slow to sell ---------------------------------------------------------

# The rule now lives in ``analytics`` beside ``row_date``, because Reports has
# to place a sale in a period exactly the way Tracking and Insights do. Kept
# here as the name this module has always called it.
selling_day = analytics.selling_day


def dated_by(row: dict) -> str | None:
    """Which field gave this row its selling day.

    ``last_sale`` is the day the report says it sold. ``group_date`` is the
    consignment's date -- the day the load was sent -- and standing in for a
    sale date it is an inference, not a reading. A day built on it can look
    exactly like a day built on real sale dates, which is how a day with no
    trading behind it becomes impossible to argue with.
    """
    if row.get("last_sale"):
        return "sold"
    if row.get("group_date"):
        return "delivered"
    return None


def _clearance_days(sales: list[dict], today: date) -> list[int]:
    """Days each cleared consignment took, arrival to its last sale.

    One figure per consignment, not per row. This is the sample the thresholds
    are read from, so it must be what actually happened, not what is still open.
    """
    spans: list[int] = []
    for group in analytics.group_consignments(sales):
        d = analytics.consignment_days_to_sell(group)
        if d is not None:
            spans.append(d)
    return spans


def slow_bands(sales: list[dict], today: date | None = None) -> dict:
    """Where "too long to sell" begins, read from the history itself.

    Most produce clears fast, so the median says little; it is the slow tail
    that matters. The Watch line is the 75th percentile of how long things have
    actually taken (a quarter of everything took at least this long), Slow the
    90th, Dead the 95th, each floored so a run of quick months cannot set the
    bar at two days. With too little history to have a tail, it falls back to
    the plain 5 / 10 / 15 trading days from the brief.
    """
    spans = sorted(_clearance_days(sales, today or date.today()))
    if len(spans) < 8:
        return {"watch": 5, "slow": 10, "dead": 15, "from": "default (too little history)"}

    def pct(p: float) -> int:
        return spans[min(len(spans) - 1, int(len(spans) * p))]

    watch = max(pct(0.75), 4)
    slow = max(pct(0.90), watch + 3)
    dead = max(pct(0.95), slow + 3)
    return {"watch": watch, "slow": slow, "dead": dead,
            "from": f"{len(spans)} cleared consignments"}


def slow_stock(sales: list[dict], today: date | None = None,
               closed: set[str] | frozenset[str] = frozenset(),
               lo: str | None = None, hi: str | None = None) -> dict:
    """Consignments still on the floor, and how long they have been there.

    Aging is measured from the delivery date to today, over the cartons that
    have NOT sold -- counted once per consignment, so a delivery settled across
    several days is one ageing position, not several. Produce that has fully
    cleared is not here; this is only what is still sitting.
    """
    today = today or date.today()
    bands = slow_bands(sales, today)
    out: list[dict] = []
    for group in analytics.group_consignments(sales):
        sent = analytics.cartons_sent(group)
        sold = sum(analytics.row_cartons(r) for r in group)
        left = sent - sold
        if sent <= 0 or left <= 0:
            continue
        arrived = min((d for r in group if (d := analytics._parse_date(r.get("date_received")))), default=None)
        if arrived is None:
            continue
        days = (today - arrived).days
        if days < bands["watch"]:
            continue
        tier = "dead" if days >= bands["dead"] else "slow" if days >= bands["slow"] else "watch"
        first = group[0]
        last_moved = max((d for r in group if (d := analytics._parse_date(r.get("last_sale")))), default=None)
        out.append({
            "ref": line_ref({"consignment_id": first.get("consignment_id"),
                             "dn": first.get("dn"),
                             "product": analytics.product_label(first)}),
            "consignment_id": first.get("consignment_id"),
            "product": analytics.product_label(first),
            "dn": first.get("dn"),
            **where(group),
            "cartons_left": int(left),
            "cartons_sent": int(sent),
            "days_on_floor": days,
            "arrived": arrived.isoformat(),
            "last_moved": last_moved.isoformat() if last_moved else None,
            "tier": tier,
        })
    order = {"dead": 0, "slow": 1, "watch": 2}
    out.sort(key=lambda r: (order[r["tier"]], -r["days_on_floor"], -r["cartons_left"]))
    # Scoped by when the stock arrived, which is what "August's slow stock"
    # means. The bands themselves still come from the whole history: a
    # threshold read off one month would move every time the month changed.
    if lo or hi:
        out = [r for r in out
               if not (lo and r["arrived"] < lo) and not (hi and r["arrived"] > hi)]
    shut = [r for r in out if is_closed("slow", r, closed)]
    out = [r for r in out if not is_closed("slow", r, closed)]
    counts = {t: sum(1 for r in out if r["tier"] == t) for t in ("watch", "slow", "dead")}
    return {"bands": bands, "counts": counts, "items": out[:50], "flagged": len(out),
            "closed": shut, "closed_count": len(shut)}


# --- stock on hand --------------------------------------------------------

# Days on hand, in the three colours the operator reads the floor by. A line
# is green for its first six days, orange from seven, red from fourteen.
ORANGE_FROM_DAYS = 7
RED_FROM_DAYS = 14


def stock_tier(days: int) -> str:
    if days >= RED_FROM_DAYS:
        return "red"
    if days >= ORANGE_FROM_DAYS:
        return "orange"
    return "green"


def _from_csv(row: dict) -> bool:
    """Whether a row came from the TechnoFresh CSV export.

    Only the CSV prints the market's Delivery Date, and only the CSV names the
    payment reference on every docket, so either tells the two apart.
    """
    return bool(row.get("payment_refs")) or ".csv" in str(row.get("source_file") or "").lower()


def _arrival(group: list[dict]) -> tuple[date | None, str]:
    """When a consignment reached the market, and what that date really is.

    The CSV export prints the Delivery Date, which is the arrival. The Daily
    Sales PDF prints no arrival at all, so ``date_received`` on those rows is
    the first day the consignment sold. Both are used, but never passed off as
    each other: the basis rides with the date so the screen can say "received"
    or "first sold" honestly. A real delivery date wins wherever one exists.
    """
    def earliest(rows):
        return min((d for r in rows if (d := analytics._parse_date(r.get("date_received")))),
                   default=None)
    delivered = earliest([r for r in group if _from_csv(r)])
    if delivered is not None:
        return delivered, "received"
    return earliest(group), "first_sold"


def _by_delivery_note(lines: list[dict]) -> list[dict]:
    """A market's unsold lines, grouped by the delivery note they came in on.

    One delivery note often carries several products, and the load is what the
    operator knows: "DN 14954 is still on the floor at Tshwane". Each note is
    as old as its oldest line and coloured by it, so a note with one red
    product among fresh ones still reads red. Oldest note first, as the lines
    within it are.
    """
    notes: dict[object, dict] = {}
    for r in lines:
        g = notes.setdefault(r.get("dn"), {"dn": r.get("dn"), "lines": [], "cartons_left": 0,
                                           "cartons_sent": 0, "days_on_hand": 0,
                                           "arrived": r["arrived"],
                                           "arrived_basis": r["arrived_basis"]})
        g["lines"].append(r)
        g["cartons_left"] += r["cartons_left"]
        g["cartons_sent"] += r["cartons_sent"]
        if r["days_on_hand"] >= g["days_on_hand"]:
            g["days_on_hand"] = r["days_on_hand"]
            g["arrived"], g["arrived_basis"] = r["arrived"], r["arrived_basis"]
    out = []
    for g in notes.values():
        g["items"] = len(g["lines"])
        g["tier"] = stock_tier(g["days_on_hand"])
        out.append(g)
    out.sort(key=lambda g: (-g["days_on_hand"], -g["cartons_left"]))
    return out


def on_hand(group: list[dict]) -> tuple[float, float]:
    """(cartons delivered, cartons still on the floor) for one consignment.

    The market says what is left: every Daily Sales report prints Qty Avail,
    the stock on the floor when it was run, so the latest report's figure is
    the answer and nothing needs working out. Only where no report has said
    is it derived, from what the market booked in (Qty Amended To) less what
    sold, and failing that from what was sent. Working it out from Qty Sent
    alone kept 120 cartons of Durban grapes on hand that the market had
    amended away.

    A consignment keeps its stock whatever its payments say: being paid for
    what sold does not sell what is left.
    """
    booked = max((int(r["qty_amended"]) for r in group
                  if r.get("qty_amended") is not None), default=None)
    sent = float(booked) if booked is not None else analytics.cartons_sent(group)
    said = [r for r in group if r.get("qty_avail") is not None]
    if said:
        latest = max(said, key=lambda r: (str(r.get("last_sale") or ""),
                                          str(r.get("created_at") or "")))
        return sent, float(latest["qty_avail"])
    return sent, sent - sum(analytics.row_cartons(r) for r in group)


def stock_on_hand(sales: list[dict], today: date | None = None,
                  closed: set[str] | frozenset[str] = frozenset(),
                  lo: str | None = None, hi: str | None = None) -> dict:
    """Everything still unsold, grouped by the market it is sitting at.

    A line is one consignment: cartons sent less cartons sold, counted once per
    delivery exactly as ``slow_stock`` counts them, however many days it sold
    over. Unlike ``slow_stock`` nothing is held back until it passes a
    threshold; every unsold carton is on the list from its first day, coloured
    by how long it has been there.

    Lines within a market run oldest first so the red ones sit at the top.
    Markets run by how many red lines they carry, then by cartons on hand.
    Closing a line uses the same name as closing a slow line, so anything
    already closed stays closed.
    """
    today = today or date.today()
    lines: list[dict] = []
    for group in analytics.group_consignments(sales):
        sent, left = on_hand(group)
        if sent <= 0 or left <= 0:
            continue
        arrived, basis = _arrival(group)
        if arrived is None:
            continue
        days = max((today - arrived).days, 0)
        first = group[0]
        lines.append({
            "ref": line_ref({"consignment_id": first.get("consignment_id"),
                             "dn": first.get("dn"),
                             "product": analytics.product_label(first)}),
            "consignment_id": first.get("consignment_id"),
            "product": analytics.product_label(first),
            "dn": first.get("dn"),
            **where(group),
            "cartons_left": int(left),
            "cartons_sent": int(sent),
            "arrived": arrived.isoformat(),
            "arrived_basis": basis,
            "days_on_hand": days,
            "tier": stock_tier(days),
        })
    # Scoped by when the stock arrived, as slow stock always was: "August's
    # stock" is what August put on the floor.
    if lo or hi:
        lines = [r for r in lines
                 if not (lo and r["arrived"] < lo) and not (hi and r["arrived"] > hi)]
    shut = [r for r in lines if is_closed("slow", r, closed)]
    lines = [r for r in lines if not is_closed("slow", r, closed)]

    markets: dict[str, dict] = {}
    for r in lines:
        name = r["market"] or UNPLACED
        m = markets.setdefault(name, {"market": name, "agents": set(), "lines": [],
                                      "cartons_left": 0, "red": 0, "orange": 0, "green": 0})
        m["lines"].append(r)
        m["cartons_left"] += r["cartons_left"]
        m[r["tier"]] += 1
        if r["market_agent"]:
            m["agents"].add(r["market_agent"])
    out = []
    for m in markets.values():
        m["lines"].sort(key=lambda r: (-r["days_on_hand"], -r["cartons_left"]))
        m["agents"] = " · ".join(sorted(m["agents"])) or None
        m["items"] = len(m["lines"])
        m["dns"] = _by_delivery_note(m["lines"])
        out.append(m)
    out.sort(key=lambda m: (-m["red"], -m["cartons_left"], m["market"]))
    return {
        "markets": out,
        "items": len(lines),
        "cartons_left": sum(r["cartons_left"] for r in lines),
        "counts": {t: sum(1 for r in lines if r["tier"] == t) for t in ("green", "orange", "red")},
        "bands": {"orange": ORANGE_FROM_DAYS, "red": RED_FROM_DAYS},
        # How many lines are dated by their first sale rather than a real
        # delivery date, so the screen can say so rather than imply otherwise.
        "dated_by_first_sale": sum(1 for r in lines if r["arrived_basis"] == "first_sold"),
        "closed": sorted(shut, key=lambda r: -r["days_on_hand"]),
        "closed_count": len(shut),
    }


# --- the whole payload ----------------------------------------------------

def date_span(sales: list[dict]) -> dict:
    """The first and last day anything sold, so the pickers can bound themselves."""
    days = sorted({d for r in sales if (d := selling_day(r))})
    return {"first": days[0] if days else None, "last": days[-1] if days else None}


def available_periods(sales: list[dict]) -> dict:
    """The months and weeks that actually have sales, for the period picker.

    Built from the selling day, not from ``analytics.row_date``: that one reads
    the consignment's send date first, so the picker would offer months the
    trading never happened in and miss the ones it did.
    """
    months: set[str] = set()
    weeks: set[str] = set()
    by_month: dict[str, set[str]] = defaultdict(set)
    for row in sales:
        day = selling_day(row)
        if not day:
            continue
        d = analytics._parse_date(day)
        if d is None:
            continue
        iso = d.isocalendar()
        m, w = d.strftime("%Y-%m"), f"{iso[0]}-W{iso[1]:02d}"
        months.add(m)
        weeks.add(w)
        by_month[m].add(w)
    return {"months": sorted(months), "weeks": sorted(weeks),
            "weeks_by_month": {m: sorted(ws) for m, ws in by_month.items()}}


def in_period(sales: list[dict], lo: str | None, hi: str | None) -> list[dict]:
    """Rows that sold inside the window. Undated rows cannot be placed, so they
    drop out of a scoped view and stay in the all-time one."""
    if not lo and not hi:
        return list(sales)
    out = []
    for r in sales:
        day = selling_day(r)
        if day is None or (lo and day < lo) or (hi and day > hi):
            continue
        out.append(r)
    return out


def compute(sales: list[dict], payments: list[dict], today: date | None = None,
            start: str | None = None, end: str | None = None,
            closed: set[str] | frozenset[str] = frozenset(),
            month: str | None = None, week: str | None = None) -> dict:
    """Everything the Tracking tab renders.

    A month answers for its own sales. Every sale on the book is settled
    against its consignment's payments oldest first, and the window then only
    decides which sales are reported, so the months add up to the whole instead
    of one month's payment cancelling another month's sale. The all-time figure
    rides alongside regardless. Slow stock is about what is sitting on the floor
    right now, so no window applies to it.
    """
    lo, hi = analytics.period_bounds(month, week)
    # The explicit day pickers are finer than a period, so they win where both
    # are set; the period fills them in otherwise.
    d_start, d_end = (start or lo), (end or hi)
    # The whole book is settled, then the window decides which sales are
    # reported. Handing it only the scoped sales made a consignment's entire
    # payment history land on one month of its sales, so a July payment cancelled
    # a September sale and September read as over-paid.
    status = payment_status(sales, payments, closed, lo, hi)
    # The days are settled on the same match, so what a day shows as paid and
    # owed agrees with Outstanding to the cent.
    filled = valued(sales)
    owed_by_row, paid_by_row, _, _, _ = _allocate(filled, payments)
    # The all-time figure rides alongside so a month is never mistaken for the
    # total exposure. The months do now add up to it.
    if lo or hi:
        status["still_to_come_all_time"] = payment_status(
            sales, payments, closed)["still_to_come"]
    return {
        "payments": status,
        "sales_by_day": sales_by_day(filled, d_start, d_end,
                                     settled=(owed_by_row, paid_by_row)),
        "slow_stock": slow_stock(sales, today, closed, lo, hi),
        "stock_on_hand": stock_on_hand(sales, today, closed, lo, hi),
        "span": date_span(sales),
        "periods": available_periods(sales),
        "filter": {"from": d_start, "to": d_end, "month": month, "week": week},
    }
