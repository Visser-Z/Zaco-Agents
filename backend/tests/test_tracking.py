"""The Tracking view: paid vs outstanding, sales per day, slow stock.

Pure functions over saved-shaped dicts, so no database is needed. The point of
the tab is that it answers from saved data, so these check the three questions
hold together over a small hand-built history.
"""

from datetime import date

from app import tracking


def _sale(product, cartons, price, day, dn=100, sent=None, received=None,
          last_sale=None, consignment_id=None, returned=0, ret_value=0.0):
    return {
        "dn": dn, "product": product, "description": None,
        "market_agent": "Farmers Trust",
        "cartons_sold": cartons, "cartons_returned": returned,
        "returns_total": ret_value, "price": price,
        "qty_received": sent, "consignment_id": consignment_id,
        "group_date": day, "date_received": received or day, "last_sale": last_sale or day,
        "nett_total": None, "payment_refs": None,
    }


def _payment(dn, product, gross, nett, accsale="PRE*BT*1"):
    return {"dn": dn, "accsale": accsale, "stm_no": 1, "market_agent": "Farmers Trust",
            "supplier_ref": f"20026*{dn}", "date": "2026-08-05", "nett": nett, "gross": gross,
            "deductions": round(gross - nett, 2), "vat": 0.0,
            "lines": [{"product": product, "delivered": 0, "sold": 0, "sales_total": gross}]}


# --- payments -------------------------------------------------------------

def test_a_sale_with_a_matching_payment_is_paid():
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01", dn=100)]
    payments = [_payment(100, "GRAPES", gross=1000.0, nett=850.0)]
    p = tracking.payment_status(sales, payments)
    assert p["batches_paid"] == 1
    assert p["batches_outstanding"] == 0
    assert p["total_paid"] == 850.0
    assert p["still_to_come"] == 0.0


def test_a_sale_with_no_payment_is_still_to_come():
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01", dn=100)]
    p = tracking.payment_status(sales, [])
    assert p["batches_outstanding"] == 1
    assert p["still_to_come"] == 1000.0        # the whole gross is owed
    assert p["oldest_outstanding"] == "2026-08-01"


def test_still_to_come_is_only_the_unpaid_shortfall():
    """Sold more than the payment covered: only the shortfall is owed. Paid for
    more than sold: nothing is owed, and it is surfaced separately rather than
    netted off against real money owed elsewhere."""
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01", dn=100),   # sold 1000
             _sale("PLUMS", 10, 100.0, "2026-08-01", dn=200)]    # sold 1000
    payments = [_payment(100, "GRAPES", gross=600.0, nett=500.0),   # part paid
                _payment(200, "PLUMS", gross=1200.0, nett=1000.0)]  # over paid
    p = tracking.payment_status(sales, payments)
    assert p["still_to_come"] == 400.0         # 1000 - 600, and nothing for plums
    assert p["batches_outstanding"] == 1
    # The over-payment is reported, never quietly cancelling out the shortfall.
    assert [r["overpaid"] for r in p["overpaid"]] == [200.0]
    assert p["total_paid"] == 1500.0           # both netts landed


def test_a_payment_with_nothing_sold_is_an_exception_not_dropped():
    payments = [_payment(999, "MANGOES", gross=500.0, nett=400.0)]
    p = tracking.payment_status([], payments)
    assert len(p["unmatched"]) == 1
    assert p["unmatched"][0]["dn"] == 999
    assert p["unmatched"][0]["paid"] == 500.0


# --- sales per day --------------------------------------------------------

def test_sales_group_by_day_newest_first_net_of_returns():
    sales = [
        _sale("GRAPES", 10, 100.0, "2026-08-01"),
        _sale("PLUMS", 5, 50.0, "2026-08-01"),
        _sale("GRAPES", 8, 100.0, "2026-08-03", returned=2, ret_value=200.0),
    ]
    days = tracking.sales_by_day(sales)["days"]
    assert [d["date"] for d in days] == ["2026-08-03", "2026-08-01"]
    assert days[0]["cartons"] == 8 and days[0]["returned"] == 2
    # Within a day, products rank by value; grapes (1000) over plums (250).
    assert [p["product"] for p in days[1]["products"]] == ["GRAPES", "PLUMS"]


# --- slow to sell ---------------------------------------------------------

def test_a_consignment_still_on_the_floor_is_flagged_by_age():
    # Sent 100, sold 10 across one consignment, delivered 2026-08-01.
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-02", sent=100,
                   received="2026-08-01", consignment_id=1)]
    s = tracking.slow_stock(sales, today=date(2026, 8, 20))
    assert s["flagged"] == 1
    item = s["items"][0]
    assert item["cartons_left"] == 90
    assert item["days_on_floor"] == 19
    assert item["tier"] == "dead"              # well past the default 15-day line


def test_a_cleared_consignment_is_not_slow():
    sales = [_sale("GRAPES", 100, 100.0, "2026-08-02", sent=100,
                   received="2026-08-01", consignment_id=1)]
    assert tracking.slow_stock(sales, today=date(2026, 8, 20))["flagged"] == 0


def test_qty_sent_counts_once_per_consignment_across_days():
    """The same consignment sold on two days must not read as 200 sent. If it
    did, sell-through and what is 'left' would both be nonsense."""
    sales = [
        _sale("GRAPES", 10, 100.0, "2026-08-02", sent=100, received="2026-08-01", consignment_id=1),
        _sale("GRAPES", 5, 100.0, "2026-08-03", sent=100, received="2026-08-01", consignment_id=1),
    ]
    s = tracking.slow_stock(sales, today=date(2026, 8, 20))
    assert s["items"][0]["cartons_sent"] == 100     # once, not 200
    assert s["items"][0]["cartons_left"] == 85      # 100 - (10 + 5)


def test_thresholds_fall_back_to_the_brief_without_enough_history():
    bands = tracking.slow_bands([_sale("X", 1, 1.0, "2026-08-01")])
    assert (bands["watch"], bands["slow"], bands["dead"]) == (5, 10, 15)
    assert "default" in bands["from"]


def test_thresholds_are_read_from_the_data_when_there_is_enough():
    # Ten cleared consignments, most quick, a slow tail at 12 and 20 days.
    spans = [1, 1, 2, 2, 3, 3, 4, 5, 12, 20]
    sales = []
    for i, span in enumerate(spans):
        sales.append(_sale("X", 10, 1.0, "2026-08-01", sent=10, consignment_id=i,
                            received="2026-08-01",
                            last_sale=date(2026, 8, 1 + span).isoformat()))
    bands = tracking.slow_bands(sales, today=date(2026, 9, 1))
    assert bands["from"].startswith("10 cleared")
    assert bands["watch"] >= 4 and bands["slow"] >= bands["watch"] + 3


# --- narrowing the per-day list to a date range ---------------------------

def test_a_date_range_narrows_the_day_list():
    sales = [_sale("A", 1, 10.0, "2026-08-01"), _sale("B", 1, 10.0, "2026-08-05"),
             _sale("C", 1, 10.0, "2026-08-09")]
    days = tracking.sales_by_day(sales, start="2026-08-04", end="2026-08-06")["days"]
    assert [d["date"] for d in days] == ["2026-08-05"]


def test_an_open_ended_range_works_from_either_side():
    sales = [_sale("A", 1, 10.0, "2026-08-01"), _sale("B", 1, 10.0, "2026-08-09")]
    assert len(tracking.sales_by_day(sales, start="2026-08-05")["days"]) == 1
    assert len(tracking.sales_by_day(sales, end="2026-08-05")["days"]) == 1
    assert len(tracking.sales_by_day(sales)["days"]) == 2


def test_the_date_range_never_narrows_what_is_owed():
    """Money owed from an earlier day is still owed. Filtering the outstanding
    list to the window would quietly understate the exposure."""
    sales = [_sale("A", 10, 100.0, "2026-08-01"), _sale("B", 10, 100.0, "2026-08-09")]
    out = tracking.compute(sales, [], today=date(2026, 8, 10),
                           start="2026-08-09", end="2026-08-09")
    assert len(out["sales_by_day"]["days"]) == 1          # the day list narrows
    assert out["payments"]["still_to_come"] == 2000.0     # the exposure does not


def test_the_span_bounds_the_pickers():
    sales = [_sale("A", 1, 10.0, "2026-08-03"), _sale("B", 1, 10.0, "2026-08-01")]
    assert tracking.date_span(sales) == {"first": "2026-08-01", "last": "2026-08-03"}


def test_the_outstanding_list_is_not_truncated():
    """It is printed and worked down, so every line has to be there."""
    sales = [_sale(f"P{i}", 1, 100.0, "2026-08-01", dn=i) for i in range(60)]
    p = tracking.payment_status(sales, [])
    assert len(p["outstanding"]) == 60


# --- exactly what sold, per product, per day ------------------------------

def test_each_day_says_what_sold_at_what_price():
    """'How much sold' is not enough to act on. The owner needs the products,
    what each fetched a carton, and what the market was paying for it."""
    sales = [
        dict(_sale("GRAPES", 20, 80.0, "2026-08-03"), market_avg=112.29),
        dict(_sale("CHERRIES", 5, 200.0, "2026-08-03"), market_avg=None),
    ]
    day = tracking.sales_by_day(sales)["days"][0]
    grapes, cherries = day["products"]           # ranked by value: 1600 then 1000
    assert (grapes["product"], grapes["cartons"], grapes["value"]) == ("GRAPES", 20, 1600.0)
    assert grapes["price"] == 80.0               # what it actually fetched
    assert grapes["market_avg"] == 112.29        # what the market was paying
    assert grapes["agents"] == ["Farmers Trust"]
    # No market average on the report means unknown, never zero.
    assert cherries["market_avg"] is None
    assert cherries["price"] == 200.0


def test_a_products_market_average_is_weighted_by_cartons():
    """A one-carton line must not outvote a hundred-carton one."""
    sales = [
        dict(_sale("GRAPES", 100, 80.0, "2026-08-03", dn=1), market_avg=100.0),
        dict(_sale("GRAPES", 1, 80.0, "2026-08-03", dn=2), market_avg=500.0),
    ]
    p = tracking.sales_by_day(sales)["days"][0]["products"][0]
    assert p["cartons"] == 101
    assert p["market_avg"] == round((100 * 100 + 500 * 1) / 101, 2)   # 103.96, not 300


def test_returns_show_against_the_product_that_came_back():
    sales = [_sale("GRAPES", 8, 100.0, "2026-08-03", returned=2, ret_value=200.0)]
    p = tracking.sales_by_day(sales)["days"][0]["products"][0]
    assert (p["cartons"], p["returned"]) == (8, 2)


# --- day-to-day performance of chosen products ----------------------------

def test_a_product_gets_a_day_by_day_series():
    sales = [
        dict(_sale("GRAPES", 20, 100.0, "2026-08-01"), market_avg=120.0),
        dict(_sale("GRAPES", 10, 80.0, "2026-08-03"), market_avg=115.0),
        _sale("PLUMS", 5, 50.0, "2026-08-02"),
    ]
    out = tracking.product_trend(sales, ["GRAPES"])
    assert out["selected"] == ["GRAPES"]
    s = out["series"][0]
    assert [d["date"] for d in s["days"]] == ["2026-08-01", "2026-08-03"]   # oldest first
    assert [d["price"] for d in s["days"]] == [100.0, 80.0]
    assert [d["market_avg"] for d in s["days"]] == [120.0, 115.0]
    assert s["cartons"] == 30 and s["value"] == 2800.0
    assert s["price_move"] == -20.0        # R100 down to R80


def test_the_price_move_spans_days_it_actually_sold():
    """A gap in the middle is not a price change, so the move runs between the
    first and last day the product moved, not the ends of the window."""
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01"),
             _sale("GRAPES", 10, 130.0, "2026-08-09")]
    s = tracking.product_trend(sales, ["GRAPES"])["series"][0]
    assert (s["first_price"], s["last_price"], s["price_move"]) == (100.0, 130.0, 30.0)
    assert s["days_sold"] == 2


def test_with_nothing_chosen_it_follows_the_money():
    sales = [_sale("SMALL", 1, 10.0, "2026-08-01"),
             _sale("BIG", 100, 100.0, "2026-08-01"),
             _sale("MID", 10, 100.0, "2026-08-01"),
             _sale("TINY", 1, 1.0, "2026-08-01")]
    out = tracking.product_trend(sales, None)
    assert out["selected"] == ["BIG", "MID", "SMALL"]      # top three by value
    assert out["available"][0] == "BIG"


def test_a_product_that_does_not_exist_falls_back_rather_than_showing_nothing():
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01")]
    out = tracking.product_trend(sales, ["NOT A REAL PRODUCT"])
    assert out["selected"] == ["GRAPES"]


def test_the_trend_respects_the_date_range():
    sales = [_sale("GRAPES", 10, 100.0, "2026-08-01"),
             _sale("GRAPES", 10, 100.0, "2026-08-09")]
    s = tracking.product_trend(sales, ["GRAPES"], start="2026-08-05")["series"][0]
    assert [d["date"] for d in s["days"]] == ["2026-08-09"]
