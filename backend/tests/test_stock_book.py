"""Zaco's delivery notes held against what the market booked, sold and paid.

The cases are the ones read off six real notes in October 2026: a count
written in lots ("54+13"), pink grapes booked as white, a line the market has
not reported because nothing on it has sold, and a line tied by hand.
"""

from datetime import date

from app import stock_book

TODAY = date(2026, 10, 11)
FT = "FARMERS TRUST JHB"


def _sale(cid, dn, product, sent, sold, day="2026-10-06", market=FT, **kw):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": market,
            "market_agent": "Farmers Trust", "qty_received": sent, "cartons_sold": sold,
            "price": 100.0, "sales_total": 100.0 * sold, "date_received": day,
            "invoice_date": day, **kw}


def _note(dn, *lines, market=FT, sent_on="2026-10-05"):
    return {"dn": dn, "sent_on": sent_on, "market": market, "agent": None, "remarks": None,
            "lines": [{"line_no": i, "description": d, "unit": "5kg", "written": w,
                       "cartons": stock_book.parse_count(w)[0], **extra}
                      for i, (d, w, *rest) in enumerate(lines, start=1)
                      for extra in [rest[0] if rest else {}]]}


def test_counts_are_read_the_way_the_driver_wrote_them():
    assert stock_book.parse_count("54+13") == (67, [54, 13])
    assert stock_book.parse_count("65-2") == (63, [65])
    assert stock_book.parse_count(" 90 ") == (90, [90])
    assert stock_book.parse_count("") == (None, [])
    assert stock_book.parse_count("90 ctn") == (90, [90])


def test_the_words_on_a_note_name_the_fruit():
    assert stock_book.note_fruit("Imp Pink grapes") == ("Grapes", "red")
    assert stock_book.note_fruit("White grapes") == ("Grapes", "white")
    assert stock_book.note_fruit("Cotton candy") == ("Grapes", "cotton")
    assert stock_book.note_fruit("Dessert local") == ("Peaches", None)
    assert stock_book.note_fruit("V/P local") == (None, None)


def test_lots_on_a_note_find_the_consignments_they_were_booked_as():
    sales = [_sale(1, 14385, "GRAPES CRIMSON SEEDLESS CLASS 1 (PUNNET 5kg)", 54, 54),
             _sale(2, 14385, "GRAPES CRIMSON SEEDLESS CLASS 1 (PUNNET 5kg)", 13, 3)]
    out = stock_book.reconcile([_note(14385, ("Imp Pink grapes", "54+13"))], sales, [], TODAY)
    line = out["notes"][0]["lines"][0]
    assert [c["consignment_id"] for c in line["consignments"]] == [1, 2]
    assert (line["sold"], line["on_floor"], line["status"], line["flags"]) == (57, 10, "on_floor", [])


def test_pink_booked_as_white_and_short_is_said():
    sales = [_sale(1, 14393, "GRAPES THOMPSON SEEDLESS CLASS 1 (PUNNET 5kg)", 57, 57,
                   qty_amended=40)]
    out = stock_book.reconcile([_note(14393, ("Pink grapes", "57"))], sales, [], TODAY)
    flags = out["notes"][0]["lines"][0]["flags"]
    assert "the market booked 17 fewer than were sent" in flags
    assert any("booked it as GRAPES THOMPSON" in f for f in flags)


def test_a_line_nothing_has_sold_on_is_still_on_the_floor():
    out = stock_book.reconcile([_note(14391, ("Pink grapes", "90"))], [], [], TODAY)
    n = out["notes"][0]
    assert n["lines"][0]["status"] == "not_reported"
    assert n["on_floor"] == 90
    assert n["flags"] == 1                      # six days and still not on a report
    m = out["markets"][0]
    assert (m["market"], m["on_floor"], m["not_reported"]) == (FT, 90, 90)
    assert m["stocktake"][0]["dn"] == 14391


def test_a_line_tied_by_hand_takes_that_consignment():
    sales = [_sale(1, 14386, "GRAPES SUGRAONE CLASS 1 (PUNNET 5kg)", 40, 40),
             _sale(2, 14386, "GRAPES COTTON CANDY CLASS 1 (PUNNET 5kg)", 40, 40)]
    note = _note(14386, ("Cotton", "40", {"consignment_id": 1}), ("White", "40"))
    lines = stock_book.reconcile([note], sales, [], TODAY)["notes"][0]["lines"]
    assert [c["consignment_id"] for c in lines[0]["consignments"]] == [1]
    assert lines[0]["linked_by_hand"]
    assert any("booked it as" in f for f in lines[0]["flags"])
    assert lines[1]["flags"] == ["the market booked it as GRAPES COTTON CANDY CLASS 1 (PUNNET 5kg)"]


def test_what_the_market_has_on_the_dn_but_the_note_does_not_is_listed():
    sales = [_sale(1, 14387, "PLUMS ANGELINO CLASS 1 (TRAY 5.5kg)", 20, 5),
             _sale(2, 14387, "ORANGES VALENCIA CLASS 1 (CARTON 15kg)", 30, 1)]
    out = stock_book.reconcile([_note(14387, ("Plums", "20"))], sales, [], TODAY)
    n = out["notes"][0]
    assert [c["consignment_id"] for c in n["not_on_note"]] == [2]
    assert FT in out["known_markets"]
