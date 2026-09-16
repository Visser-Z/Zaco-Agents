"""Stock on Hand: everything still unsold, by the market it is sitting at."""

from datetime import date

from app import tracking


def _row(cid, product, market, sent, sold, first, last=None, **kw):
    return {"consignment_id": cid, "dn": 14000 + cid, "product": product,
            "market": market, "market_agent": "Farmers Trust", "qty_received": sent,
            "cartons_sold": sold, "price": 100.0, "date_received": first,
            "last_sale": last or first, **kw}


TODAY = date(2026, 9, 16)


def test_days_on_hand_colour_green_orange_red():
    assert [tracking.stock_tier(d) for d in (0, 1, 6, 7, 13, 14, 60)] == \
        ["green", "green", "green", "orange", "orange", "red", "red"]


def test_stock_is_grouped_by_market_and_oldest_sits_first():
    sales = [_row(1, "GRAPES A", "TSHWANE MARKET", 100, 40, "2026-09-12"),   # 4 days
             _row(2, "PLUMS B", "TSHWANE MARKET", 50, 10, "2026-08-20"),     # 27 days
             _row(3, "CHERRIES C", "TSHWANE MARKET", 30, 5, "2026-09-06"),   # 10 days
             _row(4, "GRAPES D", "DURBAN MARKET", 20, 20, "2026-08-01"),     # sold out
             _row(5, "GRAPES E", "DURBAN MARKET", 60, 0, "2026-09-10")]      # 6 days
    s = tracking.stock_on_hand(sales, TODAY)
    assert [m["market"] for m in s["markets"]] == ["TSHWANE MARKET", "DURBAN MARKET"]
    tshwane = s["markets"][0]
    assert [(r["product"], r["days_on_hand"], r["tier"]) for r in tshwane["lines"]] == [
        ("PLUMS B", 27, "red"), ("CHERRIES C", 10, "orange"), ("GRAPES A", 4, "green")]
    assert (tshwane["items"], tshwane["cartons_left"], tshwane["red"]) == (3, 125, 1)
    assert s["markets"][1]["items"] == 1            # the sold-out line is not stock
    assert s["counts"] == {"green": 2, "orange": 1, "red": 1}


def test_a_consignment_sold_over_several_days_is_one_line():
    sales = [_row(7, "GRAPES A", "TSHWANE MARKET", 137, 1, "2026-09-01", "2026-09-01"),
             _row(7, "GRAPES A", "TSHWANE MARKET", 137, 58, "2026-09-03", "2026-09-05")]
    s = tracking.stock_on_hand(sales, TODAY)
    [line] = s["markets"][0]["lines"]
    assert (line["cartons_left"], line["cartons_sent"]) == (78, 137)
    assert (line["arrived"], line["days_on_hand"]) == ("2026-09-01", 15)


def test_a_pdf_date_is_the_first_sale_and_says_so():
    s = tracking.stock_on_hand([_row(1, "GRAPES A", "TSHWANE MARKET", 10, 2, "2026-09-10",
                                     source_file="dailysalesdetails.pdf · consignment 1")], TODAY)
    assert s["markets"][0]["lines"][0]["arrived_basis"] == "first_sold"
    assert s["dated_by_first_sale"] == 1


def test_a_csv_delivery_date_is_the_real_arrival_and_wins():
    sales = [_row(1, "GRAPES A", "TSHWANE MARKET", 10, 2, "2026-09-08",
                  source_file="daily.pdf"),
             _row(1, "GRAPES A", "TSHWANE MARKET", 10, 3, "2026-09-09",
                  source_file="june.csv · consignment 1")]
    line = tracking.stock_on_hand(sales, TODAY)["markets"][0]["lines"][0]
    assert (line["arrived"], line["arrived_basis"]) == ("2026-09-09", "received")


def test_a_line_closed_on_the_old_slow_list_stays_closed():
    sales = [_row(2, "PLUMS B", "TSHWANE MARKET", 50, 10, "2026-08-20")]
    ref = tracking.slow_stock(sales, TODAY)["items"][0]["ref"]
    s = tracking.stock_on_hand(sales, TODAY, closed={tracking.closed_key("slow", ref)})
    assert s["items"] == 0 and s["markets"] == []
    assert s["closed_count"] == 1


def test_a_market_that_was_never_recorded_is_named_not_dropped():
    s = tracking.stock_on_hand([_row(1, "GRAPES A", None, 10, 2, "2026-09-10")], TODAY)
    assert s["markets"][0]["market"] == tracking.UNPLACED


def test_the_month_scopes_by_arrival():
    sales = [_row(1, "GRAPES A", "TSHWANE MARKET", 10, 2, "2026-08-10"),
             _row(2, "GRAPES B", "TSHWANE MARKET", 10, 2, "2026-09-10")]
    s = tracking.compute(sales, [], today=TODAY, month="2026-09")["stock_on_hand"]
    assert [r["product"] for m in s["markets"] for r in m["lines"]] == ["GRAPES B"]
