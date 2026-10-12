"""The market's own Stock On Hand report, pulled from TechnoFresh every day.

Report 28 on the portal. Unlike Daily Sales it lists every delivery line,
including lines nothing has sold from yet, which is the stock the other
reports cannot see: DN 14391's 79 Starlight and 35 December Princess at
Farmers Trust show here with Sold 0. Each line is the position at the moment
the report is run, so one pull a day is a snapshot of what the market says is
on its floor that day.

The CSV, one market per request (the market is then known exactly, not read
off the agent heading, which the portal spells differently per report)::

    "Supplier Ref",FMSID,"Del Date",Product,Sent,"Amended To",Sold,"Stock On Hand","Avg Price"
    "Growfresh Port Natal (Dbn)"
    <blank>
    20026*000014905,749835,2026-10-09,"GRAPES CRIMSON SEEDLESS ...",720,696,57,639,440.00

Tshwane's heading carries the market as well: "TSHWANE MARKET","Farmers Trust (Pre)".
"Amended To" is blank unless the market rebooked the line; then it, not Sent,
is what the market holds itself to.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date

# The portal's market codes, named the way the sales report names them so a
# market means the same thing across the book.
MARKETS = {
    "PRE": "TSHWANE MARKET",
    "JOH": "JOBURG MKT - BR / MAR",
    "TFR": "JOBURG MKT - TFRESH",
    "DUR": "DURBAN MARKET",
    "SPR": "SPRINGS MARKET",
    "CAP": "CAPE TOWN MARKET",
}

HEADER = ["Supplier Ref", "FMSID", "Del Date", "Product", "Sent", "Amended To", "Sold",
          "Stock On Hand", "Avg Price"]


def is_stock_csv(text: str) -> bool:
    first = (text or "").lstrip("﻿").splitlines()[:1]
    return bool(first) and next(csv.reader(first), []) == HEADER


def delivery_note(ref: str) -> int | None:
    """The DN in a Supplier Ref: "20026*000014905" is 14905. A ref that is a
    date ("20026*06/09/26") or a stub names no delivery note."""
    m = re.fullmatch(r"\s*\d+\s*\*\s*0*(\d+)\s*", ref or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1000 and n != 20026 else None


def _int(v: str) -> int | None:
    v = (v or "").strip()
    try:
        return int(float(v)) if v else None
    except ValueError:
        return None


def _money(v: str) -> float | None:
    v = (v or "").strip().replace(",", "")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def parse(text: str, market: str) -> list[dict]:
    """Every delivery line in one market's Stock On Hand CSV.

    ``seq`` numbers the lines of one FMS id in the order printed: the report
    carries no line number, and two lines of one delivery can name the same
    product, so the order is what keeps them apart within a snapshot.
    """
    rows: list[dict] = []
    agent = None
    seq: dict[str, int] = defaultdict(int)
    for rec in csv.reader(io.StringIO((text or "").lstrip("﻿"))):
        if not rec or not any(c.strip() for c in rec):
            continue
        if rec == HEADER:
            continue
        if len(rec) < 9:
            # A heading: the agent, and for Tshwane the market before it.
            agent = rec[-1].strip() or agent
            continue
        fms = rec[1].strip()
        sent = _int(rec[4])
        if not fms or sent is None:
            continue
        seq[fms] += 1
        rows.append({
            "market": market,
            "agent": agent,
            "supplier_ref": rec[0].strip(),
            "dn": delivery_note(rec[0]),
            "fms_id": fms,
            "seq": seq[fms],
            "delivered_on": rec[2].strip() or None,
            "product": rec[3].strip(),
            "sent": sent,
            "amended": _int(rec[5]),
            "sold": _int(rec[6]) or 0,
            "on_hand": _int(rec[7]) or 0,
            "avg_price": _money(rec[8]),
        })
    return rows


def summary(rows: list[dict], snapshot: str | None, today: date | None = None) -> dict:
    """One snapshot, grouped market then delivery note, what is still on the
    floor first. Lines with nothing left are counted, not listed."""
    today = today or date.today()
    markets: dict[str, dict] = {}
    for r in rows:
        m = markets.setdefault(r["market"], {"market": r["market"], "agents": set(),
                                             "notes": {}, "lines": 0, "cartons": 0,
                                             "cleared": 0, "oversold": []})
        if r.get("agent"):
            m["agents"].add(r["agent"])
        if (r.get("on_hand") or 0) < 0:
            # Sold more than it booked in: the market's own figures disagree
            # with themselves, which is worth a question to the agent.
            m["oversold"].append({k: r.get(k) for k in ("dn", "supplier_ref", "fms_id", "product",
                                                        "sent", "amended", "sold", "on_hand",
                                                        "delivered_on")})
        if (r.get("on_hand") or 0) <= 0:
            m["cleared"] += 1
            continue
        key = str(r["dn"]) if r.get("dn") else f"fms {r['fms_id']}"
        n = m["notes"].setdefault(key, {"dn": r.get("dn"), "supplier_ref": r["supplier_ref"],
                                        "fms_ids": set(), "delivered_on": r.get("delivered_on"),
                                        "lines": [], "cartons": 0})
        n["fms_ids"].add(r["fms_id"])
        if r.get("delivered_on") and (not n["delivered_on"] or r["delivered_on"] < n["delivered_on"]):
            n["delivered_on"] = r["delivered_on"]
        n["lines"].append({k: r.get(k) for k in ("product", "sent", "amended", "sold",
                                                  "on_hand", "avg_price", "fms_id")})
        n["cartons"] += r["on_hand"]
        m["lines"] += 1
        m["cartons"] += r["on_hand"]

    def age(d):
        try:
            return (today - date.fromisoformat(d)).days
        except (TypeError, ValueError):
            return None

    out = []
    for m in markets.values():
        notes = []
        for n in m["notes"].values():
            n["lines"].sort(key=lambda x: -x["on_hand"])
            notes.append({**n, "fms_ids": sorted(n["fms_ids"]), "days": age(n["delivered_on"])})
        notes.sort(key=lambda n: (n["delivered_on"] or ""))
        out.append({"market": m["market"], "agents": " · ".join(sorted(m["agents"])) or None,
                    "cartons": m["cartons"], "lines": m["lines"], "cleared": m["cleared"],
                    "oversold": m["oversold"], "notes": notes})
    out.sort(key=lambda m: -m["cartons"])
    return {"snapshot": snapshot, "markets": out,
            "cartons": sum(m["cartons"] for m in out),
            "lines": sum(m["lines"] for m in out),
            "untouched": sum(1 for m in out for n in m["notes"] for l in n["lines"] if not l["sold"]),
            "oversold": sum(len(m["oversold"]) for m in out)}


# --- tying the snapshot to the book ------------------------------------------

def clean_product(name: str) -> str:
    """The product as the sales report names it: the stock report prints a
    packing code after the bracket ("(PUNNET 5kg) 1"), which is not part of it."""
    return re.sub(r"(?<=\))\s+\d+\s*$", "", (name or "").strip())


def bind(sales: list[dict], rows: list[dict]) -> dict[tuple, int]:
    """Which consignment each snapshot line is, where the book can tell.

    The report prints a delivery's lines in consignment line order, so line
    ``seq`` of an FMS id is consignment ``delivery * 100 + seq`` once the
    delivery is known. The delivery is found among the book's consignments on
    the same DN at the same market: the one whose every line on the book has
    the same product and the same cartons sent at that position, and only
    where exactly one does. A delivery nothing has sold from is not on the book
    at all, and its lines stay unbound.
    """
    from .reconcile import normalise_product as norm

    book: dict[tuple, dict[int, dict[int, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for r in sales:
        try:
            cid = int(r.get("consignment_id"))
            dn = int(r.get("dn"))
        except (TypeError, ValueError):
            continue
        if cid < 100:
            continue
        sent = r.get("qty_received")
        book[((r.get("market") or "").strip(), dn)][cid // 100][cid % 100] = (
            norm(r.get("product")), int(sent) if sent is not None else None)

    by_fms: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("dn"):
            by_fms[(r["market"], r["dn"], r["fms_id"])].append(r)

    out: dict[tuple, int] = {}
    claimed: dict[int, set] = defaultdict(set)
    for (market, dn, fms), lines in by_fms.items():
        at = {l["seq"]: l for l in lines}
        fits = []
        for delivery, held in book.get((market, dn), {}).items():
            ok = all(n in at and norm(at[n]["product"]) == p
                     and (s is None or at[n]["sent"] == s) for n, (p, s) in held.items())
            if ok and held:
                fits.append(delivery)
        if len(fits) == 1:
            claimed[fits[0]].add(fms)
            for l in lines:
                out[(market, fms, l["seq"])] = fits[0] * 100 + l["seq"]
    # Two FMS ids claiming one delivery cannot both be right; neither is kept.
    for delivery, ids in claimed.items():
        if len(ids) > 1:
            for k in [k for k, v in out.items() if v // 100 == delivery]:
                del out[k]
    return out


def apply(sales: list[dict], rows: list[dict], snapshot: str | None) -> tuple[list[dict], list[dict]]:
    """The book with the market's latest count laid over it, and the lines the
    book does not have at all because nothing on them has sold.

    A consignment on the book gets ``market_on_hand`` (and the market's booked
    figure where it was amended), dated ``market_counted``, which
    ``tracking.on_hand`` prefers to anything older. A line the book does not
    hold becomes a stock-only row: nothing sold, nothing worth, so it adds to
    what is on the floor and to nothing else. Those rows are for the stock
    views only and must never be saved or counted as sales.
    """
    if not rows or not snapshot:
        return sales, []
    ids = bind(sales, rows)
    on_book = set()
    for s in sales:
        try:
            on_book.add(int(s.get("consignment_id")))
        except (TypeError, ValueError):
            pass
    count: dict[int, dict] = {}
    unsold: list[dict] = []
    for r in rows:
        cid = ids.get((r["market"], r["fms_id"], r["seq"]))
        if cid is not None and cid in on_book:
            count[cid] = r
            continue
        if (r.get("sold") or 0) > 0 or (r.get("on_hand") or 0) <= 0:
            # Sold from but not bound: the book has it under a consignment we
            # could not pin, and guessing would count it twice.
            continue
        unsold.append({
            # Its real consignment where the delivery is known, else none yet.
            "consignment_id": cid, "stock_only": True,
            "dn": r.get("dn"), "supplier_ref": r.get("supplier_ref"),
            "market": r["market"], "market_agent": r.get("agent"),
            "product": clean_product(r["product"]), "description": None,
            "qty_received": r["sent"], "qty_amended": r.get("amended"),
            "cartons_sold": 0, "sales_total": 0.0, "price": 0.0,
            "cartons_returned": 0, "returns_total": 0.0,
            "date_received": r.get("delivered_on"), "group_date": r.get("delivered_on"),
            "invoice_date": r.get("delivered_on"), "last_sale": None,
            "fms_id": r["fms_id"], "fms_line": r["seq"],
            # Read as a CSV row, so its date is the market's delivery date.
            "source_file": "technofresh_stock_on_hand.csv",
            "market_on_hand": r["on_hand"], "market_counted": snapshot,
        })
    if not count:
        return sales, unsold
    laid = []
    for s in sales:
        try:
            hit = count.get(int(s.get("consignment_id")))
        except (TypeError, ValueError):
            hit = None
        if hit is None:
            laid.append(s)
            continue
        extra = {"market_on_hand": hit["on_hand"], "market_counted": snapshot,
                 "market_sold": hit.get("sold")}
        if hit.get("amended") is not None:
            extra["market_booked"] = hit["amended"]
        laid.append({**s, **extra})
    return laid, unsold
