"""The market's daily Stock On Hand: reading it and laying it over the book."""
from datetime import date

from app import market_stock, procurement, stock_book, tracking

CSV = '''"Supplier Ref",FMSID,"Del Date",Product,Sent,"Amended To",Sold,"Stock On Hand","Avg Price"
"TSHWANE MARKET","Farmers Trust (Pre)"

20026*14391,203511,2026-10-06,"GRANADILLAS NO VARIETY NOT GRADED NO SIZE (STANDARD TRAY 3.5kg)",2,,2,0,50.00
20026*14391,203511,2026-10-06,"PEACHES DECEMBER PRINCESS CLASS 1 LARGE (PUNNET 8kg)",35,,0,35,0.00
20026*14391,203511,2026-10-06,"PLUMS ELDORADO CLASS 1 LARGE (MULTI LAYER TRAYER 5.3kg)",42,,42,0,42.14
20026*14391,208094,2026-10-06,"GRAPES CRIMSON SEEDLESS CLASS 1 NO SIZE (PUNNET 5kg)",90,44,0,44,0.00
20026*14391,208094,2026-10-06,"GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg) 1",22,,12,10,327.27
20026*06/09/26,203485,2026-09-07,"GRANADILLAS NO VARIETY NOT GRADED NO SIZE (BOX 4.5kg)",1,,1,0,50.00

'''

TODAY = date(2026, 10, 12)
SNAP = "2026-10-12"


def sale(cid, product, sold, sent, avail=None, last="2026-10-08", dn=14391):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": "TSHWANE MARKET",
            "market_agent": "Farmers Trust", "cartons_sold": sold, "price": 10.0,
            "sales_total": sold * 10.0, "qty_received": sent, "qty_avail": avail,
            "group_date": "2026-10-07", "last_sale": last, "date_received": "2026-10-07"}


# The book: lines 01 and 03 of delivery 1190440, line 02 of delivery 1190442.
# Line 02 of the first and line 01 of the second have sold nothing, so no
# sales report has ever shown them.
SALES = [
    sale(119044001, "GRANADILLAS NO VARIETY NOT GRADED NO SIZE (STANDARD TRAY 3.5kg)", 2, 2, 0),
    sale(119044003, "PLUMS ELDORADO CLASS 1 LARGE (MULTI LAYER TRAYER 5.3kg)", 42, 42, 0),
    sale(119044202, "GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)", 10, 22, 12),
]


def rows():
    return market_stock.parse(CSV, "TSHWANE MARKET")


def test_parse_reads_lines_in_print_order():
    r = rows()
    assert len(r) == 6
    assert r[0]["agent"] == "Farmers Trust (Pre)"
    assert [x["seq"] for x in r[:3]] == [1, 2, 3]
    assert r[3]["fms_id"] == "208094" and r[3]["seq"] == 1
    assert r[3]["amended"] == 44 and r[3]["on_hand"] == 44
    # A ref that is a date names no delivery note.
    assert r[5]["dn"] is None and r[0]["dn"] == 14391
    assert market_stock.is_stock_csv(CSV)


def test_bind_pins_lines_to_consignments_by_position():
    ids = market_stock.bind(SALES, rows())
    assert ids[("TSHWANE MARKET", "203511", 2)] == 119044002
    assert ids[("TSHWANE MARKET", "208094", 2)] == 119044202
    assert ids[("TSHWANE MARKET", "208094", 1)] == 119044201


def test_apply_lays_the_count_over_the_book_and_adds_unsold_lines():
    laid, unsold = market_stock.apply(SALES, rows(), SNAP)
    cotton = next(s for s in laid if s["consignment_id"] == 119044202)
    assert cotton["market_on_hand"] == 10 and cotton["market_counted"] == SNAP
    assert sorted((u["product"], u["market_on_hand"]) for u in unsold) == [
        ("GRAPES CRIMSON SEEDLESS CLASS 1 NO SIZE (PUNNET 5kg)", 44),
        ("PEACHES DECEMBER PRINCESS CLASS 1 LARGE (PUNNET 8kg)", 35),
    ]
    assert all(u["cartons_sold"] == 0 and u["sales_total"] == 0 for u in unsold)


def test_on_hand_prefers_the_newer_count():
    laid, _ = market_stock.apply(SALES, rows(), SNAP)
    cotton = [s for s in laid if s["consignment_id"] == 119044202]
    assert tracking.on_hand(cotton) == (22.0, 10.0)
    # A sale reported after the count is newer than it.
    later = [{**cotton[0], "last_sale": "2026-10-13", "qty_avail": 4}]
    assert tracking.on_hand(later)[1] == 4.0


def test_tracking_stock_on_hand_shows_unsold_lines():
    laid, unsold = market_stock.apply(SALES, rows(), SNAP)
    out = tracking.compute(laid, [], today=TODAY, unsold=unsold)["stock_on_hand"]
    lines = [l for m in out["markets"] for l in m["lines"]] if out["markets"] and "lines" in out["markets"][0] \
        else [l for m in out["markets"] for d in m["dns"] for l in d["lines"]]
    by = {l["product"]: l for l in lines}
    assert by["PEACHES DECEMBER PRINCESS CLASS 1 LARGE (PUNNET 8kg)"]["not_sold_yet"]
    assert by["GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)"]["cartons_left"] == 10
    assert out["cartons_left"] == 35 + 44 + 10


def test_procurement_counts_unsold_lines_on_the_floor():
    laid, unsold = market_stock.apply(SALES, rows(), SNAP)
    floor = procurement.on_hand_by_product(laid + unsold)
    assert floor["GRAPES CRIMSON SEEDLESS CLASS 1 NO SIZE (PUNNET 5kg)"] == 44


def test_stock_page_ties_an_unsold_note_line():
    note = {"dn": 14391, "sent_on": "2026-10-05", "market": "TSHWANE MARKET",
            "lines": [{"line_no": 1, "description": "Pink grapes", "written": "90", "cartons": 90},
                      {"line_no": 2, "description": "Dessert", "written": "35", "cartons": 35}]}
    laid, unsold = market_stock.apply(SALES, rows(), SNAP)
    out = stock_book.reconcile([note], laid, [], TODAY, unsold=unsold)
    lines = out["notes"][0]["lines"]
    assert [l["status"] for l in lines] == ["on_floor", "on_floor"]
    assert lines[0]["on_floor"] == 44 and lines[1]["on_floor"] == 35
    assert any("booked 46 fewer" in f for f in lines[0]["flags"])


def test_summary_groups_what_is_on_the_floor():
    s = market_stock.summary(rows(), SNAP, TODAY)
    m = s["markets"][0]
    assert s["cartons"] == 35 + 44 + 10
    assert m["cleared"] == 3
    assert m["notes"][0]["dn"] == 14391 and m["notes"][0]["days"] == 6


def test_stocktake_sheet_is_a_pdf_of_what_is_shown():
    from app import stocktake_sheet

    s = market_stock.summary(rows(), SNAP, TODAY)
    groups = [{"dn": n["dn"], "ref": n["supplier_ref"], "market": m["market"],
               "delivered": n["delivered_on"], "days": n["days"], "lines": n["lines"]}
              for m in s["markets"] for n in m["notes"]]
    assert stocktake_sheet.build(groups, "TSHWANE MARKET", SNAP).startswith(b"%PDF")
    # The packing code the stock report prints after the bracket is dropped.
    assert all(not l["product"].endswith(" 1") for g in groups for l in g["lines"])
