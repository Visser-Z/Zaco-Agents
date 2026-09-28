"""Why a line is still owed, from the cartons the payments say they cover.

Measured on the live book before this was built: on the 147 lines paid to the
cent, the payments' carton counts agreed with the book's on every one. So where
they disagree on a line that still owes money, the disagreement says why.
"""

from app import tracking

DN, PRODUCT = 14890, "STRAWBERRIES NO VARIETY NOT GRADED"


def sale(cid, cartons, value, day="2026-09-03", dn=DN, product=PRODUCT):
    return {"consignment_id": cid, "dn": dn, "product": product,
            "market": "TSHWANE MARKET", "market_agent": "Farmers Trust",
            "cartons_sold": cartons, "price": value / cartons if cartons else 0,
            "sales_total": value, "last_sale": day, "date_received": "2026-09-01",
            "group_date": "2026-09-01", "qty_received": 400}


def paid(gross, cartons, day="2026-09-15", dn=DN, product=PRODUCT, fms=None, line_no=None):
    line = {"product": product, "sales_total": gross}
    if cartons is not None:
        line["sold"] = cartons
    if line_no is not None:
        line["line_no"] = line_no
    return {"accsale": f"PRE*BT*{int(gross)}", "dn": dn, "date": day, "gross": gross,
            "nett": round(gross * 0.85, 2), "fms_id": fms, "lines": [line]}


def reason_of(sales, payments):
    lines = tracking.payment_status(sales, payments)["outstanding"]
    assert len(lines) == 1, lines
    return lines[0]


def test_nothing_paid_yet_is_awaiting():
    line = reason_of([sale(1, 100, 10000.0)], [])
    assert line["reason"] == "awaiting"
    assert line["cartons_sold"] == 100 and line["cartons_paid"] is None


def test_fewer_cartons_paid_for_than_sold_is_real_debt():
    line = reason_of([sale(1, 100, 10000.0)], [paid(6000.0, 60)])
    assert line["reason"] == "short"
    assert (line["cartons_sold"], line["cartons_paid"]) == (100, 60)


def test_every_carton_paid_for_less_money_is_a_price_query():
    """All 100 cartons are on the payments, but at R 90 not R 100: the two
    reports disagree on the price. Worth a question, not a phone call."""
    line = reason_of([sale(1, 100, 10000.0)], [paid(9000.0, 100)])
    assert line["reason"] == "price"


def test_more_cartons_paid_for_than_the_book_has_is_a_missing_report():
    """The payments count 120 cartons but the book only has 100 sold, and the
    money for them has not all landed on sales. A sales day is probably not on
    the book, so what shows as owed is a gap in the history, not a debt."""
    line = reason_of([sale(1, 100, 10000.0)], [paid(8000.0, 120)])
    assert line["reason"] == "sales_missing"


def test_a_payment_that_printed_no_cartons_says_so():
    line = reason_of([sale(1, 100, 10000.0)], [paid(6000.0, None)])
    assert line["reason"] == "no_count"
    assert line["cartons_paid"] is None


def test_cartons_are_counted_across_every_consignment_on_the_note():
    """Two consignments of the same product on one delivery note, and the
    payments name only the note. Counting one consignment's cartons against
    payments for both called a line 'more paid than sold' that was nothing of
    the kind: this mistake was made once, in a measurement, and is pinned here."""
    sales = [sale(1, 60, 6000.0), sale(2, 40, 4000.0, day="2026-09-05")]
    # 100 cartons sold between them; payments cover 80 of them.
    status = tracking.payment_status(sales, [paid(8000.0, 80)])
    reasons = {r["consignment_id"]: r["reason"] for r in status["outstanding"]}
    assert set(reasons.values()) == {"short"}
    assert all(r["cartons_sold"] == 100 and r["cartons_paid"] == 80
               for r in status["outstanding"])


def test_a_payment_bound_by_fms_id_counts_against_its_own_consignment():
    """The Durban 20026*N payments carry no delivery note. Filed under a blank
    one, their cartons went nowhere and a consignment with R 71 100,00 paid on
    it read 'not paid yet'."""
    sales = [{**sale(185549101, 100, 10000.0, dn=1855491), "market": "DURBAN MARKET",
              "qty_amended": 100}]
    payment = paid(6000.0, 60, dn=None, fms="743396", line_no=1)
    payment["accsale"] = "DUR*13*202568"
    payment["lines"][0]["delivered"] = 100
    status = tracking.payment_status(sales, [payment])
    assert status["fms_bound"] == 1
    line = status["outstanding"][0]
    assert line["cartons_paid"] == 60
    assert line["reason"] == "short"


def test_a_return_that_settles_the_line_takes_it_off_the_list():
    """Also learned from the measurement: a consignment looked part-paid only
    because its return was left out. Counted in, there is nothing owed."""
    sales = [sale(1, 100, 10000.0), sale(1, -20, -2000.0, day="2026-09-06")]
    status = tracking.payment_status(sales, [paid(8000.0, 80)])
    assert status["outstanding"] == []


def test_debt_and_what_to_check_add_up_to_what_is_owed():
    sales = [sale(1, 100, 10000.0),
             sale(2, 50, 5000.0, dn=15000, product="GRAPES RALLI"),
             sale(3, 10, 1000.0, dn=15001, product="PLUMS ELDORADO")]
    payments = [paid(6000.0, 60),
                paid(4500.0, 50, dn=15000, product="GRAPES RALLI")]
    st = tracking.payment_status(sales, payments)
    assert st["owed_by_reason"]["short"] == 4000.0
    assert st["owed_by_reason"]["price"] == 500.0
    assert st["owed_by_reason"]["awaiting"] == 1000.0
    assert st["debt"] == 5000.0 and st["to_check"] == 500.0
    assert round(st["debt"] + st["to_check"] + st["credit_value"], 2) == st["still_to_come"]


def test_the_reason_is_the_consignment_s_in_any_month():
    """A month shows part of a consignment's money, but its payments count
    cartons across all of it, so August and September agree on why."""
    sales = [sale(1, 60, 6000.0, day="2026-08-30"), sale(1, 40, 4000.0, day="2026-09-02")]
    payments = [paid(5000.0, 50)]
    for lo, hi in (("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")):
        lines = tracking.payment_status(sales, payments, frozenset(), lo, hi)["outstanding"]
        assert [l["reason"] for l in lines] == ["short"]
