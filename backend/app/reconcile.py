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

MATCH_TOL = 0.01  # Rand; sales value should agree to the cent when reconciled.


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


def _key(dn, product) -> tuple:
    return (dn, normalise_product(product))


def aggregate_daily(rows: list[dict]) -> dict[tuple, dict]:
    """Sum daily Sales Total per Supplier Ref + Product, keeping the source rows
    so Nett can later be distributed back onto them."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"sales_total": 0.0, "rows": []})
    for r in rows:
        k = _key(r.get("dn"), r.get("product"))
        agg[k]["sales_total"] += float(r.get("sales_total") or 0)
        agg[k]["rows"].append(r)
    return agg


def aggregate_payment(records: list[dict]) -> dict[tuple, dict]:
    """Sum Gross and Nett per Supplier Ref + Product. An account-sale's Nett is
    apportioned across its commodity lines by their Sales Total first, so a
    multi-commodity payout is split correctly."""
    agg: dict[tuple, dict] = defaultdict(lambda: {"gross": 0.0, "nett": 0.0})
    for rec in records:
        lines = rec.get("lines") or []
        line_total = sum(l["sales_total"] for l in lines)
        base = line_total or rec.get("gross") or 0
        for l in lines:
            k = _key(rec.get("dn"), l["product"])
            agg[k]["gross"] += l["sales_total"]
            agg[k]["nett"] += (rec.get("nett") or 0) * (l["sales_total"] / base) if base else 0
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
        gross = round(pay["gross"], 2) if pay else 0.0
        nett = round(pay["nett"], 2) if pay else 0.0
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
        if pay is None or not pay.get("gross"):
            continue
        nett, gross = pay.get("nett") or 0.0, pay["gross"]
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
        "gross": round(sum(r.get("gross") or 0 for r in blank), 2),
        "nett": round(sum(r.get("nett") or 0 for r in blank), 2),
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
    for k in sorted(set(dagg) | set(pagg)):
        dn, product = k
        daily = round(dagg.get(k, {}).get("sales_total", 0.0), 2)
        gross = round(pagg.get(k, {}).get("gross", 0.0), 2)
        nett = round(pagg.get(k, {}).get("nett", 0.0), 2)
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
        if dtot <= 0 or abs(dtot - pay["gross"]) > MATCH_TOL:
            continue
        for r in grp["rows"]:
            r["nett_total"] = round(pay["nett"] * (float(r.get("sales_total") or 0) / dtot), 2)
            filled += 1
    return filled


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
