"""A payment settles the sales it was actually for.

A payment belongs to a sale, not to the month it arrived in. Comparing one
month's sales against a consignment's whole payment history made a July payment
cancel a September sale of the same delivery. Pooling the payments and paying
the sales off oldest first then moved an old shortfall onto the newest month.
Matched to the sale it paid for, each month answers for its own sales and the
months add up to the whole.
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


NECTARINES = "NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg)"


def test_a_return_booked_after_the_sale_it_reverses_stays_on_the_book():
    """From the live book, DN 14584: R5 080,01 sold in July, then a two-carton
    return of R700 on 1 August against R300 sold on 3 August. August's own rows
    come to minus R400. Read as settled and dropped, that credit vanished from
    every month while the all-time figure still carried it, so the months came
    to R400 more than the book."""
    sales = [_sale(14584, NECTARINES, 5080.01, "2026-07-28"),
             _sale(14584, NECTARINES, -700.00, "2026-08-01"),
             _sale(14584, NECTARINES, 300.00, "2026-08-03")]

    august = tracking.payment_status(sales, [], lo="2026-08-01", hi="2026-08-31")
    assert august["still_to_come"] == -400.0
    assert august["credit_value"] == -400.0
    assert [r["status"] for r in august["credits"]] == ["credit"]
    # Nothing to chase, so it stays off the list the operator works down.
    assert august["outstanding"] == []
    assert august["batches_outstanding"] == 0


def test_a_return_in_a_later_month_keeps_the_months_adding_up():
    sales = [_sale(14584, NECTARINES, 5080.01, "2026-07-28"),
             _sale(14584, NECTARINES, -700.00, "2026-08-01"),
             _sale(14584, NECTARINES, 300.00, "2026-08-03")]

    months = sum(
        tracking.payment_status(sales, [], lo=lo, hi=hi)["still_to_come"]
        for lo, hi in (("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"))
    )
    assert round(months, 2) == tracking.payment_status(sales, [])["still_to_come"] == 4680.01


def test_a_closed_line_carries_its_credit_month_by_month_too():
    """Closing the line must not reintroduce the gap it was hiding."""
    sales = [_sale(14584, NECTARINES, 5080.01, "2026-07-28"),
             _sale(14584, NECTARINES, -700.00, "2026-08-01"),
             _sale(14584, NECTARINES, 300.00, "2026-08-03")]
    ref = tracking.payment_status(sales, [])["outstanding"][0]["ref"]
    shut = {tracking.closed_key("owed", ref)}

    months = sum(
        tracking.payment_status(sales, [], closed=shut, lo=lo, hi=hi)["closed_value"]
        for lo, hi in (("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"))
    )
    assert round(months, 2) == tracking.payment_status(sales, [], closed=shut)["closed_value"]


def test_a_sale_with_no_date_settles_last():
    """An undated sale cannot be placed in the order, so it must not take credit
    from a sale known to be older."""
    dated = _sale(14587, GRAPES, 600.0, "2026-07-10")
    undated = _sale(14587, GRAPES, 600.0, None)
    payments = [_payment(14587, GRAPES, 600.0, "2026-07-20")]

    owed, _, _ = tracking.settle([dated, undated], payments)
    assert owed[id(dated)] == 0.0
    assert owed[id(undated)] == 600.0


STRAWBERRIES = "STRAWBERRIES NO VARIETY NOT GRADED NO SIZE (DOUBLE LAYER TRAY 7kg)"


def _run(dn, product, value, first, last):
    """A sales row as the Daily Sales PDF records it: a run of selling days."""
    return {**_sale(dn, product, value, last), "date_received": first}


def _dn_14013():
    """DN 14013 strawberries, from the live book."""
    sales = [_run(14013, STRAWBERRIES, 97770.0, "2026-04-10", "2026-04-11"),
             _run(14013, STRAWBERRIES, 24255.0, "2026-04-13", "2026-04-17"),
             _run(14013, STRAWBERRIES, 8850.0, "2026-04-15", "2026-04-18"),
             _run(14013, STRAWBERRIES, 500.0, "2026-04-21", "2026-04-21"),
             _run(14013, STRAWBERRIES, 19630.0, "2026-06-03", "2026-06-05")]
    payments = [_payment(14013, STRAWBERRIES, 97770.0, "2026-04-13"),
                _payment(14013, STRAWBERRIES, 24650.0, "2026-04-15"),
                _payment(14013, STRAWBERRIES, 8150.0, "2026-04-17"),
                _payment(14013, STRAWBERRIES, 14730.0, "2026-06-05"),
                _payment(14013, STRAWBERRIES, 4900.0, "2026-06-08")]
    return sales, payments


def test_a_shortfall_stays_in_the_month_it_belongs_to():
    """April's runs were R805 short. June's R19 630 was paid to the cent by the
    5 and 8 June payments, so June owes nothing and April owes the R805.
    Pooled and paid off oldest first, June carried April's shortfall."""
    sales, payments = _dn_14013()
    april = tracking.payment_status(sales, payments, lo="2026-04-01", hi="2026-04-30")
    june = tracking.payment_status(sales, payments, lo="2026-06-01", hi="2026-06-30")
    assert june["still_to_come"] == 0.0
    assert june["batches_outstanding"] == 0
    assert april["still_to_come"] == 805.0
    assert april["still_to_come"] + june["still_to_come"] == \
        tracking.payment_status(sales, payments)["still_to_come"]


def test_a_payment_split_across_account_sales_is_still_an_exact_match():
    sales, payments = _dn_14013()
    owed, _, _ = tracking.settle(sales, payments)
    assert owed[id(sales[4])] == 0.0          # 14 730 + 4 900 = 19 630


def test_a_genuine_shortfall_is_not_hidden():
    """DN 14238, from the live book: the 30 April run was paid to the cent the
    same day, and the only later payment, R2 450, does not cover May."""
    product = "GRAPES CLASS 1 NO SIZE (PUNNET 5kg)"
    sales = [_run(14238, product, 3340.0, "2026-04-29", "2026-04-30"),
             _run(14238, product, 3880.0, "2026-04-29", "2026-05-02"),
             _run(14238, product, 1910.0, "2026-05-04", "2026-05-05")]
    payments = [_payment(14238, product, 300.0, "2026-04-30"),
                _payment(14238, product, 3040.0, "2026-04-30"),
                _payment(14238, product, 2450.0, "2026-05-06")]
    april = tracking.payment_status(sales, payments, lo="2026-04-01", hi="2026-04-30")
    may = tracking.payment_status(sales, payments, lo="2026-05-01", hi="2026-05-31")
    assert april["still_to_come"] == 0.0
    assert may["still_to_come"] == 3340.0


def test_a_sale_on_the_31st_paid_on_the_5th_counts_as_paid_in_its_own_month():
    sales = [_run(14587, GRAPES, 1000.0, "2026-07-31", "2026-07-31")]
    payments = [_payment(14587, GRAPES, 1000.0, "2026-08-05", nett=850.0)]
    july = tracking.payment_status(sales, payments, lo="2026-07-01", hi="2026-07-31")
    august = tracking.payment_status(sales, payments, lo="2026-08-01", hi="2026-08-31")
    assert july["still_to_come"] == 0.0
    assert july["total_paid"] == 850.0
    assert july["received_in_window"] == 0.0
    # August received the money but sold nothing, so it paid for nothing.
    assert august["total_paid"] == 0.0
    assert august["received_in_window"] == 850.0


def test_a_payment_cannot_pay_for_fruit_that_had_not_sold_yet():
    """The 3 July payment is for the run that had started by then, even though
    a later run is older by its last sale date's position in the list."""
    sales = [_run(14954, NECTARINES, 400.0, "2026-06-27", "2026-06-27"),
             _run(14954, NECTARINES, 600.0, "2026-07-06", "2026-07-07")]
    payments = [_payment(14954, NECTARINES, 500.0, "2026-07-03")]
    owed, _, _ = tracking.settle(sales, payments)
    assert owed[id(sales[0])] == 0.0
    # 100 left over, and nothing else had started, so it goes to the next sale
    # rather than becoming credit.
    assert owed[id(sales[1])] == 500.0


def test_a_credit_line_inside_a_payment_comes_off_what_was_paid():
    """PRE*BT*381695, from the live book: a R5 904,88 account sale carrying one
    commodity line of minus R105,12. A credit inside a payment reduces what
    that payment paid for the commodity."""
    product = "GRAPES CLASS 1 NO SIZE (PUNNET 5kg)"
    sales = [_run(20026, product, 1000.0, "2026-05-20", "2026-05-21")]
    payments = [_payment(20026, product, 1000.0, "2026-05-22"),
                {"accsale": "PRE*BT*381695", "dn": 20026, "date": "2026-05-25",
                 "gross": 894.88, "nett": 760.65,
                 "lines": [{"product": "PLUMS", "sales_total": 1000.0},
                           {"product": product, "sales_total": -105.12}]}]
    out = tracking.payment_status(sales, payments)
    assert out["still_to_come"] == 105.12
    assert out["reversals"] == []


def test_a_whole_account_sale_below_nil_is_a_reversal_not_a_credit():
    """PRE*BT*400352, gross minus R7 090 and nett nil, is the agent clawing
    money back. Netted into what was paid it would read as sales never paid
    for. It is reported on its own, and by default does not add to what is
    outstanding: that choice is a product decision, and both figures are given."""
    sales = [_run(14587, GRAPES, 14080.0, "2026-09-10", "2026-09-12")]
    payments = [_payment(14587, GRAPES, 14080.0, "2026-09-15"),
                {**_payment(14587, GRAPES, -7090.0, "2026-09-18", nett=0.0),
                 "accsale": "PRE*BT*400352"}]
    out = tracking.payment_status(sales, payments)
    assert out["still_to_come"] == 0.0
    assert [(r["accsale"], r["gross"], r["nett"]) for r in out["reversals"]] == [
        ("PRE*BT*400352", -7090.0, 0.0)]
    assert out["reversal_value"] == -7090.0
    assert out["still_to_come_with_reversals"] == 7090.0


def test_what_is_owed_is_grouped_by_the_market_that_owes_it():
    """Chasing is done market by market: one call covers every line on a floor."""
    sales = [{**_sale(14587, GRAPES, 1000.0, "2026-09-07"),
              "market": "TSHWANE MARKET", "market_agent": "Farmers Trust"},
             {**_sale(14588, GRAPES, 400.0, "2026-09-08"),
              "market": "TSHWANE MARKET", "market_agent": "Farmers Trust"},
             {**_sale(14599, NECTARINES, 2500.0, "2026-09-09"),
              "market": "DURBAN MARKET", "market_agent": "Grow Port Natal"}]
    out = tracking.payment_status(sales, [])
    markets = out["outstanding_markets"]
    assert [(m["market"], m["owed"], m["items"]) for m in markets] == [
        ("DURBAN MARKET", 2500.0, 1), ("TSHWANE MARKET", 1400.0, 2)]
    assert markets[1]["agents"] == "Farmers Trust"
    assert markets[1]["oldest"] == "2026-09-07"
    assert [r["owed"] for r in markets[1]["lines"]] == [1000.0, 400.0]
    # The flat list the print sheet uses is untouched, and both hold the same money.
    assert sum(m["owed"] for m in markets) == out["still_to_come"]


def test_a_line_whose_market_was_never_recorded_is_named_not_dropped():
    out = tracking.payment_status([_sale(14587, GRAPES, 500.0, "2026-09-07")], [])
    assert [m["market"] for m in out["outstanding_markets"]] == [tracking.UNPLACED]


def test_each_sales_day_says_what_came_back_for_it_and_what_is_still_owed():
    """A sale on the 31st paid on the 5th is money back for the 31st."""
    sales = [_run(14587, GRAPES, 1000.0, "2026-07-31", "2026-07-31"),
             _run(14588, GRAPES, 400.0, "2026-08-03", "2026-08-03")]
    payments = [_payment(14587, GRAPES, 1000.0, "2026-08-05", nett=850.0)]
    sd = tracking.compute(sales, payments)["sales_by_day"]
    by_day = {d["date"]: d for d in sd["days"]}
    assert (by_day["2026-07-31"]["paid"], by_day["2026-07-31"]["owed"]) == (850.0, 0.0)
    assert (by_day["2026-08-03"]["paid"], by_day["2026-08-03"]["owed"]) == (0.0, 400.0)
    months = {m["month"]: m for m in sd["months"]}
    assert (months["2026-07"]["value"], months["2026-07"]["paid"]) == (1000.0, 850.0)
    assert (months["2026-08"]["value"], months["2026-08"]["owed"]) == (400.0, 400.0)
    assert (sd["totals"]["paid"], sd["totals"]["owed"]) == (850.0, 400.0)


def test_what_the_days_owe_agrees_with_outstanding():
    sales, payments = _dn_14013()
    out = tracking.compute(sales, payments)
    days_owed = round(sum(d["owed"] for d in out["sales_by_day"]["days"]), 2)
    assert days_owed == out["payments"]["still_to_come"] == 805.0
