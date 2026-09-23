"""The buy plan: what to take on, how much, and where to send it."""

from datetime import date

from app import procurement

GRAPES = "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"
PLUMS = "PLUMS FORTUNE CLASS 2 LARGE (ECONOMIC PACK 8kg)"
TODAY = date(2026, 9, 22)


def _row(cid, product, market, agent, sold, value, day, received, qty, of_market=1.0):
    """One consignment. `of_market` is what it fetched against the market's own
    average for that commodity, which is what the price half of the score reads."""
    price = value / sold
    return {"consignment_id": cid, "product": product, "market": market, "market_agent": agent,
            "cartons_sold": sold, "price": price, "sales_total": value,
            "market_avg": round(price / of_market, 2),
            "last_sale": day, "date_received": received, "group_date": received,
            "qty_received": qty, "qty_amended": qty, "dn": cid // 100}


def _month(cid, product, market, agent, sold, value, month, qty=None, day="03", start="01",
           of_market=1.0):
    return _row(cid, product, market, agent, sold, value,
                f"2026-{month}-{day}", f"2026-{month}-{start}", qty or sold, of_market)


def _pay(acc, gross, nett, day):
    return {"accsale": acc, "gross": gross, "nett": nett, "date": day, "lines": []}


# Grapes: three months at Tshwane, everything sold, moving up.
# Plums: three months at Durban, half of it sold, slipping.
ROWS = [
    _month(1001, GRAPES, "TSHWANE MARKET", "Farmers Trust", 100, 40000.0, "07"),
    _month(1101, GRAPES, "TSHWANE MARKET", "Farmers Trust", 100, 42000.0, "08"),
    _month(1201, GRAPES, "TSHWANE MARKET", "Farmers Trust", 100, 50000.0, "09"),
    _month(2001, PLUMS, "DURBAN MARKET", "Grow Port Natal", 50, 10000.0, "07", qty=100,
           day="12", of_market=0.7),
    _month(2101, PLUMS, "DURBAN MARKET", "Grow Port Natal", 50, 9000.0, "08", qty=100,
           day="12", of_market=0.7),
    _month(2201, PLUMS, "DURBAN MARKET", "Grow Port Natal", 50, 6000.0, "09", qty=100,
           day="12", of_market=0.7),
]
PAYS = [_pay("PRE*BT*1", 10000.0, 8500.0, "2026-09-15"),
        _pay("DUR*13*1", 10000.0, 8500.0, "2026-09-15")]


def _line(out, product):
    return next(l for l in out["lines"] if l["product"] == product)


def test_the_plan_ranks_what_sells_and_clears_above_what_does_not():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    assert [l["product"] for l in out["lines"]] == [GRAPES, PLUMS]
    assert _line(out, GRAPES)["priority"] == "critical"
    assert _line(out, GRAPES)["score"] > _line(out, PLUMS)["score"]


def test_how_much_to_take_on_is_what_will_sell_less_what_is_on_the_floor():
    """Plums leave 50 of every 100 cartons on the floor, so the next load is
    the month's expectation less the stock already sitting there."""
    out = procurement.build(ROWS, PAYS, today=TODAY)
    plums = _line(out, PLUMS)
    assert plums["expected_cartons"] == 50.0
    assert plums["on_hand"] == 150            # three consignments, 50 left on each
    assert plums["take_on"] == 0              # nothing needed, it is already there
    grapes = _line(out, GRAPES)
    assert (grapes["on_hand"], grapes["take_on"]) == (0, 100)


def test_every_line_carries_where_to_send_it():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    grapes = _line(out, GRAPES)
    assert (grapes["market"], grapes["market_agent"]) == ("TSHWANE MARKET", "Farmers Trust")
    assert grapes["where_kind"] == "only"     # never sent anywhere else
    assert grapes["destinations"][0]["back_per_carton"] is not None


def test_a_product_sent_to_two_markets_names_the_better_one():
    rows = ROWS + [
        _month(3001, GRAPES, "DURBAN MARKET", "Grow Port Natal", 20, 4000.0, "08", qty=40),
        _month(3101, GRAPES, "DURBAN MARKET", "Grow Port Natal", 20, 4000.0, "09", qty=40)]
    grapes = _line(procurement.build(rows, PAYS, today=TODAY), GRAPES)
    assert (grapes["where_kind"], grapes["market"]) == ("best", "TSHWANE MARKET")
    assert grapes["lead_per_carton"] > 0


def test_a_product_that_stopped_trading_is_off_the_plan():
    """What is resting is not something to buy, so it does not appear."""
    old = _month(4001, "CHERRIES OTHER CLASS 1 LARGE", "TSHWANE MARKET", "Farmers Trust",
                 10, 5000.0, "04")
    out = procurement.build(ROWS + [old], PAYS, today=TODAY)
    assert all("CHERRIES" not in l["product"] for l in out["lines"])


def test_the_plan_says_what_it_is_counting_and_what_it_cannot_know():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    assert out["totals"]["cartons"] == 100 and out["totals"]["to_take_on"] == 1
    assert any("consignment" in c and "margin" in c for c in out["caveats"])
    assert [p["key"] for p in out["priorities"]][0] == "critical"


def test_an_empty_book_is_an_empty_plan():
    out = procurement.build([], [], today=TODAY)
    assert out["lines"] == [] and out["totals"]["cartons"] == 0


def test_a_line_already_covered_by_stock_sits_under_the_ones_to_act_on():
    """Cotton Candy grapes came top of the live plan with 149 cartons already
    on the floor and nothing to take on, above lines needing a decision."""
    rows = ROWS + [
        _month(5001, "GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)",
               "TSHWANE MARKET", "Farmers Trust", 60, 30000.0, "07", qty=120),
        _month(5101, "GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)",
               "TSHWANE MARKET", "Farmers Trust", 60, 31000.0, "08", qty=120),
        _month(5201, "GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)",
               "TSHWANE MARKET", "Farmers Trust", 60, 33000.0, "09", qty=120)]
    out = procurement.build(rows, PAYS, today=TODAY)
    covered = _line(out, "GRAPES COTTON CANDY CLASS 1 NO SIZE (PUNNET 5kg)")
    assert covered["on_hand"] == 180 and covered["take_on"] == 0
    assert "covered by what is already there" in covered["reasons"]
    for group in out["priorities"]:
        takes = [l["take_on"] > 0 for l in group["lines"]]
        assert takes == sorted(takes, reverse=True), group["key"]


def test_same_day_selling_reads_as_same_day():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    assert all("clears in 0 days" not in r for l in out["lines"] for r in l["reasons"])


def test_a_band_is_a_fixed_mark_on_the_score():
    """The same score is the same priority in any month, so this month's plan
    can be read against last month's."""
    assert procurement.band(1.0) == procurement.band(0.85) == "critical"
    assert procurement.band(0.849) == procurement.band(0.75) == "high"
    assert procurement.band(0.749) == procurement.band(0.60) == "steady"
    assert procurement.band(0.599) == procurement.band(0.0) == "hold"
    # A one-line plan is not a plan of one Critical line: the mark is the mark.
    assert procurement.band(0.62) == "steady"


def test_a_weak_month_crowns_nobody():
    """Ranked bands always put something at the top. Half-selling plums that
    take a fortnight to clear are not the week's Critical line just because
    nothing better was on offer."""
    weak = [_month(6001, PLUMS, "DURBAN MARKET", "Grow Port Natal", 20, 1000.0, m, qty=100,
                   day="14", of_market=0.8) for m in ("07", "08", "09")]
    out = procurement.build(weak, PAYS, today=TODAY)
    assert [l["priority"] for l in out["lines"]] == ["hold"]
    assert [g["key"] for g in out["priorities"]] == ["hold"]
