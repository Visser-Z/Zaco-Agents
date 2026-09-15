"""Sales per day: what came in, what came back, and where it happened.

The client's complaint was that a number on this screen could not be read
without guessing what it meant. So the tests here are mostly about a figure
saying which question it answers: money or cartons, net or gross, this market
or all of them.
"""

from app import tracking

GRAPES = "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"
PLUMS = "PLUMS FORTUNE CLASS 2 LARGE (ECONOMIC PACK 8kg)"


def _sale(day, product, cartons, price, *, market="TSHWANE MARKET",
          agent="Farmers Trust", group=None, back=0, back_value=0.0, dn=14587):
    return {
        "product": product, "description": group or "Imp White Grapes",
        "cartons_sold": cartons, "price": price,
        "sales_total": round(cartons * price, 2),
        "cartons_returned": back, "returns_total": back_value,
        "last_sale": day, "group_date": day, "date_received": day,
        "market": market, "market_agent": agent,
        "dn": dn, "supplier_ref": dn, "consignment_id": dn, "stm_no": dn,
        "qty_received": 500,
    }


# --- the day's money -------------------------------------------------------

def test_a_day_reports_value_and_cartons_separately():
    out = tracking.sales_by_day([_sale("2026-09-07", GRAPES, 10, 100.0)])
    day = out["days"][0]
    assert day["value"] == 1000.0
    assert day["cartons"] == 10


def test_what_came_back_is_reported_in_money_as_well_as_cartons():
    """"Back: 2" says nothing about what it cost. The client asked what the
    returns were worth, and on which products."""
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 8, 100.0, back=2, back_value=200.0)])
    day = out["days"][0]
    assert (day["returned"], day["returns_value"]) == (2, 200.0)
    assert (day["products"][0]["returned"], day["products"][0]["returns_value"]) == (2, 200.0)


def test_a_day_with_nothing_returned_says_nothing_came_back():
    day = tracking.sales_by_day([_sale("2026-09-07", GRAPES, 10, 100.0)])["days"][0]
    assert day["returned"] == 0 and day["returns_value"] == 0.0


# --- by group, by market ---------------------------------------------------

def test_a_day_breaks_down_by_market():
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 10, 100.0, market="TSHWANE MARKET"),
        _sale("2026-09-07", GRAPES, 5, 100.0, market="DURBAN MARKET")])
    markets = out["days"][0]["markets"]
    assert [m["market"] for m in markets] == ["TSHWANE MARKET", "DURBAN MARKET"]
    assert [m["value"] for m in markets] == [1000.0, 500.0]


def test_each_product_says_which_markets_it_sold_at_that_day():
    """"This group sold X at this market today" is the question."""
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 10, 100.0, market="TSHWANE MARKET"),
        _sale("2026-09-07", GRAPES, 5, 100.0, market="DURBAN MARKET"),
        _sale("2026-09-07", PLUMS, 4, 50.0, market="DURBAN MARKET", group="Imp Plums")])
    grapes = next(p for p in out["days"][0]["products"] if p["product"] == GRAPES)
    assert [(m["market"], m["value"]) for m in grapes["markets"]] == [
        ("TSHWANE MARKET", 1000.0), ("DURBAN MARKET", 500.0)]
    plums = next(p for p in out["days"][0]["products"] if p["product"] == PLUMS)
    assert [m["market"] for m in plums["markets"]] == ["DURBAN MARKET"]


def test_a_product_carries_the_operators_own_group_code():
    out = tracking.sales_by_day([_sale("2026-09-07", PLUMS, 4, 50.0, group="Imp Plums")])
    assert out["days"][0]["products"][0]["group"] == "Imp Plums"


def test_a_row_with_no_market_is_named_rather_than_left_blank():
    """A blank market vanishes from a share table; a named one can be chased."""
    out = tracking.sales_by_day([dict(_sale("2026-09-07", GRAPES, 10, 100.0), market=None)])
    assert out["days"][0]["markets"][0]["market"] == tracking.UNPLACED


# --- which market is doing better ------------------------------------------

def test_market_share_is_of_value_not_cartons():
    """A market moving a lot of cheap fruit is not the one carrying the day."""
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 10, 300.0, market="TSHWANE MARKET"),   # R3 000
        _sale("2026-09-07", PLUMS, 100, 10.0, market="DURBAN MARKET")])    # R1 000
    shares = {m["market"]: m["share"] for m in out["markets"]}
    assert shares["TSHWANE MARKET"] == 0.75
    assert shares["DURBAN MARKET"] == 0.25


def test_market_shares_are_ordered_best_first_and_add_up():
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 1, 100.0, market="SPRINGS MARKET"),
        _sale("2026-09-07", GRAPES, 6, 100.0, market="TSHWANE MARKET"),
        _sale("2026-09-06", GRAPES, 3, 100.0, market="DURBAN MARKET")])
    assert [m["market"] for m in out["markets"]] == [
        "TSHWANE MARKET", "DURBAN MARKET", "SPRINGS MARKET"]
    assert round(sum(m["share"] for m in out["markets"]), 4) == 1.0


def test_market_share_spans_the_whole_period_not_one_day():
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 5, 100.0, market="TSHWANE MARKET"),
        _sale("2026-09-06", GRAPES, 5, 100.0, market="TSHWANE MARKET")])
    assert out["markets"][0]["value"] == 1000.0


# --- the month total, beside the days --------------------------------------

def test_the_month_total_comes_back_with_the_days():
    """It must not sit behind a filter: the client reads it beside the days."""
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 10, 100.0),
        _sale("2026-09-04", GRAPES, 5, 100.0),
        _sale("2026-08-31", GRAPES, 2, 100.0)])
    months = {m["month"]: m for m in out["months"]}
    assert months["2026-09"]["value"] == 1500.0
    assert months["2026-09"]["days"] == 2
    assert months["2026-08"]["value"] == 200.0


def test_months_come_back_newest_first():
    out = tracking.sales_by_day([
        _sale("2026-08-31", GRAPES, 2, 100.0), _sale("2026-09-07", GRAPES, 1, 100.0)])
    assert [m["month"] for m in out["months"]] == ["2026-09", "2026-08"]


def test_a_month_says_which_days_it_runs_between():
    out = tracking.sales_by_day([
        _sale("2026-09-07", GRAPES, 1, 100.0), _sale("2026-09-02", GRAPES, 1, 100.0)])
    m = out["months"][0]
    assert (m["first"], m["last"]) == ("2026-09-02", "2026-09-07")


# --- nothing is hidden -----------------------------------------------------

def test_every_day_comes_back_with_no_truncation():
    """The client's request was that nothing gets left out of this screen."""
    rows = [_sale(f"2026-09-{d:02d}", GRAPES, 1, 100.0) for d in range(1, 29)]
    out = tracking.sales_by_day(rows)
    assert len(out["days"]) == 28
    assert out["totals"]["days"] == 28


def test_the_period_total_matches_the_days_it_is_made_of():
    rows = [_sale("2026-09-07", GRAPES, 10, 100.0),
            _sale("2026-09-06", PLUMS, 4, 50.0, back=1, back_value=50.0)]
    out = tracking.sales_by_day(rows)
    assert out["totals"]["value"] == round(sum(d["value"] for d in out["days"]), 2)
    assert out["totals"]["returns_value"] == 50.0


def test_the_window_still_bounds_what_comes_back():
    rows = [_sale("2026-09-07", GRAPES, 1, 100.0), _sale("2026-08-07", GRAPES, 1, 100.0)]
    out = tracking.sales_by_day(rows, start="2026-09-01", end="2026-09-30")
    assert [d["date"] for d in out["days"]] == ["2026-09-07"]
    assert [m["month"] for m in out["months"]] == ["2026-09"]
