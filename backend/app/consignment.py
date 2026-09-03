"""Consignment settlement: who is owed what, and what Zaco actually earned.

Zaco does not buy produce. A supplier hands it over on consignment, Zaco places
it with a market agent, and the supplier is paid only once it sells. Zaco's
earnings are a commission percentage of what comes back.

The money chain, and where each figure comes from::

    market buyer pays
      -> market agent deducts commission, levies, VAT     (Payment Details PDF)
        -> NETT lands with Zaco                           (Payment Details PDF)
          -> Zaco keeps commission_pct% of the Nett       (consignment_deals)
            -> the remainder is OWED TO THE SUPPLIER      (computed here)

Everything above the Nett line the market reports tell us. Everything below it
exists only here: the market agents see Zaco (producer code 20026) as the
supplier and know nothing about the farmers behind it.

Two things this module refuses to do, both deliberate:

*Nothing is inferred from an absent deal.* A consignment with no recorded
commission produces no settlement at all, rather than one computed at a default
rate. Quietly assuming 30% would manufacture a debt to a real person out of a
missing form field.

*Unsold stock creates no liability.* On consignment the supplier is paid on what
SOLD. Cartons that never moved cost the supplier, not Zaco -- which is the
opposite of a buy-and-resell business, and the single biggest reason the old
cost-based arithmetic had to go.
"""

from __future__ import annotations

from . import analytics

# What Zaco keeps when a deal does not state its own rate. Only ever applied as
# a UI suggestion when recording a new deal; never used to compute a settlement
# behind the operator's back. See `settle`.
DEFAULT_COMMISSION_PCT = 30.0


def deal_key(supplier_ref, product) -> tuple:
    """Deals are struck per delivery line, the unit produce arrives in."""
    return (supplier_ref, (product or "").strip().upper())


def row_key(row: dict) -> tuple:
    return deal_key(row.get("supplier_ref") or row.get("dn"), row.get("product"))


def _nett(row: dict) -> float | None:
    """What actually reached Zaco for this consignment.

    Gross is never used as a substitute. It is the market's sale value, not
    Zaco's money, and settling a supplier on it would pay away the market
    agent's commission as well as Zaco's own.
    """
    nett = row.get("nett_total")
    return float(nett) if nett is not None else None


def settle_row(row: dict, deal: dict | None) -> dict | None:
    """Split one consignment's Nett between Zaco and the supplier.

    Returns None when the settlement cannot honestly be computed: no Nett has
    landed yet, or no commission has been agreed for this line.
    """
    nett = _nett(row)
    if nett is None or deal is None:
        return None
    pct = deal.get("commission_pct")
    if pct is None:
        return None
    commission = round(nett * (float(pct) / 100.0), 2)
    return {
        "supplier_ref": row.get("supplier_ref") or row.get("dn"),
        "product": row.get("product"),
        "supplier_id": deal.get("supplier_id"),
        "supplier": deal.get("supplier_name"),
        "nett": round(nett, 2),
        "commission_pct": float(pct),
        "commission": commission,
        "owed_to_supplier": round(nett - commission, 2),
        "settled_at": deal.get("settled_at"),
        "settled_amount": deal.get("settled_amount"),
        "settled": deal.get("settled_at") is not None,
        "cartons_sold": analytics.row_cartons(row),
        "cartons_sent": int(analytics._num(row.get("qty_received")) or 0),
        # Which delivery this settlement came out of. A consignment settled over
        # several account sales produces a line each, all naming the same
        # delivery, so anything counting deliveries has to count this once.
        "consignment_id": row.get("consignment_id"),
        "market": row.get("market"),
        "market_agent": row.get("market_agent"),
    }


def settle(rows: list[dict], deals: dict[tuple, dict]) -> dict:
    """Settlement across every consignment, plus what is still outstanding.

    `deals` maps (supplier_ref, PRODUCT) to the recorded terms.
    """
    settled: list[dict] = []
    awaiting_terms: list[dict] = []
    awaiting_payment: list[dict] = []

    for row in rows:
        deal = deals.get(row_key(row))
        line = settle_row(row, deal)
        if line is not None:
            settled.append(line)
            continue
        # Say WHY it could not be settled: the two causes need different action.
        stub = {
            "supplier_ref": row.get("supplier_ref") or row.get("dn"),
            "product": row.get("product"),
            "nett": _nett(row),
            "cartons_sold": analytics.row_cartons(row),
            "cartons_sent": int(analytics._num(row.get("qty_received")) or 0),
        }
        if _nett(row) is None:
            awaiting_payment.append(stub)   # the market has not paid yet
        else:
            awaiting_terms.append(stub)     # nobody has said whose it is, or at what rate

    owed = round(sum(s["owed_to_supplier"] for s in settled if not s["settled"]), 2)
    paid = round(sum(float(s["settled_amount"] or s["owed_to_supplier"])
                     for s in settled if s["settled"]), 2)
    earned = round(sum(s["commission"] for s in settled), 2)

    return {
        "lines": settled,
        "commission_earned": earned,
        "owed_to_suppliers": owed,
        "paid_to_suppliers": paid,
        "nett_received": round(sum(s["nett"] for s in settled), 2),
        # Not a rounding error: these are consignments the system cannot speak
        # for at all, and their money is missing from every total above.
        "awaiting_terms": awaiting_terms,
        "awaiting_payment": awaiting_payment,
        "unattributed_nett": round(sum(s["nett"] or 0 for s in awaiting_terms), 2),
    }


def by_supplier(settlement: dict) -> list[dict]:
    """Position per supplier: earned, owed, paid, and how much is still unsold.

    Sorted by what is owed, because that is the question this answers.
    """
    groups: dict[str, dict] = {}
    seen_deliveries: dict[str, set] = {}
    for line in settlement["lines"]:
        name = line["supplier"] or "Unnamed supplier"
        g = groups.setdefault(name, {
            "supplier": name, "supplier_id": line["supplier_id"],
            "consignments": 0, "nett": 0.0, "commission": 0.0,
            "owed": 0.0, "paid": 0.0, "cartons_sold": 0, "cartons_sent": 0,
        })
        # Money and cartons sold are per account sale and add up. What was
        # HANDED OVER belongs to the delivery, and several account sales can come
        # off one delivery -- so it is added once, or a supplier appears to have
        # brought three times what they did and "unsold" goes badly wrong.
        delivery = line.get("consignment_id") or ("line", id(line))
        deliveries = seen_deliveries.setdefault(name, set())
        if delivery not in deliveries:
            deliveries.add(delivery)
            g["consignments"] += 1
            g["cartons_sent"] += line["cartons_sent"]
        g["nett"] += line["nett"]
        g["commission"] += line["commission"]
        g["cartons_sold"] += line["cartons_sold"]
        if line["settled"]:
            g["paid"] += float(line["settled_amount"] or line["owed_to_supplier"])
        else:
            g["owed"] += line["owed_to_supplier"]

    out = []
    for g in groups.values():
        for k in ("nett", "commission", "owed", "paid"):
            g[k] = round(g[k], 2)
        # Cartons handed over that never sold. The supplier carries this loss,
        # but Zaco has to be able to see and explain it.
        g["unsold"] = max(0, g["cartons_sent"] - g["cartons_sold"])
        out.append(g)
    out.sort(key=lambda g: g["owed"], reverse=True)
    return out


def outstanding_stock(rows: list[dict], deals: dict[tuple, dict]) -> list[dict]:
    """Consignment stock still sitting with a market agent, unsold.

    Sent minus **everything** sold off it, per consignment. A consignment sold
    over several account sales is several rows, and each carries the delivery's
    full Qty Sent, so measuring a single row would report stock as unsold that a
    later run had already cleared. This is produce at risk right now, so it is
    reported whether or not terms have been agreed.
    """
    out = []
    for group in analytics.group_consignments(rows):
        sent = int(max(analytics._num(r.get("qty_received")) for r in group))
        sold = sum(analytics.row_cartons(r) for r in group)
        left = sent - sold
        if sent <= 0 or left <= 0:
            continue
        latest = max(group, key=lambda r: (r.get("last_sale") or "", r.get("stm_no") or 0))
        row = group[0]
        deal = deals.get(row_key(row)) or {}
        out.append({
            "supplier_ref": row.get("supplier_ref") or row.get("dn"),
            "product": row.get("product"),
            "supplier": deal.get("supplier_name"),
            "market": row.get("market"),
            "market_agent": row.get("market_agent"),
            "cartons_sent": sent,
            "cartons_sold": sold,
            "cartons_left": left,
            "share_unsold": round(left / sent, 4),
            "last_sale": latest.get("last_sale"),
            "date_received": row.get("date_received"),
        })
    out.sort(key=lambda d: d["cartons_left"], reverse=True)
    return out


def expected_commission(rows: list[dict], deals: dict[tuple, dict],
                        product: str | None = None) -> dict | None:
    """What a product has actually earned Zaco per carton sold.

    Replaces the old profit-per-carton figure, which assumed a purchase price
    that does not exist. Returns None where no consignment of the product has
    agreed terms, rather than assuming the default rate.
    """
    commission = 0.0
    cartons = 0
    covered = 0
    for row in rows:
        if product is not None and analytics.product_label(row) != product:
            continue
        line = settle_row(row, deals.get(row_key(row)))
        if line is None:
            continue
        commission += line["commission"]
        cartons += line["cartons_sold"]
        covered += 1
    if not covered or not cartons:
        return None
    return {
        "commission": round(commission, 2),
        "per_carton": round(commission / cartons, 2),
        "cartons_sold": cartons,
        "consignments_with_terms": covered,
    }
