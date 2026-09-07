"""Sales analytics aggregation tests.

These run on plain row dicts shaped exactly like the `statements` table, so no
database is needed. The important business rule under test: value is GROSS
(cartons_sold x price), because the Daily Sales format leaves Nett blank.
"""

from datetime import date

from app import analytics


def _row(**kw) -> dict:
    base = {
        "market_agent": "Farmers Trust",
        "market": "TSHWANE MARKET",
        "description": "IMP Nect",
        "product": "NECTARINES OTHER",
        "cartons_sold": 10,
        "price": 50.0,
        "nett_total": None,
        "group_date": "2026-07-27",
        "invoice_date": "2026-07-27",
        "date_received": "2026-07-27",
        "status": "27.07",
        "created_at": "2026-07-28T09:00:00",
    }
    base.update(kw)
    return base


def test_value_is_gross_not_nett():
    # nett_total is blank on this format; value must come from cartons x price.
    row = _row(cartons_sold=10, price=50.0, nett_total=None)
    assert analytics.row_value(row) == 500.0


def test_value_tolerates_missing_numbers():
    assert analytics.row_value(_row(cartons_sold=None, price=None)) == 0.0
    assert analytics.row_value(_row(price=None)) == 0.0


def test_best_sellers_ranked_by_value_with_pareto():
    rows = [
        _row(product="WHITE GRAPES", cartons_sold=100, price=90.0),   # 9000
        _row(product="NECTARINES OTHER", cartons_sold=10, price=50.0),  # 500
        _row(product="PLUMS", cartons_sold=5, price=20.0),            # 100
    ]
    best = analytics.best_sellers(rows)
    assert [b["label"] for b in best] == ["WHITE GRAPES", "NECTARINES OTHER", "PLUMS"]
    assert best[0]["value"] == 9000.0
    # 9000 / 9600 = 0.9375 of total, so the top line alone is class A.
    assert best[0]["abc"] == "A"
    assert best[-1]["cumulative_share"] == 1.0


def test_best_sellers_group_same_product_across_markets():
    rows = [
        _row(product="NECTARINES OTHER", market="TSHWANE MARKET", cartons_sold=10, price=50.0),
        _row(product="NECTARINES OTHER", market="JOBURG MKT", cartons_sold=4, price=50.0),
    ]
    best = analytics.best_sellers(rows)
    assert len(best) == 1
    assert best[0]["cartons"] == 14
    assert best[0]["value"] == 700.0


def test_grouped_on_the_product_not_the_operator_code():
    """The short code belongs to the Excel column, not to the analytics.

    It is assigned by hand, so the same fruit can be coded on one row and not
    yet on the next. Grouping on the code split those two apart and ranked one
    product as two -- once under its code and once under its raw name.
    """
    rows = [
        _row(product="NECTARINES OTHER", description="IMP Nect", cartons_sold=10, price=50.0),
        _row(product="NECTARINES OTHER", description=None, cartons_sold=4, price=50.0),
    ]
    best = analytics.best_sellers(rows)
    assert [b["label"] for b in best] == ["NECTARINES OTHER"]
    assert best[0]["cartons"] == 14


def test_falls_back_to_the_code_when_the_product_is_missing():
    rows = [_row(product=None, description="IMP Nect", cartons_sold=1, price=1.0)]
    assert analytics.best_sellers(rows)[0]["label"] == "IMP Nect"
    rows = [_row(product=None, description=None, cartons_sold=1, price=1.0)]
    assert analytics.best_sellers(rows)[0]["label"] == analytics.UNKNOWN


def test_by_market_and_by_agent():
    rows = [
        _row(market="TSHWANE MARKET", market_agent="Farmers Trust", cartons_sold=10, price=50.0),
        _row(market="JOBURG MKT", market_agent="Subtropico", cartons_sold=2, price=50.0),
    ]
    result = analytics.compute(rows)
    by_market = {m["label"]: m["value"] for m in result["by_market"]}
    assert by_market == {"TSHWANE MARKET": 500.0, "JOBURG MKT": 100.0}
    by_agent = {a["label"]: a["value"] for a in result["by_agent"]}
    assert by_agent == {"Farmers Trust": 500.0, "Subtropico": 100.0}


def test_null_market_bucketed_as_unknown():
    rows = [_row(market=None, cartons_sold=1, price=10.0)]
    assert analytics.compute(rows)["by_market"][0]["label"] == analytics.UNKNOWN


def test_trend_by_week_and_month():
    rows = [
        _row(group_date="2026-07-27", cartons_sold=10, price=50.0),   # 2026-W31
        _row(group_date="2026-07-28", cartons_sold=2, price=50.0),    # 2026-W31
        _row(group_date="2026-08-04", cartons_sold=4, price=50.0),    # 2026-W32
    ]
    weekly = analytics.trend(rows, "week")
    assert [w["period"] for w in weekly] == ["2026-W31", "2026-W32"]
    assert weekly[0]["value"] == 600.0

    monthly = analytics.trend(rows, "month")
    assert {m["period"] for m in monthly} == {"2026-07", "2026-08"}


def test_kpis():
    rows = [
        _row(market="TSHWANE MARKET", market_agent="Farmers Trust",
             product="NECTARINES OTHER", cartons_sold=10, price=50.0, group_date="2026-07-27"),
        _row(market="JOBURG MKT", market_agent="Subtropico",
             product="PLUMS", cartons_sold=5, price=20.0, group_date="2026-08-04"),
    ]
    k = analytics.kpis(rows)
    assert k["total_value"] == 600.0
    assert k["total_cartons"] == 15
    assert k["statement_count"] == 2
    assert k["product_count"] == 2
    assert k["market_count"] == 2
    assert k["agent_count"] == 2
    assert k["first_date"] == "2026-07-27"
    assert k["last_date"] == "2026-08-04"
    # Nothing came back in these rows, so sold and net agree and the rate is 0.
    assert k["cartons_sold"] == 15
    assert k["cartons_returned"] == 0
    assert k["gross_value"] == 600.0
    assert k["return_rate"] == 0.0


def test_kpis_report_sold_returned_and_net_separately():
    """April 2026, the month this was built for: 3 448 cartons sold, 280 of them
    came back, 3 168 net -- and R593 067,80 rung up against R90 100,00 returned.

    Reported as the net alone it is a clean month of 3 168 cartons and
    R502 967,80, which is what the workbook is right to hold and what the
    dashboard was wrong to be the only view of.
    """
    rows = [
        _row(cartons_sold=2168, price=200.0,
             cartons_returned=200, returns_total=64400.0),
        # Carries the odd cents: 1 000 x 69.3678 = 69 367.80.
        _row(cartons_sold=1000, price=69.3678,
             cartons_returned=80, returns_total=25700.0),
    ]
    k = analytics.kpis(rows)

    assert k["cartons_sold"] == 3448        # what sold
    assert k["cartons_returned"] == 280     # what came back
    assert k["total_cartons"] == 3168       # the net, as the workbook holds it

    assert k["gross_value"] == 593067.80
    assert k["returns_value"] == 90100.00
    assert k["total_value"] == 502967.80

    # Measured against what SOLD. Over the 3 168 that stuck it would read 8.8%,
    # which flatters the month by dividing by the smaller number.
    assert k["return_rate"] == round(280 / 3448, 4) == 0.0812


def test_returns_are_read_as_zero_where_the_source_never_carried_them():
    """History recorded before returns were captured, and the account-sales PDF,
    leave both columns NULL. That must read as nothing returned rather than
    poisoning the totals -- but it is not evidence that nothing was."""
    rows = [_row(cartons_sold=10, price=50.0, cartons_returned=None, returns_total=None)]
    k = analytics.kpis(rows)
    assert (k["cartons_sold"], k["cartons_returned"], k["total_cartons"]) == (10, 0, 10)
    assert k["gross_value"] == k["total_value"] == 500.0
    assert k["return_rate"] == 0.0


# --- product performance (the signals behind buying advice) ---------------

def test_days_to_sell_counts_first_sale_to_last():
    assert analytics.days_to_sell({"date_received": "2026-07-01", "last_sale": "2026-07-08"}) == 7
    # Cleared the same day.
    assert analytics.days_to_sell({"date_received": "2026-07-01", "last_sale": "2026-07-01"}) == 0
    # Missing either end, or nonsense ordering, gives nothing rather than a guess.
    assert analytics.days_to_sell({"date_received": "2026-07-01"}) is None
    assert analytics.days_to_sell({"date_received": "2026-07-08", "last_sale": "2026-07-01"}) is None


def test_product_performance_metrics():
    rows = [
        _row(cartons_sold=80, price=100.0, qty_received=100,
             date_received="2026-07-01", last_sale="2026-07-03", group_date="2026-07-01"),
        _row(cartons_sold=20, price=100.0, qty_received=100,
             date_received="2026-08-01", last_sale="2026-08-01", group_date="2026-08-01"),
    ]
    p = analytics.product_performance(rows)[0]
    assert p["label"] == "NECTARINES OTHER"
    assert p["cartons"] == 100 and p["value"] == 10000.0
    assert p["avg_price"] == 100.0
    assert p["sell_through"] == 0.5           # 100 sold of 200 sent
    assert p["avg_days_to_sell"] == 1.0       # (2 days + 0 days) / 2
    assert p["slowest_days"] == 2
    assert p["months_seen"] == ["2026-07", "2026-08"]


def test_sell_through_ignores_rows_with_no_sent_quantity():
    """Counting sales from a row that never recorded a sent quantity pushes
    sell-through above 100%, which is nonsense the assistant would repeat."""
    rows = [
        _row(description="X", cartons_sold=50, price=1.0, qty_received=100),
        _row(description="X", cartons_sold=90, price=1.0, qty_received=None),
    ]
    p = analytics.product_performance(rows)[0]
    assert p["sell_through"] == 0.5           # not 1.4
    assert p["cartons"] == 140                # total sold is still complete


def test_one_delivery_settled_twice_is_not_counted_as_two():
    """Rows are account sales now. A consignment settled over two of them is two
    rows, both carrying the delivery's full Qty Sent -- so summing that column
    would report 200 cartons sent when 100 went out, and halve sell-through.
    The trap this guards is that every figure still looks plausible."""
    rows = [
        _row(description="IMP Nect", consignment_id=900, cartons_sold=60, price=100.0,
             qty_received=100, date_received="2026-07-01", last_sale="2026-07-03"),
        _row(description="IMP Nect", consignment_id=900, cartons_sold=30, price=100.0,
             qty_received=100, date_received="2026-07-01", last_sale="2026-07-09"),
    ]
    p = analytics.product_performance(rows)[0]
    assert analytics.cartons_sent(rows) == 100
    assert p["sell_through"] == 0.9            # 90 of 100, not 90 of 200
    assert p["consignments"] == 1              # one delivery, not two rows
    assert p["cartons"] == 90                  # sold still adds up per account sale
    # The fruit sat there until the last sale of the LAST run, not the first.
    assert p["avg_days_to_sell"] == 8.0 and p["slowest_days"] == 8


def test_rows_without_a_consignment_id_are_still_counted_separately():
    """History recorded before the split, and every PDF import, has no
    consignment id and was one row per consignment already. Pooling them on a
    blank id would merge unrelated deliveries into one."""
    rows = [
        _row(description="X", cartons_sold=50, price=1.0, qty_received=100),
        _row(description="X", cartons_sold=50, price=1.0, qty_received=100),
    ]
    assert analytics.cartons_sent(rows) == 200
    assert analytics.product_performance(rows)[0]["consignments"] == 2


def test_product_performance_without_dates_or_quantities():
    p = analytics.product_performance([_row(description="X", qty_received=None,
                                            date_received=None, last_sale=None)])[0]
    assert p["sell_through"] is None
    assert p["avg_days_to_sell"] is None and p["slowest_days"] is None


# --- which date a row is bucketed under ------------------------------------

def test_a_row_is_bucketed_on_the_day_it_sold():
    """The report a row is read from states the day it sold, and that lands on
    last_sale. group_date is the consignment's date -- the day the load was
    sent -- so a week of reports collapses onto the few days loads arrived."""
    row = {"last_sale": "2026-08-07", "group_date": "2026-08-01",
           "invoice_date": "2026-08-02", "date_received": "2026-07-28"}
    assert analytics.row_date(row) == date(2026, 8, 7)


def test_insights_and_tracking_date_a_row_the_same_way():
    """The two pages answering "when did this sell" differently is what made a
    week look like three days on one page and seven on the other."""
    from app import tracking
    rows = [
        {"last_sale": "2026-08-07", "group_date": "2026-08-01"},
        {"last_sale": None, "group_date": "2026-08-04"},
        {"last_sale": "2026-08-08", "group_date": None},
    ]
    for row in rows:
        assert str(analytics.row_date(row)) == tracking.selling_day(row)


def test_history_without_a_sale_date_still_places_itself():
    """Rows recorded before the sale date was captured must stay dated, or a
    period filter would drop them entirely."""
    assert analytics.row_date({"group_date": "2026-08-01"}) == date(2026, 8, 1)
    assert analytics.row_date({"invoice_date": "2026-08-02"}) == date(2026, 8, 2)
    assert analytics.row_date({"date_received": "2026-07-28"}) == date(2026, 7, 28)
    assert analytics.row_date({}) is None
