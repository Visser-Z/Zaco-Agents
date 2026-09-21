"""Reconcile accumulated Daily Sales against Payment Details, to supply Nett.

The two reports don't share a transaction id -- only Supplier Ref and the
commodity -- and they run on different cadences (sales daily, payments weekly).
So reconciliation is by **Supplier Ref + Product**, comparing the summed daily
Sales Total against the Payment Details Gross Payments across the payment's date
range. When those agree, the payment's Nett is known and is distributed back
onto the daily rows by sales value.

All pure functions over plain dicts, so they unit-test without a database.

  daily row  : {"dn", "product", "sales_total", ... (any extra keys pass through)}
  payment rec: as produced by ``payment_details.parse_payment_details``
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import payment_details

MATCH_TOL = 0.01  # Rand; sales value should agree to the cent when reconciled.


def _num(value) -> float:
    """A money or carton figure as a float, whatever shape it arrived in.

    Numeric columns do not always come back from PostgREST as JSON numbers, and
    a payment whose gross is the string "100.00" used to raise out of a sum or a
    round and take the page with it rather than reconcile.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalise_product(product: str | None) -> str:
    """Collapse the two reports' different spellings of the same commodity to a
    common key: drop parentheses, weight/size tokens ("5kg", "5.00 kg"),
    punctuation and case. So "NECTARINES ... LARGE (MULTI LAYER TRAYER 5kg)" and
    "NECTARINES ... LARGE MULTI LAYER TRAYER 5.00 kg" become the same string."""
    p = (product or "").upper()
    # A stray number bled onto the Product line by the PDF layout (see
    # daily_sales._PRODUCT_TAIL). New rows are cleaned at parse time; this
    # catches rows already saved with the bad name so they still match.
    p = re.sub(r"(?<=\))\s+\d+$", "", p.strip())
    p = p.replace("(", " ").replace(")", " ")
    p = re.sub(r"\d+(?:\.\d+)?\s*(?:KG|GMS|G)\b", " ", p)   # weight tokens
    p = re.sub(r"[^A-Z0-9 ]", " ", p)                       # stray punctuation
    return re.sub(r"\s+", " ", p).strip()


def _norm_dn(dn):
    """A supplier ref reduced to one canonical value.

    The two sides of a match do not always agree on type: a ref can arrive as
    14588 from one export and "14588" from the other, and those are the same
    delivery. Keyed as they came, they were two different groups that could
    never reconcile with each other.
    """
    if dn is None:
        return None
    if isinstance(dn, str):
        text = dn.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return text                      # a ref that is genuinely not a number
    try:
        return int(dn)
    except (TypeError, ValueError):
        return dn


def _sort_key(key: tuple) -> tuple:
    """Order (dn, product) keys without ever comparing unlike types.

    Numeric refs first in numeric order, then any non-numeric ref, then the
    rows carrying no ref at all.
    """
    dn, product = key
    return (dn is None, not isinstance(dn, int),
            dn if isinstance(dn, int) else 0, str(dn), str(product or ""))


def _key(dn, product) -> tuple:
    return (_norm_dn(dn), normalise_product(product))


def aggregate_daily(rows: list[dict]) -> dict[tuple, dict]:
    """Sum daily Sales Total per Supplier Ref + Product, keeping the source rows
    so Nett can later be distributed back onto them."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"sales_total": 0.0, "rows": []})
    for r in rows:
        k = _key(r.get("dn"), r.get("product"))
        agg[k]["sales_total"] += _num(r.get("sales_total"))
        agg[k]["rows"].append(r)
    return agg


def aggregate_payment(records: list[dict]) -> dict[tuple, dict]:
    """Sum Gross and Nett per Supplier Ref + Product. An account-sale's Nett is
    apportioned across its commodity lines by their Sales Total first, so a
    multi-commodity payout is split correctly."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"gross": 0.0, "nett": 0.0})
    for rec in records:
        lines = rec.get("lines") or []
        line_total = sum(_num(l["sales_total"]) for l in lines)
        base = line_total or _num(rec.get("gross"))
        for l in lines:
            k = _key(rec.get("dn"), l["product"])
            agg[k]["gross"] += _num(l["sales_total"])
            agg[k]["nett"] += _num(rec.get("nett")) * (_num(l["sales_total"]) / base) if base else 0
    return agg


def parse_payment_refs(value: str | None) -> dict[str, float]:
    """"REF=123.45;REF2=67.89" -> {ref: value}. Tolerates a bare ref list."""
    out: dict[str, float] = {}
    for part in (value or "").split(";"):
        part = part.strip()
        if not part:
            continue
        ref, _, amount = part.partition("=")
        try:
            out[ref.strip()] = float(amount) if amount else 0.0
        except ValueError:
            out[ref.strip()] = 0.0
    return out


def by_payment_reference(daily_rows: list[dict], payment_records: list[dict]) -> list[dict]:
    """Reconcile on the account-sale number each sale was paid under.

    The CSV export names that reference on every docket, which is a direct join
    between a sale and its payment. It replaces matching on supplier ref +
    product name + value, and is exact: no normalising, no near-misses.

    One row per payment reference, comparing what the sales side says was sold
    under it against what the payment report says was paid for it."""
    sold: dict[str, float] = defaultdict(float)
    rows_for: dict[str, list[dict]] = defaultdict(list)
    for row in daily_rows:
        for ref, value in parse_payment_refs(row.get("payment_refs")).items():
            sold[ref] += value
            rows_for[ref].append(row)

    payments = {r["accsale"]: r for r in payment_records if r.get("accsale")}

    out: list[dict] = []
    for ref in sorted(set(sold) | set(payments)):
        pay = payments.get(ref)
        daily_total = round(sold.get(ref, 0.0), 2)
        gross = round(_num(pay["gross"]), 2) if pay else 0.0
        nett = round(_num(pay["nett"]), 2) if pay else 0.0
        out.append(
            {
                "reference": ref,
                "dn": (pay or {}).get("dn") or next(
                    (r.get("dn") for r in rows_for.get(ref, []) if r.get("dn")), None),
                "date": (pay or {}).get("date"),
                "daily_total": daily_total,
                "payment_gross": gross,
                "payment_nett": nett,
                "status": _status(daily_total, gross),
                "consignments": len(rows_for.get(ref, [])),
            }
        )
    return out


def fill_netts_by_reference(daily_rows: list[dict], payment_records: list[dict]) -> int:
    """Fill Nett from the payment each sale actually names.

    An account sale settles several rows at once -- the products on it, and any
    consignment that contributed to the same run -- so its Nett is split between
    them by sales value. That is how the operator's own book does it: two product
    rows on one statement carry Netts in exactly the ratio of their gross.

    A row can also name more than one reference (a consignment sold across two
    runs), in which case its Nett is the sum of its share of each.

    The shares are rounded to the cent and the largest row absorbs what rounding
    leaves over, so the rows on a statement add up to the payment **exactly**.
    Without that, a three-product statement can miss by a cent and read as a
    discrepancy when nothing is wrong.
    """
    payments = {r["accsale"]: r for r in payment_records if r.get("accsale")}

    # reference -> [(row, value sold under it)]
    members: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for row in daily_rows:
        for ref, value in parse_payment_refs(row.get("payment_refs")).items():
            members[ref].append((row, value))

    shares: dict[int, float] = defaultdict(float)
    for ref, group in members.items():
        pay = payments.get(ref)
        if pay is None or not _num(pay.get("gross")):
            continue
        nett, gross = _num(pay.get("nett")), _num(pay["gross"])
        cents = [round(nett * (value / gross), 2) for _, value in group]
        claimed = round(nett * (sum(v for _, v in group) / gross), 2)
        if (residual := round(claimed - sum(cents), 2)):
            biggest = max(range(len(group)), key=lambda i: abs(group[i][1]))
            cents[biggest] = round(cents[biggest] + residual, 2)
        for (row, _), share in zip(group, cents):
            shares[id(row)] += share

    filled = 0
    for row in daily_rows:
        if id(row) in shares:
            row["nett_total"] = round(shares[id(row)], 2)
            filled += 1
    return filled


def unattributed(payment_records: list[dict]) -> dict:
    """Payments the report prints no commodity breakdown for.

    Some account sales in a Payment Details export carry a Gross and a Nett but
    no per-commodity lines. There is no product to match them against, so they
    can never reconcile -- and their money would otherwise vanish from the
    picture without explanation. Surfaced so the operator can see the gap is in
    the source document, not in the matching."""
    blank = [r for r in payment_records if not r.get("lines")]
    return {
        "count": len(blank),
        "gross": round(sum(_num(r.get("gross")) for r in blank), 2),
        "nett": round(sum(_num(r.get("nett")) for r in blank), 2),
        "accsales": [r.get("stm_no") for r in blank if r.get("stm_no")],
    }


def _status(daily: float, gross: float) -> str:
    if gross <= 0:
        return "unpaid"                     # sold, not yet in a payment run
    if daily <= 0:
        return "no_sales"                   # paid, no matching daily rows accumulated
    if abs(daily - gross) <= MATCH_TOL:
        return "matched"                    # fully reconciled -> Nett is trustworthy
    if daily < gross:
        return "outstanding"               # more still to sell/accumulate in range
    return "over"                           # sold more than paid (check)


def reconcile(daily_rows: list[dict], payment_records: list[dict]) -> list[dict]:
    """One row per Supplier Ref + Product, comparing summed daily sales to the
    payment Gross and reporting the status and the Nett."""
    dagg = aggregate_daily(daily_rows)
    pagg = aggregate_payment(payment_records)
    out: list[dict] = []
    # A supplier ref is nullable, and Python will not order None against an
    # int, so a history holding one row without a ref and one with it used to
    # raise straight out of the sort and take the whole page with it.
    for k in sorted(set(dagg) | set(pagg), key=_sort_key):
        dn, product = k
        daily = round(_num(dagg.get(k, {}).get("sales_total", 0.0)), 2)
        gross = round(_num(pagg.get(k, {}).get("gross", 0.0)), 2)
        nett = round(_num(pagg.get(k, {}).get("nett", 0.0)), 2)
        out.append(
            {
                "dn": dn,
                "product": product,
                "daily_total": daily,
                "payment_gross": gross,
                "payment_nett": nett,
                "status": _status(daily, gross),
            }
        )
    return out


def fill_netts(daily_rows: list[dict], payment_records: list[dict]) -> int:
    """Write ``nett_total`` onto each daily row whose Supplier Ref + Product is
    **fully reconciled** (daily sales == payment gross), splitting the payment
    Nett across those rows by sales value. Returns how many rows were filled.

    Only fully-matched groups are filled: a partially-sold group would otherwise
    receive a Nett for produce not all paid yet."""
    dagg = aggregate_daily(daily_rows)
    pagg = aggregate_payment(payment_records)
    filled = 0
    for k, pay in pagg.items():
        grp = dagg.get(k)
        if not grp:
            continue
        dtot = grp["sales_total"]
        if dtot <= 0 or abs(dtot - _num(pay["gross"])) > MATCH_TOL:
            continue
        for r in grp["rows"]:
            r["nett_total"] = round(_num(pay["nett"]) * (_num(r.get("sales_total")) / dtot), 2)
            filled += 1
    return filled


# --- FMS id to delivery ---------------------------------------------------
#
# Supplier Ref cannot carry the join. It is operator-entered free text: in this
# book 20 refs cover more than one delivery, and some are a date or the single
# letter N. The payment report's FMS id is system-generated, one per delivery,
# and the same on every account sale that delivery is ever paid on. It is not
# the market's Delivery ID -- they come from different systems -- but the two
# are one-to-one, and once a payment's FMS id is tied to its delivery, every
# line of every account sale for it lands on an exact consignment:
#
#     consignment = delivery * 100 + line number    (1855491, 02 -> 185549102)
#
# The tie is worked out from the lines both reports carry: a payment line and
# a consignment agree when they sit at the same market, under the same line
# number, and sell the same product. It is recomputed from the saved history on
# every read rather than stored, so it cannot go stale, and a delivery whose
# sales were loaded after its payment binds as soon as they arrive.


def delivery_of(consignment_id) -> int | None:
    """The delivery a consignment came in on: its id less the line number."""
    try:
        c = int(consignment_id)
    except (TypeError, ValueError):
        return None
    return c // 100 if c >= 100 else None


def consignment_of(delivery_id: int, line_no: int) -> int:
    return int(delivery_id) * 100 + int(line_no)


def _degenerate_ref(dn) -> bool:
    """A supplier ref that names no delivery: blank, a producer code echoed
    back, a truncated stub. Such a ref cannot break a tie."""
    try:
        n = int(dn)
    except (TypeError, ValueError):
        return True
    return n < 1000 or n == 20026


def bind_deliveries(sales: list[dict], payments: list[dict]) -> dict[str, int]:
    """Which delivery each payment's FMS id belongs to, where that can be told.

    Evidence is pooled across every account sale carrying the FMS id, then
    each delivery at the same market is scored by how many of those numbered
    lines it holds with the same product. A delivery with a line number the
    payment uses but a different product there is ruled out, and so is one
    whose booked quantity (Qty Amended To) differs from the line's Delivered
    figure where both are known: that is what tells two deliveries apart when
    they sit under one ref and sell the same thing on the same line. The FMS id binds
    to the best delivery when it holds two or more agreeing lines and nothing
    else holds as many, or when it is the only delivery that fits at all.

    A tie is broken by the supplier ref only where the ref actually names a
    delivery, and never decides a match on its own. Anything still open is
    left unbound, and its money falls back to the old supplier ref match
    rather than being guessed onto a consignment.
    """
    # market -> delivery -> line -> products sold on it
    held: dict[str, dict[int, dict[int, set[str]]]] = defaultdict(lambda: defaultdict(dict))
    # consignment -> what the market booked in, where the report said
    booked: dict[int, int] = {}
    refs_of: dict[int, set] = defaultdict(set)
    for row in sales:
        cid = row.get("consignment_id")
        delivery = delivery_of(cid)
        market = (row.get("market") or "").strip()
        if delivery is None or not market:
            continue
        line = int(cid) % 100
        held[market][delivery].setdefault(line, set()).add(normalise_product(row.get("product")))
        if row.get("qty_amended") is not None:
            booked[int(cid)] = int(row["qty_amended"])
        if row.get("dn") is not None:
            refs_of[delivery].add(_norm_dn(row.get("dn")))

    # fms -> (market, {(line, product)}, refs)
    evidence: dict[str, dict] = {}
    for rec in payments:
        fms = rec.get("fms_id")
        # Where the payment was made, from its AccSale prefix: saved payments
        # carry only the agent's name, and two markets share one agent.
        market = rec.get("market") or payment_details.destination(rec.get("accsale"))["market"]
        if not fms or not market:
            continue
        e = evidence.setdefault(str(fms), {"market": market, "lines": set(), "refs": set(),
                                           "markets": set()})
        e["markets"].add(market)
        if not _degenerate_ref(rec.get("dn")):
            e["refs"].add(_norm_dn(rec.get("dn")))
        for line in rec.get("lines") or []:
            if line.get("line_no") is not None:
                e["lines"].add((int(line["line_no"]), normalise_product(line.get("product")),
                                line.get("delivered")))

    chosen: dict[str, tuple[int, int]] = {}
    for fms, e in evidence.items():
        if len(e["markets"]) != 1 or not e["lines"]:
            continue
        scored = []
        def fits(delivery: int, n: int, qty) -> bool:
            have = booked.get(consignment_of(delivery, n))
            return qty is None or have is None or int(qty) == have

        for delivery, lines in held.get(e["market"], {}).items():
            agree = sum(1 for n, p, q in e["lines"]
                        if p in lines.get(n, ()) and fits(delivery, n, q))
            clash = sum(1 for n, p, q in e["lines"]
                        if n in lines and (p not in lines[n] or not fits(delivery, n, q)))
            if agree and not clash:
                scored.append((agree, delivery))
        if not scored:
            continue
        scored.sort(reverse=True)
        best = scored[0][0]
        top = [d for s, d in scored if s == best]
        if len(top) > 1 and e["refs"]:
            top = [d for d in top if refs_of[d] & e["refs"]] or top
        if len(top) != 1:
            continue
        if best >= 2 or len(scored) == 1:
            chosen[fms] = (best, top[0])

    # One delivery, one FMS id. Where two claim the same delivery the stronger
    # keeps it; an even contest binds neither rather than picking one.
    by_delivery: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for fms, (score, delivery) in chosen.items():
        by_delivery[delivery].append((score, fms))
    bound: dict[str, int] = {}
    for delivery, claims in by_delivery.items():
        claims.sort(reverse=True)
        if len(claims) == 1 or claims[0][0] > claims[1][0]:
            bound[claims[0][1]] = delivery
    return bound


def line_consignment(rec: dict, line: dict, bound: dict[str, int],
                     products_of: dict[int, dict[int, str]]) -> int | None:
    """The consignment one payment line paid for, if its FMS id is bound.

    By its line number where the line carries one. A line in the wrapped
    layout prints none, so it is placed by product among the delivery's own
    consignments, and only where exactly one of them sells it.
    """
    delivery = bound.get(str(rec.get("fms_id"))) if rec.get("fms_id") else None
    if delivery is None:
        return None
    if line.get("line_no") is not None:
        return consignment_of(delivery, line["line_no"])
    want = normalise_product(line.get("product"))
    hits = [n for n, p in products_of.get(delivery, {}).items() if p == want]
    return consignment_of(delivery, hits[0]) if len(hits) == 1 else None


# --- mixed histories ------------------------------------------------------
#
# A history can hold rows from both exports at once: CSV rows name the account
# sale that paid them, PDF rows never do. Choosing one strategy for the whole
# run on the strength of `any(payment_refs)` meant a single CSV row switched
# every PDF row to reference matching, where it has no reference to match on
# and reports as "no sales". On a real August round that turned R107 580 of
# matched sales into R0. So each row is matched by what it actually carries.


def _split_on_refs(daily_rows: list[dict], payment_records: list[dict]):
    """Rows and payments split into the reference-matchable and the rest."""
    with_refs = [r for r in daily_rows if r.get("payment_refs")]
    without = [r for r in daily_rows if not r.get("payment_refs")]
    claimed: set[str] = set()
    for row in with_refs:
        claimed |= set(parse_payment_refs(row.get("payment_refs")))
    named = [p for p in payment_records if p.get("accsale") in claimed]
    unnamed = [p for p in payment_records if p.get("accsale") not in claimed]
    return with_refs, without, named, unnamed


def reconcile_any(daily_rows: list[dict], payment_records: list[dict]) -> list[dict]:
    """Reconcile a history whose rows came from either export, or from both.

    Rows naming their payment are matched on that reference, which is exact.
    The rest fall back to supplier ref + product. A payment already claimed by
    reference is not offered to the fallback, so nothing is counted twice.
    """
    with_refs, without, named, unnamed = _split_on_refs(daily_rows, payment_records)
    if not with_refs:
        return reconcile(daily_rows, payment_records)
    out = by_payment_reference(with_refs, named)
    if without or unnamed:
        out += reconcile(without, unnamed)
    return out


def fill_netts_any(daily_rows: list[dict], payment_records: list[dict]) -> int:
    """``fill_netts`` for a mixed history, split the same way as ``reconcile_any``."""
    with_refs, without, named, unnamed = _split_on_refs(daily_rows, payment_records)
    if not with_refs:
        return fill_netts(daily_rows, payment_records)
    return (fill_netts_by_reference(with_refs, named)
            + fill_netts(without, unnamed))
