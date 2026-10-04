"""Where each carton of the order goes, and the sheet the trucks are loaded from."""

from app import dispatch, dispatch_sheet

PRODUCT = "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"


def dest(market, back, cartons, through=1.0, days=5.0, price=None):
    return {"market": market, "market_agent": market.title(), "back_per_carton": back,
            "cartons": cartons, "sell_through": through, "days_to_sell": days,
            "price": price or back * 1.2}


def line(take_on, *dests, market=None):
    return {"product": PRODUCT, "take_on": take_on, "destinations": list(dests),
            "market": market or (dests[0]["market"] if dests else None),
            "market_agent": None, "back_per_carton": None}


def sent(allocations):
    return [(a["market"], a["cartons"], a["over"]) for a in allocations]


# Ninety days of history, an order for thirty: a market's share is a third of
# what it took, unless its best month says it can take more.

def test_the_best_market_is_filled_to_what_it_takes_then_the_next():
    l = line(100, dest("DURBAN", 300.0, 180), dest("TSHWANE", 250.0, 300))
    assert sent(dispatch.split(l, 90, 30, "30 days")) == [("DURBAN", 60, 0), ("TSHWANE", 40, 0)]


def test_a_best_month_shows_a_market_can_take_more_than_its_average():
    l = line(100, dest("DURBAN", 300.0, 180), dest("TSHWANE", 250.0, 300))
    peak = {(PRODUCT, "DURBAN", "Durban"): 120.0}
    assert sent(dispatch.split(l, 90, 30, "30 days", peak)) == [("DURBAN", 100, 0)]


def test_more_than_any_market_takes_goes_to_the_best_and_says_so():
    l = line(200, dest("DURBAN", 300.0, 180), dest("TSHWANE", 250.0, 90))
    assert sent(dispatch.split(l, 90, 30, "30 days")) == [("DURBAN", 170, 110), ("TSHWANE", 30, 0)]


def test_a_market_that_did_not_sell_what_it_got_is_not_split_into():
    l = line(50, dest("DURBAN", 300.0, 30, through=0.4), dest("TSHWANE", 250.0, 150))
    assert sent(dispatch.split(l, 90, 30, "30 days")) == [("TSHWANE", 50, 0)]


def test_a_share_too_small_to_load_goes_with_the_rest():
    l = line(52, dest("DURBAN", 300.0, 150), dest("TSHWANE", 250.0, 300))
    # Durban takes 50; the 2 left are too few for Tshwane, so they ride with Durban.
    assert sent(dispatch.split(l, 90, 30, "30 days")) == [("DURBAN", 52, 0)]


def test_a_small_order_is_never_called_more_than_a_market_takes():
    l = line(3, dest("DURBAN", 300.0, 30))
    assert sent(dispatch.split(l, 90, 30, "30 days")) == [("DURBAN", 3, 0)]


def test_with_nothing_to_split_by_it_goes_where_the_plan_says():
    l = line(12, market="SPRINGS MARKET")
    out = dispatch.split(l, 90, 30, "30 days")
    assert sent(out) == [("SPRINGS MARKET", 12, 0)]
    assert out[0]["why"] == "the only market it has been to"


def test_the_reason_is_in_words_the_loader_needs():
    l = line(40, dest("DURBAN", 300.0, 180, through=1.0, days=2.0))
    why = dispatch.split(l, 90, 30, "30 days")[0]["why"]
    assert why == "sold 100% of what it got, clears in 2 days, takes up to 72 cartons in 30 days, and asked for more"


def plan_for_sheet():
    l = line(100, dest("DURBAN MARKET", 300.0, 180), dest("TSHWANE MARKET", 250.0, 300))
    l.update(priority="critical", fruit="Grapes", headroom=None, trial={
        "cartons": 10, "market": "SPRINGS MARKET", "market_agent": "Subtropico", "worth": 400.0,
        "why": "SPRINGS MARKET gives back 90% of the going rate on grapes"})
    plan = {"month": "2026-10", "horizon": {"days": 30, "label": "the next month", "is_month": True},
            "window": {"from": "2026-07-01", "to": "2026-09-28"}, "lines": [l]}
    return plan


def test_the_sheet_groups_by_market_and_keeps_optional_loads_apart():
    plan = plan_for_sheet()
    d = dispatch.build(plan, [], [], 3)
    markets = {g["market"]: g for g in d["markets"]}
    assert markets["DURBAN MARKET"]["cartons"] == 60
    assert markets["TSHWANE MARKET"]["cartons"] == 40
    # A test load is listed, but not counted in what is sent, and not given a
    # "should return" figure: what it is worth is a gain, said in words.
    test = markets["SPRINGS MARKET"]["lines"][0]
    assert test["kind"] == "test" and test["value"] is None
    assert "about R 400,00 more than sending these cartons to DURBAN MARKET" in test["why"]
    assert markets["SPRINGS MARKET"]["cartons"] == 0
    assert d["cartons"] == 100 and d["optional"] == 10 and d["split_lines"] == 1

    pdf = dispatch_sheet.build(plan, prepared_for="Partner")
    assert pdf[:4] == b"%PDF"
    assert dispatch_sheet.filename(plan) == "zaco-dispatch-2026-10.pdf"


def test_a_market_sitting_on_old_money_is_named_on_the_sheet():
    rows = [{"consignment_id": 117230101, "dn": 14220, "product": "STRAWBERRIES",
             "market": "TSHWANE MARKET", "market_agent": "Farmers Trust", "cartons_sold": 10,
             "price": 675.0, "sales_total": 6750.0, "last_sale": "2026-04-16",
             "date_received": "2026-04-16", "group_date": "2026-04-16", "qty_received": 31}]
    risk = dispatch.market_risk(rows, [])[("TSHWANE MARKET", "Farmers Trust")]
    assert risk["owed"] == 6750.0 and risk["oldest_days"] > 30
    assert risk["note"].startswith("R 6 750,00 owed, the oldest ")
    assert risk["note"].endswith("days: chase it before sending more")
