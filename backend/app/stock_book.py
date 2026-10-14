"""Zaco's delivery notes against what the market booked, sold and paid.

The market's reports only show a consignment once something on it has sold, so
stock that has not sold yet is invisible to them: on 5 October 2026 DN 14391
went to Farmers Trust with 90 cartons of pink grapes, 35 V/P and an orange
that no report showed, because none of it had sold. Zaco's own delivery note
says what went on the truck, and is the start of the stock.

Each note is entered as written: the DN number (the number the market prints
as Supplier Ref), the date, the market, and each line in the driver's words,
"Imp Pink grapes, 5kg, 54+13". Each line is then tied to the market's
consignments on that DN by fruit, by colour for grapes, and by carton count,
and from that comes where the stock is: still on the floor, sold and waiting
for payment, or paid.

The words on a note are not the market's: "Pink grapes" was Crimson at Marco
and Red Sultana at Farmers Trust, "Dessert local" was peaches. The count is
what settles which line is which, and where it does not, a person ties the
line by hand. Where the market booked something other than the note says, it
is said: on DN 14393 it booked 57 pink grapes as white, and 17 cartons fewer
than were sent.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from itertools import combinations

from . import analytics, tracking

# What a word on a note says about the fruit. Grapes carry a colour as well,
# because a load usually holds both and the count alone may not split them.
_RED = {"PINK", "RED", "CRIMSON", "FLAME", "GLOBE", "SULTANA", "CELEBRATION", "RALLI",
        "SCARLOTTA", "ALLISON", "BLACK", "AUTUMN", "JACK"}
_WHITE = {"WHITE", "THOMPSON", "SUGRAONE", "PRIME", "DESSERT SEEDLESS", "SONITA", "REGAL",
          "ARRA", "GREEN", "SUPERIOR"}
_FRUIT_WORDS = [
    ("cotton", "Grapes", "cotton"),
    ("cotton candy", "Grapes", "cotton"),
    ("grape", "Grapes", None), ("pink", "Grapes", "red"), ("white", "Grapes", "white"),
    ("crimson", "Grapes", "red"), ("sultana", "Grapes", "red"), ("globe", "Grapes", "red"),
    ("thompson", "Grapes", "white"), ("sugraone", "Grapes", "white"),
    ("nectarine", "Nectarines", None), ("plum", "Plums", None),
    ("dessert", "Peaches", None), ("peach", "Peaches", None),
    ("granadilla", "Granadillas", None), ("orange", "Oranges", None),
    ("valencia", "Oranges", None), ("navel", "Oranges", None),
    ("cherr", "Cherries", None), ("strawberr", "Strawberries", None),
    ("apricot", "Apricots", None), ("pear", "Pears", None), ("apple", "Apples", None),
    ("mango", "Mangoes", None), ("litchi", "Litchis", None), ("lemon", "Lemons", None),
]


def parse_count(written: str | None) -> tuple[int | None, list[int]]:
    """The cartons on a note's line, as written: "54+13" is 67 in two lots,
    "65-2" is 63 (two boxes short, as the market stamped it)."""
    text = (written or "").replace(" ", "")
    if not text:
        return None, []
    if not re.fullmatch(r"\d+([+-]\d+)*", text):
        nums = re.findall(r"\d+", text)
        return (int(nums[0]), [int(nums[0])]) if nums else (None, [])
    total = 0
    parts = []
    for sign, n in re.findall(r"([+-]?)(\d+)", text):
        if sign == "-":
            total -= int(n)
        else:
            total += int(n)
            parts.append(int(n))
    return total, parts


def note_fruit(description: str) -> tuple[str | None, str | None]:
    """The fruit (and for grapes, the colour) a note's line names, if any."""
    text = (description or "").lower()
    if "cotton" in text:
        return "Grapes", "cotton"
    fruit = colour = None
    for word, f, c in _FRUIT_WORDS:
        if word in text:
            fruit = fruit or f
            if f == "Grapes" and c and colour is None:
                colour = c
    if fruit is None and colour:
        fruit = "Grapes"
    return fruit, colour


def market_colour(product: str) -> str | None:
    """A grape's colour as the market names it; None where it gives no variety."""
    name = (product or "").upper()
    if "COTTON CANDY" in name:
        return "cotton"
    if "DESSERT SEEDLESS" in name or any(w in name.split() for w in _WHITE):
        return "white"
    if any(w in name.split() for w in _RED):
        return "red"
    return None


def fits(line: dict, cons: dict) -> bool:
    """Whether a market consignment could be this line of the note."""
    fruit, colour = line["_fruit"], line["_colour"]
    if fruit is None:
        return True
    if analytics.product_type(cons["product"]) != fruit:
        return False
    if fruit != "Grapes" or colour is None:
        return True
    theirs = market_colour(cons["product"])
    return theirs is None or theirs == colour


def consignments(sales: list[dict], payments: list[dict],
                 unsold: list[dict] | None = None) -> dict[int, list[dict]]:
    """Every market consignment by its delivery note, with what it sold and
    what of that is paid, from the same matching the Tracking tab uses.

    ``unsold`` is the market's own count of lines nothing has sold from yet
    (``market_stock.apply``). No sales report shows those, so without the count
    a note line on one could only read "not reported"."""
    filled = tracking.valued(sales)
    owed, _, _, _, extra = tracking._allocate(filled, payments)
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in filled:
        if r.get("consignment_id") and r.get("dn") is not None:
            groups[int(r["consignment_id"])].append(r)
    by_dn: dict[int, list[dict]] = defaultdict(list)
    for cid, rows in groups.items():
        rows.sort(key=lambda r: analytics.selling_day(r) or "")
        first = rows[0]
        sent_, avail = tracking.on_hand(rows)
        counted = next((r.get("market_counted") for r in rows
                        if r.get("market_on_hand") is not None), None)
        if counted is None and not any(r.get("qty_avail") is not None for r in rows):
            avail = None
        try:
            dn = int(first["dn"])
        except (TypeError, ValueError):
            continue
        sent = max((int(r.get("qty_received") or 0) for r in rows), default=0)
        booked = max((int(r["qty_amended"]) for r in rows if r.get("qty_amended") is not None),
                     default=sent)
        if counted is not None:
            booked = int(sent_)
        by_dn[dn].append({
            "consignment_id": cid, "dn": dn, "product": first.get("product"),
            "market": first.get("market"), "market_agent": first.get("market_agent"),
            "sent": sent, "booked": booked,
            "sold": round(sum(analytics.row_cartons(r) for r in rows)),
            "value": round(sum(float(r.get("sales_total") or 0) for r in rows), 2),
            "owed": round(sum(owed[id(r)] for r in rows if owed[id(r)] > 0), 2),
            "paid": round(sum(extra["gross_paid"][id(r)] for r in rows), 2),
            "avail": None if avail is None else int(avail), "counted": counted,
            "first_sale": analytics.selling_day(first),
            "last_sale": analytics.selling_day(rows[-1]),
        })
    for r in unsold or []:
        try:
            dn = int(r["dn"])
        except (TypeError, ValueError):
            continue
        booked = int(r["qty_amended"]) if r.get("qty_amended") is not None else int(r["qty_received"])
        by_dn[dn].append({
            # No consignment id until something sells; a stand-in that cannot
            # collide with a real one, so a person can still tie it by hand.
            "consignment_id": r.get("consignment_id") or -(int(r["fms_id"]) * 100 + int(r["fms_line"])),
            "dn": dn, "product": r["product"], "market": r["market"],
            "market_agent": r.get("market_agent"), "sent": int(r["qty_received"]),
            "booked": booked, "sold": 0, "value": 0.0, "owed": 0.0, "paid": 0.0,
            "avail": int(r["market_on_hand"]), "counted": r.get("market_counted"),
            "first_sale": None, "last_sale": None,
        })
    return by_dn


def _match(note: dict, cons: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Tie each line of the note to the market's consignments on its DN."""
    lines = note["lines"]
    taken: dict[int, list[dict]] = {i: [] for i in range(len(lines))}
    free = sorted(cons, key=lambda c: (c["market"] != note["market"], c["consignment_id"]))
    used: set[int] = set()

    def take(i: int, group: list[dict]) -> None:
        taken[i] += group
        used.update(c["consignment_id"] for c in group)

    def open_for(i: int, strict: bool = True) -> list[dict]:
        return [c for c in free if c["consignment_id"] not in used
                and (not strict or fits(lines[i], c))]

    # By hand first: a person said which consignment this is.
    for i, l in enumerate(lines):
        if l.get("consignment_id"):
            take(i, [c for c in free if c["consignment_id"] == int(l["consignment_id"])
                     and c["consignment_id"] not in used])
    # The right fruit and the same count, as one consignment or as the lots
    # the note wrote it in ("54+13" booked as two).
    for i, l in enumerate(lines):
        if taken[i]:
            continue
        same = [c for c in open_for(i) if c["sent"] == l["cartons"]]
        if same:
            take(i, same[:1])
            continue
        if len(l["_parts"]) > 1:
            pick = []
            for part in l["_parts"]:
                hit = next((c for c in open_for(i) if c["sent"] == part and c not in pick), None)
                if hit:
                    pick.append(hit)
            if len(pick) == len(l["_parts"]):
                take(i, pick)
    # The right fruit, several consignments that add up to the count.
    for i, l in enumerate(lines):
        if taken[i]:
            continue
        pool = open_for(i)[:8]
        for n in (2, 3):
            hit = next((list(g) for g in combinations(pool, n)
                        if sum(c["sent"] for c in g) == l["cartons"]), None)
            if hit:
                take(i, hit)
                break
    # The right fruit, the only one left, whatever its count.
    for i, l in enumerate(lines):
        if not taken[i] and len(open_for(i)) == 1 and l["_fruit"]:
            take(i, open_for(i))
    # Last, the count alone, whatever the market called it: that is the
    # market booking it as something else, and is said as such.
    for i, l in enumerate(lines):
        if not taken[i]:
            same = [c for c in open_for(i, strict=False) if c["sent"] == l["cartons"]]
            if len(same) == 1:
                take(i, same)
    left = [c for c in free if c["consignment_id"] not in used]
    return taken, left


def reconcile(notes: list[dict], sales: list[dict], payments: list[dict],
              today: date | None = None, unsold: list[dict] | None = None) -> dict:
    """Every note's lines with where their stock is, and stock per market."""
    today = today or date.today()
    by_dn = consignments(sales, payments, unsold)
    out_notes = []
    markets: dict[str, dict] = {}
    for note in sorted(notes, key=lambda n: (str(n.get("sent_on") or ""), n["dn"]), reverse=True):
        lines = []
        for l in sorted(note.get("lines") or [], key=lambda l: l.get("line_no") or 0):
            total, parts = parse_count(l.get("written") or str(l.get("cartons") or ""))
            fruit, colour = note_fruit(l.get("description"))
            lines.append({**l, "cartons": int(l.get("cartons") if l.get("cartons") is not None else total or 0),
                          "_parts": parts or [int(l.get("cartons") or 0)],
                          "_fruit": fruit, "_colour": colour})
        note = {**note, "lines": lines}
        taken, left = _match(note, by_dn.get(int(note["dn"]), []))
        age = (today - date.fromisoformat(str(note["sent_on"])[:10])).days if note.get("sent_on") else None
        result_lines = []
        for i, l in enumerate(lines):
            got = taken[i]
            flags = []
            if not got:
                status = "not_reported"
                floor = l["cartons"]
                booked = sold = 0
                value = owed = paid = 0.0
                avail = None
            else:
                booked = sum(c["booked"] for c in got)
                sold = sum(c["sold"] for c in got)
                value = round(sum(c["value"] for c in got), 2)
                owed = round(sum(c["owed"] for c in got), 2)
                paid = round(sum(c["paid"] for c in got), 2)
                floor = max(booked - sold, 0)
                avails = [c["avail"] for c in got if c["avail"] is not None]
                avail = sum(avails) if len(avails) == len(got) else None
                counted = [c["counted"] for c in got if c.get("counted")]
                if avail is not None and len(counted) == len(got):
                    # The market counted it this evening: its floor is the
                    # floor. Where the book works it out differently, a sale or
                    # a return is not on the book yet, and that is said.
                    if avail < 0:
                        flags.append(f"the market sold {-avail} more than it booked in")
                    elif avail != floor:
                        flags.append(f"the market counts {avail} on the floor, the book works out {floor}")
                    floor = max(avail, 0)
                market_sent = sum(c["sent"] for c in got)
                if market_sent != l["cartons"]:
                    flags.append(f"the market recorded {market_sent} sent, the note says {l['cartons']}")
                if booked < market_sent:
                    flags.append(f"the market booked {market_sent - booked} fewer than were sent")
                if not all(fits(l, c) for c in got):
                    flags.append("the market booked it as " + " and ".join(
                        sorted({c["product"] for c in got})))
                if any(c["market"] != note["market"] for c in got):
                    flags.append("it was booked at " + " and ".join(
                        sorted({c["market"] for c in got if c["market"] != note["market"]})))
                status = ("on_floor" if floor > 0 else "owed" if owed > 0.005 else "paid")
            result_lines.append({
                "line_no": l.get("line_no"), "description": l.get("description"),
                "unit": l.get("unit"), "written": l.get("written"), "cartons": l["cartons"],
                "consignments": [{k: c[k] for k in ("consignment_id", "product", "market",
                                                    "sent", "booked", "sold", "avail")}
                                 for c in got],
                "booked": booked, "sold": sold, "on_floor": floor, "market_avail": avail,
                "market_counted": max((c["counted"] for c in got if c.get("counted")), default=None),
                "value": value, "owed": owed, "paid": paid, "status": status, "flags": flags,
                "linked_by_hand": bool(l.get("consignment_id")),
            })
        extra = [{k: c[k] for k in ("consignment_id", "product", "market", "sent", "booked", "sold")}
                 for c in left]
        out_notes.append({
            "dn": note["dn"], "sent_on": str(note.get("sent_on") or "")[:10] or None,
            "market": note.get("market"), "agent": note.get("agent"), "remarks": note.get("remarks"),
            "age_days": age, "lines": result_lines, "not_on_note": extra,
            "cartons": sum(x["cartons"] for x in result_lines),
            "on_floor": sum(x["on_floor"] for x in result_lines),
            "flags": sum(len(x["flags"]) for x in result_lines) + len(extra)
            + sum(1 for x in result_lines if x["status"] == "not_reported" and (age or 0) > 3),
        })
        m = markets.setdefault(note.get("market") or "Unknown", {
            "market": note.get("market") or "Unknown", "notes": 0, "sent": 0, "on_floor": 0,
            "not_reported": 0, "owed": 0.0, "paid": 0.0, "stocktake": []})
        m["notes"] += 1
        for x in result_lines:
            m["sent"] += x["cartons"]
            m["on_floor"] += x["on_floor"]
            m["owed"] += x["owed"]
            m["paid"] += x["paid"]
            if x["status"] == "not_reported":
                m["not_reported"] += x["cartons"]
            if x["on_floor"] > 0:
                m["stocktake"].append({
                    "dn": note["dn"], "sent_on": str(note.get("sent_on") or "")[:10],
                    "age_days": age, "description": x["description"], "unit": x["unit"],
                    "products": [c["product"] for c in x["consignments"]],
                    "on_floor": x["on_floor"], "market_avail": x["market_avail"],
                    "reported": x["status"] != "not_reported"})
    for m in markets.values():
        m["owed"] = round(m["owed"], 2)
        m["paid"] = round(m["paid"], 2)
        m["stocktake"].sort(key=lambda s: -(s["age_days"] or 0))
    return {
        "notes": out_notes,
        "markets": sorted(markets.values(), key=lambda m: -m["on_floor"]),
        "known_markets": sorted({c["market"] for cs in by_dn.values() for c in cs if c.get("market")}),
        "totals": {
            "notes": len(out_notes),
            "on_floor": sum(m["on_floor"] for m in markets.values()),
            "not_reported": sum(m["not_reported"] for m in markets.values()),
            "owed": round(sum(m["owed"] for m in markets.values()), 2),
            "paid": round(sum(m["paid"] for m in markets.values()), 2),
            "flags": sum(n["flags"] for n in out_notes),
        },
    }
