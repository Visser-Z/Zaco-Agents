"""Looking forward: what the book supports saying about next month, and what it
does not. The second half matters more than the first -- a projection that
sounds confident on four consignments is worse than no projection."""

from datetime import date

from app import forecast

GRAPES = "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"
PLUMS = "PLUMS FORTUNE CLASS 2 LARGE (ECONOMIC PACK 8kg)"


def _sale(product, day, cartons, price, *, cid=1, sent=None,
          market="TSHWANE MARKET", agent="Farmers Trust", received=None, dn=None):
    return {
        "product": product, "description": product,
        "cartons_sold": cartons, "price": price,
        "sales_total": round(cartons * price, 2),
        "last_sale": day, "group_date": received or day,
        "date_received": received or day, "invoice_date": received or day,
        "qty_received": sent, "consignment_id": cid, "stm_no": cid,
        "dn": dn if dn is not None else cid, "supplier_ref": dn if dn is not None else cid,
        "market": market, "market_agent": agent,
        "cartons_returned": 0, "returns_total": 0.0,
    }


def _pay(dn, product, day, gross):
    return {"accsale": f"PRE*BT*{dn}", "dn": dn, "date": day, "gross": gross,
            "nett": round(gross * 0.84, 2),
            "lines": [{"product": product, "sales_total": gross}]}


# --- what a projection is allowed to claim --------------------------------

def test_a_product_is_projected_at_the_median_of_the_months_it_traded():
    rows = [_sale(GRAPES, "2026-04-10", 100, 100.0, cid=1),
            _sale(GRAPES, "2026-05-10", 50, 100.0, cid=2),
            _sale(GRAPES, "2026-06-10", 80, 100.0, cid=3)]
    out = forecast.project(rows, today=date(2026, 6, 20))
    p = out["products"][0]
    assert out["month"] == "2026-07"
    assert p["estimate"] == 8000.0          # median of 10 000, 5 000, 8 000
    assert (p["low"], p["high"]) == (5000.0, 10000.0)
    assert p["months_used"] == ["2026-04", "2026-05", "2026-06"]
    assert p["confidence"] == "fair"


def test_one_month_of_a_product_is_a_reading_not_a_range():
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1)]
    p = forecast.project(rows, today=date(2026, 6, 20))["products"][0]
    assert p["confidence"] == "single month"
    assert p["low"] == p["high"] == p["estimate"] == 8000.0


def test_the_working_travels_with_the_estimate():
    """A figure whose months cannot be checked is a figure to be believed."""
    rows = [_sale(GRAPES, "2026-05-10", 50, 100.0, cid=1),
            _sale(GRAPES, "2026-06-10", 80, 100.0, cid=2)]
    p = forecast.project(rows, today=date(2026, 6, 20))["products"][0]
    assert p["basis"] == {"2026-05": 5000.0, "2026-06": 8000.0}
    assert p["confidence"] == "low"


def test_only_the_recent_window_counts():
    """A month from the far side of the season is a different crop."""
    rows = [_sale(GRAPES, "2026-01-10", 1000, 100.0, cid=1)] + [
        _sale(GRAPES, f"2026-0{m}-10", 50, 100.0, cid=m) for m in (4, 5, 6)]
    p = forecast.project(rows, today=date(2026, 6, 20))["products"][0]
    assert "2026-01" not in p["months_used"]
    assert p["estimate"] == 5000.0


def test_a_product_that_has_stopped_trading_is_resting_not_projected():
    """Out of season is not the same as underperforming, and averaging it back
    to life would have the operator buying fruit that has finished."""
    rows = [_sale(PLUMS, "2026-04-10", 100, 100.0, cid=1),
            _sale(GRAPES, "2026-05-10", 50, 100.0, cid=2),
            _sale(GRAPES, "2026-06-10", 50, 100.0, cid=3)]
    out = forecast.project(rows, today=date(2026, 6, 20))
    assert [p["product"] for p in out["products"]] == [GRAPES]
    assert [r["product"] for r in out["resting"]] == [PLUMS]
    assert out["resting"][0]["last_traded"] == "2026-04"
    # A resting product contributes nothing, rather than a share of its old self.
    assert out["total"]["estimate"] == 5000.0


# --- what the projection has to admit -------------------------------------

def test_a_short_book_says_it_cannot_see_the_season():
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1)]
    out = forecast.project(rows, today=date(2026, 6, 20))
    assert any("Season and trend cannot be told apart" in c for c in out["caveats"])


def test_a_stale_book_says_how_far_behind_it_is():
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1)]
    out = forecast.project(rows, today=date(2026, 9, 14))
    assert any("last recorded sale is in 2026-06" in c for c in out["caveats"])


def test_a_month_carried_by_a_couple_of_loads_says_so():
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1),
            _sale(GRAPES, "2026-06-11", 80, 100.0, cid=2)]
    out = forecast.project(rows, today=date(2026, 6, 20))
    assert any("fewer than five consignments" in c for c in out["caveats"])


def test_a_hole_in_the_history_is_reported_as_a_hole():
    """A month with nothing in it is either a month with no trade or a month
    nobody imported, and the difference decides whether the total is real."""
    rows = [_sale(GRAPES, "2026-04-10", 80, 100.0, cid=1),
            _sale(GRAPES, "2026-06-10", 80, 100.0, cid=2)]
    out = forecast.project(rows, today=date(2026, 6, 20))
    assert any("No sales are recorded for 2026-05" in c for c in out["caveats"])


def test_an_empty_book_projects_nothing_and_says_why():
    out = forecast.project([], today=date(2026, 6, 20))
    assert out["total"] == {"low": 0.0, "estimate": 0.0, "high": 0.0}
    assert out["products"] == []
    assert out["caveats"]


# --- how fast it sells -----------------------------------------------------

def test_velocity_is_measured_per_consignment_and_taken_as_a_median():
    """One load that sat for a month must not set the pace for the product."""
    rows = [
        _sale(GRAPES, "2026-06-02", 40, 100.0, cid=1, received="2026-06-01", sent=40),
        _sale(GRAPES, "2026-06-04", 40, 100.0, cid=2, received="2026-06-03", sent=40),
        _sale(GRAPES, "2026-07-01", 40, 100.0, cid=3, received="2026-06-01", sent=40),
    ]
    v = forecast.velocity(rows)[0]
    assert v["consignments"] == 3
    assert v["days_to_clear"] == 1.0        # median of 1, 1 and 30
    assert v["slowest_days"] == 30          # the slow one is still reported
    assert v["cartons_per_day"] == 40.0


def test_a_load_that_cleared_the_same_day_counts_as_a_day():
    """Dropping it would leave the fastest movers with no record at all."""
    rows = [_sale(GRAPES, "2026-06-01", 30, 100.0, cid=1, received="2026-06-01", sent=30)]
    v = forecast.velocity(rows)[0]
    assert v["days_to_clear"] == 0.0
    assert v["cartons_per_day"] == 30.0


def test_sell_through_never_reads_above_everything_sent():
    rows = [_sale(GRAPES, "2026-06-02", 30, 100.0, cid=1, received="2026-06-01", sent=100)]
    assert forecast.velocity(rows)[0]["sell_through"] == 0.3


# --- where to send it ------------------------------------------------------

def test_an_outlet_carries_the_volume_its_price_was_achieved_on():
    """One carton at a record price is not a better market than four hundred at
    a fair one, so the count has to travel with the price."""
    rows = [_sale(GRAPES, "2026-06-02", 1, 500.0, cid=1, market="SPRINGS MARKET", agent="Subtropico"),
            _sale(GRAPES, "2026-06-02", 400, 380.0, cid=2, market="TSHWANE MARKET", agent="Farmers Trust")]
    best, second = forecast.outlets(rows)
    assert (best["market"], best["price"], best["cartons"]) == ("SPRINGS MARKET", 500.0, 1)
    assert (second["market"], second["price"], second["cartons"]) == ("TSHWANE MARKET", 380.0, 400)


# --- when the money lands --------------------------------------------------

def test_payment_lag_is_measured_from_the_last_sale_to_the_money():
    rows = [_sale(GRAPES, "2026-06-10", 50, 100.0, cid=1, dn=14587),
            _sale(GRAPES, "2026-06-20", 50, 100.0, cid=1, dn=14587)]
    lag = forecast.payment_lag(rows, [_pay(14587, GRAPES, "2026-06-30", 10000.0)])
    assert lag == [{"market_agent": "Farmers Trust", "days_to_pay": 10.0,
                    "slowest_days": 10, "payments": 1}]


def test_a_payment_dated_before_the_sale_is_not_a_negative_lag():
    rows = [_sale(GRAPES, "2026-06-20", 50, 100.0, cid=1, dn=14587)]
    assert forecast.payment_lag(rows, [_pay(14587, GRAPES, "2026-06-01", 5000.0)]) == []


def test_the_whole_picture_comes_back_in_one_call():
    rows = [_sale(GRAPES, "2026-06-10", 50, 100.0, cid=1, dn=14587, sent=50)]
    out = forecast.build(rows, [_pay(14587, GRAPES, "2026-06-20", 5000.0)],
                         today=date(2026, 6, 25))
    assert set(out) == {"projection", "velocity", "outlets", "payment_lag"}
    assert out["projection"]["month"] == "2026-07"


# --- what the model is handed ---------------------------------------------

def test_the_prompt_carries_the_caveats_above_the_numbers():
    """A model summarising will drop a caveat before it drops a figure, so the
    caveats lead and are labelled as non-optional."""
    from app import assistant
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1)]
    text = assistant.forecast_context(rows, [])
    lead = text.index("READ THESE FIRST")
    assert lead < text.index("Whole book:")
    assert "must appear" in text


def test_the_prompt_gives_the_range_not_just_the_middle():
    from app import assistant
    rows = [_sale(GRAPES, "2026-05-10", 50, 100.0, cid=1),
            _sale(GRAPES, "2026-06-10", 80, 100.0, cid=2)]
    text = assistant.forecast_context(rows, [])
    assert "expected (R) | low (R) | high (R)" in text
    assert "do not recalculate" in text


def test_the_prompt_says_an_outlet_price_needs_its_volume():
    from app import assistant
    rows = [_sale(GRAPES, "2026-06-10", 400, 380.0, cid=1)]
    text = assistant.forecast_context(rows, [])
    assert "not a better outlet than" in text


def test_a_resting_product_is_labelled_as_out_of_season_not_as_a_failure():
    from app import assistant
    rows = [_sale(PLUMS, "2026-04-10", 100, 100.0, cid=1),
            _sale(GRAPES, "2026-05-10", 50, 100.0, cid=2),
            _sale(GRAPES, "2026-06-10", 50, 100.0, cid=3)]
    text = assistant.forecast_context(rows, [])
    assert "out of season rather than failing" in text
    assert PLUMS in text.split("### Not projected")[1]


def test_an_empty_book_offers_the_model_nothing_to_project():
    from app import assistant
    assert "not enough recorded history" in assistant.forecast_context([], [])


def test_the_data_block_carries_both_what_happened_and_what_is_expected():
    from app import assistant
    rows = [_sale(GRAPES, "2026-06-10", 80, 100.0, cid=1)]
    block = assistant.data_block(rows, [])
    assert "## Totals across all recorded sales (exact)" in block
    assert "## Projection for" in block


def test_the_panel_has_a_specialist_for_the_month_ahead_and_for_the_cash():
    from app import assistant
    keys = [a["key"] for a in assistant.ANALYSTS]
    assert "ahead" in keys and "cash" in keys
    ahead = next(a for a in assistant.ANALYSTS if a["key"] == "ahead")
    assert "never recompute it" in ahead["brief"]
