"""Prices against the market average, flagged only in a consignment's first days."""

import asyncio

import pytest

import app.main as main
from app import price_check


def _row(cons, day, cartons, value, avg, agent="Farmers Trust", **kw):
    return {"consignment_id": cons, "dn": 14391, "product": "GRAPES CRIMSON", "market": "TSHWANE MARKET",
            "market_agent": agent, "last_sale": day, "group_date": day, "cartons_sold": cartons,
            "sales_total": value, "market_avg": avg, "source_file": "technofresh_sales.pdf", **kw}


def test_under_the_average_in_the_first_days_is_flagged_with_the_money():
    # 44 cartons at R 100 on day 1 against a market average of R 249,89 (Tshwane, 13 Oct 2026).
    rows = [_row(1, "2026-10-13", 44, 4400.0, 249.89)]
    out = price_check.check(rows, rows, 5)
    line = out["lines"][0]
    assert (line["day"], line["cartons"], line["realised"], line["missed"]) == (1, 44, 100.0, 6595.16)
    a = out["agents"][0]
    assert a["flagged"] == 1 and a["missed"] == 6595.16
    # The agent's typical 15% comes off first: that nett is what Zaco and the
    # grower would have shared, and Zaco keeps 40% of it.
    assert a["missed_nett"] == round(6595.16 * 0.85, 2)
    assert a["zaco_missed"] == round(6595.16 * 0.85 * 0.40, 2)


def test_after_the_window_it_is_clearance_and_not_flagged():
    rows = [_row(1, "2026-10-01", 10, 2000.0, 200.0),      # day 1, at the average
            _row(1, "2026-10-08", 10, 1000.0, 200.0)]      # day 8, half the average
    out = price_check.check(rows, rows, 5)
    assert out["flagged"] == 0
    assert out["totals"]["clearance_sold"] == 1000.0 and out["totals"]["clearance_under"] == 1000.0
    # Widen the window and the same sale is flagged.
    assert price_check.check(rows, rows, 8)["flagged"] == 1


def test_at_or_above_the_average_is_never_flagged():
    rows = [_row(1, "2026-10-01", 10, 2000.0, 200.0), _row(2, "2026-10-01", 10, 2500.0, 200.0)]
    out = price_check.check(rows, rows, 5)
    assert out["flagged"] == 0 and out["agents"][0]["share_of_market"] == 1.125


def test_day_one_comes_from_the_whole_book_not_the_period_on_screen():
    september = _row(1, "2026-09-29", 10, 2000.0, 200.0)
    october = _row(1, "2026-10-05", 10, 1000.0, 200.0)     # day 7 of this consignment
    out = price_check.check([september, october], [october], 5)
    assert out["flagged"] == 0 and out["totals"]["clearance_sold"] == 1000.0


def test_a_csv_delivery_date_beats_the_first_sale():
    rows = [_row(1, "2026-10-06", 10, 1000.0, 200.0, date_received="2026-10-01",
                 payment_refs="PRE*BT*1=1000", source_file="sales.csv")]
    assert price_check.check(rows, rows, 5)["flagged"] == 0   # day 6, past five days
    assert price_check.check(rows, rows, 6)["lines"][0]["day"] == 6


def test_lines_without_a_market_average_are_counted_not_guessed():
    rows = [_row(1, "2026-10-01", 10, 1000.0, None), _row(2, "2026-10-01", 10, 1000.0, 0.0)]
    out = price_check.check(rows, rows, 5)
    assert not out["known"] and out["unpriced"] == 2 and out["flagged"] == 0


def test_an_agents_own_cut_is_used_when_its_paid_sales_show_it():
    paid = [_row(10 + i, "2026-09-01", 10, 1000.0, 100.0, nett_total=900.0) for i in range(8)]
    flagged = _row(1, "2026-10-01", 10, 1000.0, 200.0)
    out = price_check.check(paid + [flagged], [flagged], 5)
    a = out["agents"][0]
    assert a["deduction_basis"] == "own" and a["deduction"] == pytest.approx(0.10)
    assert a["missed_nett"] == 900.0 and a["zaco_missed"] == 360.0


def _daily(*days):
    """A Sales per day payload as tracking.daily shapes it, cut down."""
    return {"sales_by_day": {
        "days": [{"date": d, "products": [{"product": "GRAPES CRIMSON"}, {"product": "PLUMS"}]}
                 for d in days],
        "months": [{"month": "2026-10"}], "totals": {}}}


def test_sales_per_day_carries_the_flag_on_the_product_day_and_month():
    rows = [_row(1, "2026-10-13", 44, 4400.0, 249.89),                      # day 1, flagged
            _row(2, "2026-10-01", 10, 2000.0, 200.0), _row(2, "2026-10-13", 10, 1000.0, 200.0)]  # day 13, clearance
    out = price_check.lay_on_days(_daily("2026-10-13", "2026-10-14"), price_check.by_day(rows, 5))
    oct13, oct14 = out["sales_by_day"]["days"]
    grapes = oct13["products"][0]["market_check"]
    assert grapes["status"] == "flagged" and grapes["short"] == 6595.16 and grapes["day"] == 1
    assert grapes["nett"] == round(6595.16 * 0.85, 2) and grapes["agents"] == ["Farmers Trust"]
    assert oct13["under_market"]["flagged"] == 1 and oct13["under_market"]["nett"] == grapes["nett"]
    assert oct13["products"][1]["market_check"] is None
    assert oct14["under_market"] is None
    month = out["sales_by_day"]["months"][0]["under_market"]
    assert month["short"] == 6595.16 and out["sales_by_day"]["totals"]["under_market"] == month
    assert out["price_check"]["fresh_days"] == 5


def test_every_priced_line_says_how_far_over_or_under_the_market_it_sold():
    rows = [_row(1, "2026-10-13", 10, 2200.0, 200.0),                       # 10% over
            _row(2, "2026-10-13", 10, 1800.0, 200.0, product="PLUMS")]      # 10% under, flagged
    checked = price_check.by_day(rows, 5)["days"]["2026-10-13"]["products"]
    over, under = checked["GRAPES CRIMSON"], checked["PLUMS"]
    assert (over["status"], over["pct"], over["diff"], over["market_avg"]) == ("ok", 0.1, 200.0, 200.0)
    assert over["diff_nett"] == 170.0 and over["short"] == 0
    assert (under["status"], under["pct"], under["diff"]) == ("flagged", -0.1, -200.0)
    assert under["nett"] == 170.0 and under["zaco"] == 68.0 and under["flag_day"] == 1


def test_a_clearance_sale_is_marked_but_carries_no_money():
    rows = [_row(2, "2026-10-01", 10, 2000.0, 200.0), _row(2, "2026-10-13", 10, 1000.0, 200.0)]
    checked = price_check.by_day(rows, 5)
    line = checked["days"]["2026-10-13"]["products"]["GRAPES CRIMSON"]
    assert line["status"] == "clearance" and line["day"] == 13 and line["short"] == 0
    assert checked["days"]["2026-10-13"]["flagged"] == 0


def test_the_setting_is_held_in_its_range():
    assert price_check.fresh_days_from(None) == 5
    assert price_check.fresh_days_from("0") == 1
    assert price_check.fresh_days_from(99) == 30


def test_settings_default_before_the_migration(monkeypatch):
    async def missing(user, path, params=None):
        raise RuntimeError('relation "public.app_settings" does not exist')
    monkeypatch.setattr(main, "db_get", missing)
    out = asyncio.run(main._app_settings(object()))
    assert out["price_check_days"] == 5 and out["migration"] == "0030_app_settings.sql"


def test_settings_refuse_days_out_of_range():
    with pytest.raises(main.HTTPException):
        asyncio.run(main.save_app_settings(price_check_days=0, profile={"id": "u", "token": "t"}))
