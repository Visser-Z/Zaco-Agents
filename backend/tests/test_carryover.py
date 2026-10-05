"""Last month's unsold stock carried into this month's Stock on Hand."""

from datetime import date

from app import tracking


def sale(cid, sent, sold, day, product="GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"):
    return {"consignment_id": cid, "dn": cid // 100, "product": product,
            "market": "TSHWANE MARKET", "market_agent": "Farmers Trust", "cartons_sold": sold,
            "price": 100.0, "sales_total": sold * 100.0, "last_sale": day, "date_received": day,
            "group_date": day, "qty_received": sent, "qty_avail": sent - sold}


SEPT = sale(118000101, 100, 60, "2026-09-20")
OCT = sale(118000201, 50, 10, "2026-10-02", product="PLUMS ELDORADO CLASS 1 NO SIZE (DOUBLE LAYER TRAY 5kg)")
TODAY = date(2026, 10, 5)


def lines(out):
    return {r["consignment_id"]: r for m in out["markets"] for r in m["lines"]}


def test_a_new_month_offers_last_months_stock_once():
    status = tracking.carryover_status([SEPT, OCT], TODAY)
    assert (status["month"], status["from_month"]) == ("2026-10", "2026-09")
    assert status["pending"] and status["lines"] == 1 and status["cartons"] == 40
    carried = [{"month": "2026-10", "ref": status["_left"][0]["ref"], "cartons": 40}]
    again = tracking.carryover_status([SEPT, OCT], TODAY, carried=carried)
    assert not again["pending"] and again["carried_cartons"] == 40


def test_october_shows_septembers_stock_once_it_is_carried():
    before = lines(tracking.stock_on_hand([SEPT, OCT], TODAY, lo="2026-10-01", hi="2026-10-31"))
    assert list(before) == [118000201]
    ref = lines(tracking.stock_on_hand([SEPT], TODAY))[118000101]["ref"]
    carried = [{"month": "2026-10", "ref": ref, "cartons": 40}]
    after = lines(tracking.stock_on_hand([SEPT, OCT], TODAY, lo="2026-10-01", hi="2026-10-31",
                                         carried=carried))
    assert set(after) == {118000101, 118000201}
    assert after[118000101]["carried_from"] == "2026-09"
    # September still shows it where it arrived, and says where it went.
    sept = lines(tracking.stock_on_hand([SEPT, OCT], TODAY, lo="2026-09-01", hi="2026-09-30",
                                        carried=carried))
    assert sept[118000101]["carried_to"] == "2026-10"


def test_carrying_stock_moves_no_sale_and_no_payment():
    """What sold in September stays September's; the carry-over only changes
    where the unsold stock is shown."""
    ref = lines(tracking.stock_on_hand([SEPT], TODAY))[118000101]["ref"]
    carried = [{"month": "2026-10", "ref": ref, "cartons": 40}]
    plain = tracking.compute([SEPT, OCT], [], today=TODAY, month="2026-09")
    moved = tracking.compute([SEPT, OCT], [], today=TODAY, month="2026-09", carried=carried)
    assert plain["payments"] == moved["payments"]
    assert plain["sales_by_day"] == moved["sales_by_day"]
