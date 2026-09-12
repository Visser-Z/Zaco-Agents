"""A payment settles the sales it was actually for, oldest first.

A payment belongs to a consignment, not to a month. Comparing one month's
sales against a consignment's whole payment history made a July payment cancel
a September sale of the same delivery: September reported as over-paid while
that delivery was, across the book, tens of thousands short. Settled oldest
first, each month answers for its own sales and the months add up to the whole.
"""

from app import tracking


def _sale(dn, product, value, day):
    """One sale, valued the way the history carries it."""
    return {"dn": dn, "supplier_ref": dn, "product": product,
            "cartons_sold": 1, "price": value, "sales_total": value,
            "last_sale": day, "group_date": day, "market_agent": "Farmers Trust"}


def _payment(dn, product, gross, day, nett=None):
    return {"accsale": f"PRE*BT*{dn}", "dn": dn, "date": day,
            "gross": gross, "nett": nett if nett is not None else gross * 0.85,
            "lines": [{"product": product, "sales_total": gross}]}


GRAPES = "GRAPES SUGRAONE CLASS 2 NO SIZE (PUNNET 5kg)"


def test_a_payment_settles_the_oldest_sales_first():
    sales = [_sale(14587, GRAPES, 1000.0, "2026-07-10"),
             _sale(14587, GRAPES, 400.0, "2026-08-14"),
             _sale(14587, GRAPES, 440.0, "2026-09-07")]
    payments = [_payment(14587, GRAPES, 1200.0, "2026-08-07")]

    july = tracking.payment_status(sales, payments, lo="2026-07-01", hi="2026-07-31")
    august = tracking.payment_status(sales, payments, lo="2026-08-01", hi="2026-08-31")
    september = tracking.payment_status(sales, payments, lo="2026-09-01", hi="2026-09-30")

    assert july["still_to_come"] == 0.0        # 1000 of the 1200 lands here
    assert august["still_to_come"] == 200.0    # the remaining 200 covers 200 of 400
    assert september["still_to_come"] == 440.0 # nothing reaches September


def test_the_months_add_up_to_the_whole_book():
    sales = [_sale(14587, GRAPES, 1000.0, "2026-07-10"),
             _sale(14587, GRAPES, 400.0, "2026-08-14"),
             _sale(14587, GRAPES, 440.0, "2026-09-07")]
    payments = [_payment(14587, GRAPES, 1200.0, "2026-08-07")]

    months = sum(
        tracking.payment_status(sales, payments, lo=lo, hi=hi)["still_to_come"]
        for lo, hi in (("2026-07-01", "2026-07-31"),
                       ("2026-08-01", "2026-08-31"),
                       ("2026-09-01", "2026-09-30"))
    )
    all_time = tracking.payment_status(sales, payments)["still_to_come"]
    assert all_time == 1840.0 - 1200.0
    assert round(months, 2) == all_time


def test_the_real_september_case():
    """DN 14587, from the live book: R17 600 received on 7 August against a
    delivery that had already sold R49 371,79 in July. September's own 11
    cartons must still read as owed, not as over-paid."""
    sales = [_sale(14587, GRAPES, 49371.79, "2026-07-20"),
             _sale(14587, GRAPES, 4000.00, "2026-08-12"),
             _sale(14587, GRAPES, 4400.00, "2026-09-07")]
    payments = [_payment(14587, GRAPES, 17600.0, "2026-08-07", nett=14953.93)]

    september = tracking.payment_status(sales, payments, lo="2026-09-01", hi="2026-09-30")
    assert september["still_to_come"] == 4400.00
    assert september["batches_outstanding"] == 1
    assert september["overpaid"] == []
    # The money arrived in August, so September reports none received.
    assert september["total_paid"] == 0.0


def test_paying_more_than_was_ever_sold_is_an_over_payment_not_a_credit():
    sales = [_sale(14587, GRAPES, 400.0, "2026-09-07")]
    payments = [_payment(14587, GRAPES, 1000.0, "2026-09-08")]

    out = tracking.payment_status(sales, payments)
    assert out["still_to_come"] == 0.0
    assert [r["overpaid"] for r in out["overpaid"]] == [600.0]
    assert out["batches_paid"] == 1


def test_an_unpaid_delivery_owes_all_of_it():
    sales = [_sale(14621, "GRAPES CLASS 2 NO SIZE (PUNNET 5kg)", 9850.0, "2026-09-07")]
    out = tracking.payment_status(sales, [])
    assert out["still_to_come"] == 9850.0
    assert out["outstanding"][0]["status"] == "unpaid"
    assert out["oldest_outstanding"] == "2026-09-07"


def test_a_payment_with_nothing_sold_against_it_is_surfaced():
    payments = [_payment(99999, GRAPES, 500.0, "2026-09-08")]
    out = tracking.payment_status([], payments)
    assert out["still_to_come"] == 0.0
    assert len(out["unmatched"]) == 1
    assert out["unmatched"][0]["paid"] == 500.0


def test_closing_a_line_takes_it_off_the_total_but_keeps_its_value():
    sales = [_sale(14621, "GRAPES CLASS 2 NO SIZE (PUNNET 5kg)", 9850.0, "2026-09-07")]
    open_view = tracking.payment_status(sales, [])
    ref = open_view["outstanding"][0]["ref"]

    shut = tracking.payment_status(sales, [], closed={tracking.closed_key("owed", ref)})
    assert shut["still_to_come"] == 0.0
    assert shut["closed_value"] == 9850.0
    assert [r["ref"] for r in shut["closed"]] == [ref]


def test_a_sale_with_no_date_settles_last():
    """An undated sale cannot be placed in the order, so it must not take credit
    from a sale known to be older."""
    dated = _sale(14587, GRAPES, 600.0, "2026-07-10")
    undated = _sale(14587, GRAPES, 600.0, None)
    payments = [_payment(14587, GRAPES, 600.0, "2026-07-20")]

    owed, _, _ = tracking.settle([dated, undated], payments)
    assert owed[id(dated)] == 0.0
    assert owed[id(undated)] == 600.0
