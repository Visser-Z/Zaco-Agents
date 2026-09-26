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


# --- growing it: room to grow, and a market worth trying ---------------------

def test_a_market_that_took_everything_fast_at_the_going_rate_is_asked_for_more():
    """Replacing what sold can never grow the book. Grapes sell out in a day at
    the market average three months running, so the plan asks for a fifth more
    again, and says in the line's own figures why."""
    grow = _line(procurement.build(ROWS, PAYS, today=TODAY), GRAPES)["headroom"]
    assert grow["cartons"] == 30           # 100 expected, all of it sold, rising
    assert grow["share"] == 0.30           # a fifth, and a tenth on top for rising
    assert grow["market"] == "TSHWANE MARKET"
    assert grow["worth"] > 0
    assert "sold every carton sent, 3 months running" in grow["why"]
    assert "cleared in 2 days" in grow["why"]
    assert "up on last month" in grow["why"]


def test_nothing_is_stretched_while_it_is_still_sitting_on_the_floor():
    """Plums leave half the load unsold. However they are priced, a market
    sitting on stock is not a market short of the fruit."""
    assert _line(procurement.build(ROWS, PAYS, today=TODAY), PLUMS)["headroom"] is None


def test_selling_out_under_the_market_average_earns_no_stretch():
    """Everything sold, fast, but at four fifths of what the floor was paying:
    the way to sell more of that is to cut the price again, not a plan."""
    cheap = [_month(7001 + i * 100, "PEACHES KEISIE CLASS 1 LARGE (CARTON 5kg)",
                    "TSHWANE MARKET", "Farmers Trust", 100, 20000.0, m, of_market=0.8)
             for i, m in enumerate(("07", "08", "09"))]
    out = procurement.build(ROWS + cheap, PAYS, today=TODAY)
    line = _line(out, "PEACHES KEISIE CLASS 1 LARGE (CARTON 5kg)")
    assert line["sell_through"] == 1.0 and line["vs_market"] == 0.8
    assert line["headroom"] is None


def test_a_month_of_history_is_not_enough_to_ask_for_more():
    one = [_month(7301, "PEARS PACKHAMS CLASS 1 LARGE (CARTON 12kg)",
                  "TSHWANE MARKET", "Farmers Trust", 80, 24000.0, "09")]
    out = procurement.build(ROWS + one, PAYS, today=TODAY)
    assert _line(out, "PEARS PACKHAMS CLASS 1 LARGE (CARTON 12kg)")["headroom"] is None


def test_the_plan_adds_up_what_there_is_room_to_grow():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    grow = _line(out, GRAPES)["headroom"]
    assert out["totals"]["growth_lines"] == 1
    assert out["totals"]["growth_cartons"] == grow["cartons"]
    assert out["totals"]["growth_worth"] == grow["worth"]


# A second grape line that has only ever gone to Durban, where the same fruit
# gives back far less of the going rate than it does at Tshwane.
ONLY = "GRAPES SONITA CLASS 1 NO SIZE (PUNNET 5kg)"
STUCK = [_month(8001 + i * 100, ONLY, "DURBAN MARKET", "Grow Port Natal",
                80, 24000.0, m, of_market=0.6)
         for i, m in enumerate(("07", "08", "09"))]


def test_a_product_that_has_only_seen_one_market_is_offered_a_test_load():
    out = procurement.build(ROWS + STUCK, PAYS, today=TODAY)
    line = _line(out, ONLY)
    assert line["where_kind"] == "only"
    trial = line["trial"]
    assert trial["market"] == "TSHWANE MARKET"
    assert trial["cartons"] == 12          # a sixth of the 80 it is expected to sell
    assert trial["holds"] > trial["holds_here"]
    assert trial["worth"] > 0
    assert "of the going rate on grapes" in trial["why"]


def test_what_a_test_load_is_worth_is_this_product_lifted_by_the_gap():
    """Never the other market's rand per carton: the fruit it sells there may
    simply be dearer fruit. Sonita at 60% of the going rate against Crimson at
    100% is a two thirds lift on Sonita's own figure, not Crimson's."""
    out = procurement.build(ROWS + STUCK, PAYS, today=TODAY)
    line = _line(out, ONLY)
    trial = line["trial"]
    lift = trial["holds"] / trial["holds_here"] - 1
    assert trial["per_carton"] == round(line["back_per_carton"] * lift, 2)
    assert trial["per_carton"] < line["back_per_carton"]
    assert out["totals"]["trials"] == 1
    assert out["totals"]["trial_cartons"] == trial["cartons"]


def test_no_test_load_where_the_other_market_is_no_better():
    """Tshwane and Durban both give back the going rate on this fruit, so
    freighting it across the country proves nothing."""
    level = [_month(8501 + i * 100, ONLY, "DURBAN MARKET", "Grow Port Natal",
                    80, 24000.0, m) for i, m in enumerate(("07", "08", "09"))]
    out = procurement.build(ROWS + level, PAYS, today=TODAY)
    assert _line(out, ONLY)["trial"] is None


def test_no_test_load_on_a_product_too_small_to_read_a_result_off():
    small = [_month(8801 + i * 100, ONLY, "DURBAN MARKET", "Grow Port Natal",
                    10, 3000.0, m, of_market=0.6) for i, m in enumerate(("07", "08", "09"))]
    out = procurement.build(ROWS + small, PAYS, today=TODAY)
    line = _line(out, ONLY)
    assert line["expected_cartons"] < procurement.TRIAL_WORTH_TESTING
    assert line["trial"] is None


def test_the_plan_says_what_the_growth_figures_assume():
    out = procurement.build(ROWS + STUCK, PAYS, today=TODAY)
    assert any("Room to grow" in c for c in out["caveats"])
    assert any("test load" in c for c in out["caveats"])


# --- how long the order is for --------------------------------------------

def test_a_month_is_what_the_plan_gives_when_nothing_is_asked_for():
    out = procurement.build(ROWS, PAYS, today=TODAY)
    assert out["horizon"] == {"days": 30, "label": "the next month",
                              "scale": 1.0, "is_month": True}


def test_a_week_s_order_is_a_week_s_worth():
    """Grapes sell 100 cartons a month, so seven days of them is about 23."""
    month = _line(procurement.build(ROWS, PAYS, today=TODAY), GRAPES)
    week = _line(procurement.build(ROWS, PAYS, today=TODAY, days=7), GRAPES)
    assert week["expected_cartons"] == round(month["expected_cartons"] * 7 / 30, 1)
    assert week["expected_value"] == round(month["expected_value"] * 7 / 30, 2)
    assert week["take_on"] == round(week["expected_cartons"])


def test_stock_on_the_floor_is_not_scaled_with_the_order():
    """A week's order still has to come off the whole 150 cartons sitting at
    Durban: stock is stock, however long you are buying for."""
    week = _line(procurement.build(ROWS, PAYS, today=TODAY, days=7), PLUMS)
    month = _line(procurement.build(ROWS, PAYS, today=TODAY), PLUMS)
    assert week["on_hand"] == month["on_hand"] == 150
    assert week["take_on"] == 0


def test_the_priority_does_not_move_with_the_horizon():
    """How a product trades is not a function of how much of it is being
    bought, so the same line keeps its mark whatever period is chosen."""
    for days in (7, 14, 30, 90):
        out = procurement.build(ROWS, PAYS, today=TODAY, days=days)
        assert _line(out, GRAPES)["priority"] == "critical"
        assert _line(out, GRAPES)["score"] == _line(
            procurement.build(ROWS, PAYS, today=TODAY), GRAPES)["score"]


def test_a_short_order_says_it_is_a_rate_not_a_forecast():
    out = procurement.build(ROWS, PAYS, today=TODAY, days=7)
    assert any("cut to 7 days" in c for c in out["caveats"])
    assert any("the next 7 days" in c for c in out["caveats"])
    # A month's plan carries no such note, because nothing was scaled.
    assert not any("cut to" in c for c in procurement.build(ROWS, PAYS, today=TODAY)["caveats"])


def test_a_quarter_asks_for_three_months_of_it():
    quarter = _line(procurement.build(ROWS, PAYS, today=TODAY, days=90), GRAPES)
    month = _line(procurement.build(ROWS, PAYS, today=TODAY), GRAPES)
    assert quarter["expected_cartons"] == round(month["expected_cartons"] * 3, 1)
    assert quarter["take_on"] == round(quarter["expected_cartons"])


def test_a_test_load_is_judged_on_the_month_not_on_the_slice_being_bought():
    """A week's worth of a good line is a small number, which is no reason not
    to try it at a second market."""
    rows = ROWS + STUCK
    week = _line(procurement.build(rows, PAYS, today=TODAY, days=7), ONLY)
    assert week["expected_cartons"] < procurement.TRIAL_WORTH_TESTING
    assert week["monthly_cartons"] >= procurement.TRIAL_WORTH_TESTING
    assert week["trial"] is not None
    # Sized off the week, with the floor holding it up.
    assert week["trial"]["cartons"] == procurement.TRIAL_MIN


def test_an_odd_number_of_days_still_works():
    out = procurement.build(ROWS, PAYS, today=TODAY, days=21)
    assert out["horizon"]["label"] == "the next 21 days"
    assert out["horizon"]["scale"] == 0.7
