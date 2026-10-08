"""Payments per delivery note: what the market paid, took and left Zaco."""
from app import tracking


def sale(cid, dn, product, total, day, market="TSHWANE MARKET"):
    return {"consignment_id": cid, "dn": dn, "product": product, "sales_total": total,
            "cartons_sold": 10, "price": total / 10, "group_date": day, "last_sale": day,
            "market": market, "market_agent": "Farmers Trust"}


def pay(acc, fms, day, gross, deductions, vat, lines, dn=None):
    return {"accsale": acc, "fms_id": fms, "date": day, "dn": dn,
            "gross": gross, "deductions": deductions, "vat": vat,
            "nett": round(gross - deductions - vat, 2), "lines": lines,
            "market_agent": "Farmers Trust"}


GRAPE, PLUM = "GRAPES RALLI CLASS 1", "PLUMS FORTUNE CLASS 1"
SALES = [
    sale(20300101, 14500, GRAPE, 1000.0, "2026-09-01"),
    sale(20300102, 14500, PLUM, 500.0, "2026-09-01"),
    # A second delivery under the same note.
    sale(20800101, 14500, PLUM, 300.0, "2026-09-02"),
]
LINES_A = [{"line_no": 1, "product": GRAPE, "sales_total": 1000.0},
           {"line_no": 2, "product": PLUM, "sales_total": 500.0}]
LINES_B = [{"line_no": 1, "product": PLUM, "sales_total": 300.0}]
PAYMENTS = [
    pay("PRE*BT*1", "203001", "2026-09-05", 1500.0, 200.0, 30.0, LINES_A, dn=1),
    pay("PRE*BT*2", "208001", "2026-10-02", 300.0, 40.0, 6.0, LINES_B, dn=1),
]


def test_groups_by_delivery_note_with_market_columns():
    out = tracking.payments_by_delivery(tracking.valued(SALES), PAYMENTS)
    assert out["deliveries"] == 1 and out["unplaced"] == 0
    m = out["markets"][0]
    assert m["market"] == "TSHWANE MARKET"
    note = m["notes"][0]
    # The note is read off the sales book, not the payment's ref of "1".
    assert note["dn"] == "14500"
    assert note["fms_ids"] == ["203001", "208001"]
    assert note["gross"] == 1800.0
    assert note["deductions"] == 240.0 and note["vat"] == 36.0
    assert note["took"] == 276.0
    assert note["nett"] == 1524.0
    assert note["sold"] == 1800.0
    assert [p["date"] for p in note["payments"]] == ["2026-09-05", "2026-10-02"]


def test_window_picks_payments_by_day_paid():
    out = tracking.payments_by_delivery(tracking.valued(SALES), PAYMENTS,
                                        lo="2026-10-01", hi="2026-10-31")
    note = out["markets"][0]["notes"][0]
    assert note["count"] == 1 and note["gross"] == 300.0
    # What the note sold is its whole position, whatever the window.
    assert note["sold"] == 1800.0


def test_payment_with_no_delivery_on_the_book_is_kept_apart():
    stray = pay("DUR*13*9", "999999", "2026-09-09", 100.0, 10.0, 1.5, [])
    out = tracking.payments_by_delivery(tracking.valued(SALES), PAYMENTS + [stray])
    assert out["unplaced"] == 1
    durban = next(m for m in out["markets"] if m["market"] == "DURBAN MARKET")
    assert durban["notes"][0]["dn"] is None
    assert durban["notes"][0]["took"] == 11.5
    assert out["gross"] == 1900.0


def test_compute_carries_it():
    out = tracking.compute(SALES, PAYMENTS)
    assert out["payments_by_delivery"]["payments"] == 2
