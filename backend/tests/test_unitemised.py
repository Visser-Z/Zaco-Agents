"""Two things the September 2026 reports showed the matcher getting wrong.

1. Subtropico paid money it did not itemise. SPR*SUB*47500 paid R 14 660,00
   and listed R 2 700,00 of products under it; the other R 11 960,00 was the
   delivery's first selling day, to the cent. The app read only the lines and
   called that day owed when the market's own summary said nothing was.

2. Durban paid a run of days, not the oldest ones. DUR*13*202568 paid 189
   cartons, R 71 100,00, for 1 to 4 September on delivery 1855491Z. Oldest
   first put it on 27 to 31 August, so August read paid and September owed,
   the wrong way round.
"""

from app import tracking
from app.main import _apply_checks

MARKET, AGENT = "SPRINGS MARKET", "Subtropico"
NECS = "NECTARINES OTHER CLASS 2 MEDIUM (DOMPEL JUMBLE 5kg)"
PLUMS = "PLUMS ELDORADO CLASS 2 MEDIUM (DOMPEL JUMBLE 7kg)"
CELEB = "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET 5kg)"


def day(cid, dn, product, sold, value, when, market=MARKET, first="2026-09-15", sent=150):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": market,
            "market_agent": AGENT, "cartons_sold": sold, "price": value / sold,
            "sales_total": value, "last_sale": when, "date_received": first,
            "group_date": first, "qty_received": sent, "qty_amended": sent}


def payment(accsale, dn, when, gross, lines, fms=None):
    return {"accsale": accsale, "dn": dn, "date": when, "gross": gross,
            "nett": round(gross * 0.85, 2), "fms_id": fms, "lines": lines}


def line(n, product, sold, value, delivered=150):
    return {"line_no": n, "product": product, "sold": sold, "sales_total": value,
            "delivered": delivered}


def owed(sales, pays):
    return tracking.payment_status(sales, pays)["still_to_come"]


# --- money paid without a line --------------------------------------------------

def springs():
    sales = [day(235130601, 14361, PLUMS, 33, 4130.0, "2026-09-15", sent=80),
             day(235130601, 14361, PLUMS, 1, 120.0, "2026-09-16", sent=80),
             day(235130603, 14361, NECS, 50, 6710.0, "2026-09-15"),
             day(235130603, 14361, NECS, 18, 2580.0, "2026-09-16")]
    # Lists only the 16th; the 15th, R 10 840,00, is paid but not itemised.
    pays = [payment("SPR*SUB*47500", 14361, "2026-09-17", 13540.0,
                    [line(1, PLUMS, 1, 120.0, 80), line(3, NECS, 18, 2580.0)], fms=109107)]
    return sales, pays


def test_unitemised_money_pays_the_whole_days_it_adds_up_to():
    sales, pays = springs()
    assert owed(sales, pays) == 0.0
    assert tracking.payment_flags(sales, pays)["count"] == 0


def test_without_the_gap_rule_the_first_day_would_read_owed():
    sales, pays = springs()
    pays[0]["gross"] = 2700.0          # as if the market had paid only what it listed
    assert owed(sales, pays) == 10840.0


def test_unitemised_money_that_fits_no_whole_days_is_flagged_not_guessed():
    sales, pays = springs()
    pays[0]["gross"] = 13000.0         # R 10 300,00 unitemised: no run of days is that
    assert owed(sales, pays) == 10840.0
    flags = tracking.payment_flags(sales, pays)
    assert [(f["kind"], f["accsale"], f["amount"]) for f in flags["items"]] == \
        [("unlisted", "SPR*SUB*47500", 10300.0)]
    item = flags["items"][0]
    assert "only listed 2,700.00" in item["message"]
    assert {c["consignment_id"] for c in item["candidates"]} == {235130601, 235130603}


def test_a_person_can_place_unitemised_money_on_a_sale():
    sales, pays = springs()
    pays[0]["gross"] = 13000.0
    _apply_checks(pays, [{"accsale": "SPR*SUB*47500", "product": tracking.NOT_LISTED,
                          "decision": "link", "consignment_id": 235130603}])
    assert pays[0]["lines"][-1]["sales_total"] == 10300.0
    assert pays[0]["lines"][-1]["linked_consignment"] == 235130603
    # The nectarines' R 6 710,00 is paid; R 3 590,00 is left over as credit,
    # and the plums' R 4 130,00 is still owed.
    assert owed(sales, pays) == 4130.0
    assert tracking.payment_flags(sales, pays)["count"] == 0


def test_the_nett_is_split_over_what_was_paid_not_what_was_listed():
    sales, pays = springs()
    rows = tracking.payment_status(sales, pays)
    # Nett reaching Zaco for these sales: 85% of what was paid, not of what was listed.
    assert rows["total_paid"] == round(13540.0 * 0.85, 2)
    lines, _, _, gaps = tracking._payment_lines(pays, sales)
    rate = next(iter(lines.values()))[0]["rate"]
    assert abs(rate - 0.85) < 1e-9 and abs(gaps[0]["rate"] - 0.85) < 1e-9


# --- a payment for a run of days -------------------------------------------------

def durban():
    m = "DURBAN MARKET"
    sales = [day(185549101, 1855491, CELEB, 20, 7600.0, "2026-08-27", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 65, 24700.0, "2026-08-28", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 62, 23760.0, "2026-08-31", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 61, 23220.0, "2026-09-01", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 47, 17100.0, "2026-09-02", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 52, 19760.0, "2026-09-03", m, "2026-08-27", 336),
             day(185549101, 1855491, CELEB, 29, 11020.0, "2026-09-04", m, "2026-08-27", 336)]
    pays = [payment("DUR*13*202568", None, "2026-09-09", 71100.0,
                    [line(1, CELEB, 189, 71100.0, 336)], fms=743396)]
    return sales, pays


def test_a_payment_for_a_run_of_days_pays_those_days():
    sales, pays = durban()
    status = tracking.payment_status(sales, pays)
    assert status["still_to_come"] == 56060.0
    # What is owed is August's, not September's.
    assert {o["date"] for o in status["outstanding"]} == {"2026-08-27"}
    filled = tracking.valued(sales)
    owed_rows, *_ = tracking._allocate(filled, pays)
    unpaid = sorted(r["last_sale"] for r in filled if owed_rows[id(r)] > 0.01)
    assert unpaid == ["2026-08-27", "2026-08-28", "2026-08-31"]


def test_two_runs_that_fit_are_a_guess_and_neither_is_taken():
    sales, pays = durban()
    # 28 August alone now also comes to R 23 220,00 and 61 cartons, like 1 September.
    sales[1].update(sales_total=23220.0, cartons_sold=61)
    pays[0]["gross"] = 23220.0
    pays[0]["lines"] = [line(1, CELEB, 61, 23220.0, 336)]
    filled = tracking.valued(sales)
    owed_rows, *_ = tracking._allocate(filled, pays)
    unpaid = sorted(r["last_sale"] for r in filled if owed_rows[id(r)] > 0.01)
    # Falls back to oldest first, as before: the 27th and part of the 28th.
    assert "2026-09-01" in unpaid


def test_a_run_must_match_the_cartons_as_well_as_the_money():
    sales, pays = durban()
    pays[0]["lines"][0]["sold"] = 150
    filled = tracking.valued(sales)
    owed_rows, *_ = tracking._allocate(filled, pays)
    assert owed_rows[id(filled[0])] == 0.0     # oldest first again


# --- how long a line has been owed ---------------------------------------------

def test_a_line_is_owed_since_its_oldest_unpaid_sale_not_its_first_sale():
    """Delivery 1855491Z sold from 27 August. 1 to 4 September is paid, so what
    is owed has been waiting since the 27th; with the August days paid instead
    it would be waiting since 1 September."""
    sales, pays = durban()
    line = tracking.payment_status(sales, pays)["outstanding"][0]
    assert line["date"] == "2026-08-27" and line["unpaid_since"] == "2026-08-27"

    pays[0]["gross"] = 56060.0
    pays[0]["lines"] = [line(1, CELEB, 147, 56060.0, 336)] if False else \
        [{"line_no": 1, "product": CELEB, "sold": 147, "sales_total": 56060.0, "delivered": 336}]
    pays[0]["date"] = "2026-09-02"
    status = tracking.payment_status(sales, pays)
    owed = status["outstanding"][0]
    assert owed["date"] == "2026-08-27"
    assert owed["unpaid_since"] == "2026-09-01"
    assert status["oldest_outstanding"] == "2026-09-01"


# --- a return the market had already paid for -----------------------------------

def tshwane_14587():
    """Delivery 14587 as the market reported it: paid in full to 25 August,
    100 cartons back on the 26th, R 22 910,00 sold after, R 7 090,00 clawed
    back on 18 September. Sold and paid both come to R 201 696,70."""
    m, prod = "TSHWANE MARKET", "GRAPES SUGRAONE CLASS 2 NO SIZE (PUNNET 5kg)"
    days = [("2026-08-11", 499, 178746.70), ("2026-08-25", 101, 30200.0),
            ("2026-08-26", -100, -30000.0), ("2026-08-31", 39, 8830.0),
            ("2026-09-01", 50, 6000.0), ("2026-09-03", 3, 1080.0),
            ("2026-09-07", 11, 4400.0), ("2026-09-16", 26, 2600.0)]
    sales = [{"consignment_id": 118309101, "dn": 14587, "product": prod, "market": m,
              "market_agent": "Farmers Trust", "cartons_sold": c, "price": abs(v / c),
              "sales_total": v, "last_sale": d, "date_received": "2026-07-30",
              "group_date": "2026-07-30", "qty_received": 600} for d, c, v in days]
    line = lambda sold, value: [{"product": prod, "sold": sold, "sales_total": value}]
    pays = [payment("PRE*BT*1", 14587, "2026-08-12", 178746.70, line(499, 178746.70)),
            payment("PRE*BT*396073", 14587, "2026-08-26", 30200.0, line(101, 30200.0)),
            {"accsale": "PRE*BT*400352", "dn": 14587, "date": "2026-09-18", "gross": -7090.0,
             "nett": 0.0, "lines": line(28, -7090.0)}]
    return sales, pays


def test_a_return_already_paid_for_pays_the_sales_after_it():
    sales, pays = tshwane_14587()
    status = tracking.payment_status(sales, pays)
    assert status["still_to_come"] == 0.0
    assert status["outstanding"] == []
    # The claw-back took back exactly what the return left over: settled, not
    # a R 7 090,00 exposure on top.
    assert status["reversals"] == []
    for month in ("2026-08", "2026-09"):
        lo, hi = analytics_bounds(month)
        assert tracking.payment_status(sales, pays, lo=lo, hi=hi)["still_to_come"] == 0.0


def test_a_claw_back_bigger_than_the_return_left_is_still_reported():
    sales, pays = tshwane_14587()
    pays[2]["gross"] = pays[2]["lines"][0]["sales_total"] = -9090.0
    status = tracking.payment_status(sales, pays)
    assert [r["gross"] for r in status["reversals"]] == [-2000.0]


def analytics_bounds(month):
    from app import analytics
    return analytics.period_bounds(month)


def test_a_claw_back_that_also_pays_for_something_still_pays_for_it():
    """PRE*BT*395300 took back R 1 488,00 of cherries and paid R 210,00 for
    granadillas. The account sale is negative as a whole, but the granadillas
    are paid, and the cherries take back what their return left over."""
    m = "TSHWANE MARKET"
    cher, gran = "CHERRIES OTHER CLASS 1 LARGE (HALF TRAY 2.5kg)", "GRANADILLAS NO VARIETY NOT GRADED NO SIZE (STANDARD TRAY)"
    row = lambda cid, prod, c, v, d: {"consignment_id": cid, "dn": 14628, "product": prod,
                                      "market": m, "market_agent": "Farmers Trust",
                                      "cartons_sold": c, "price": abs(v / c), "sales_total": v,
                                      "last_sale": d, "date_received": "2026-08-10",
                                      "group_date": "2026-08-10", "qty_received": 171}
    sales = [row(118584704, cher, 20, 5000.0, "2026-08-11"),
             row(118584704, cher, -6, -1500.0, "2026-08-15"),
             row(118584705, gran, 6, 210.0, "2026-08-18")]
    pays = [payment("PRE*BT*394232", 14628, "2026-08-14", 5000.0,
                    [{"product": cher, "sold": 20, "sales_total": 5000.0}]),
            {"accsale": "PRE*BT*395300", "dn": 14628, "date": "2026-08-21", "gross": -1278.0,
             "nett": 0.0, "lines": [{"product": cher, "sold": 0, "sales_total": -1488.0},
                                    {"product": gran, "sold": 6, "sales_total": 210.0}]}]
    status = tracking.payment_status(sales, pays)
    assert status["outstanding"] == []
    assert status["reversals"] == []
    # The return left R 1 500,00, the claw-back took R 1 488,00: R 12,00 is
    # still to Zaco's credit on the cherries.
    assert status["still_to_come"] == -12.0


def test_a_payment_with_no_lines_at_all_pays_the_whole_days_it_adds_up_to():
    """SPR*SUB*46255 paid R 3 850,00 on 20 August and printed no products. It
    is delivery 14615's first two days to the cent; the payment of the 25th
    paid the days after them, line by line."""
    nec, plum = "NECTARINES OTHER CLASS 1 MEDIUM (DOMPEL JUMBLE 7kg)", "PLUMS BLACK DIAMOND CLASS 1 MEDIUM (DOMPEL JUMBLE 5kg)"
    sales = [day(239848203, 14615, nec, 5, 1150.0, "2026-08-18", first="2026-08-18", sent=79),
             day(239848201, 14615, plum, 8, 1480.0, "2026-08-18", first="2026-08-18", sent=13),
             day(239848203, 14615, nec, 2, 440.0, "2026-08-19", first="2026-08-18", sent=79),
             day(239848201, 14615, plum, 3, 540.0, "2026-08-19", first="2026-08-18", sent=13),
             day(239848201, 14615, plum, 2, 200.0, "2026-08-20", first="2026-08-18", sent=13),
             day(239848203, 14615, nec, 5, 800.0, "2026-08-20", first="2026-08-18", sent=79)]
    pays = [payment("SPR*SUB*46255", 14615, "2026-08-20", 3610.0, [], fms=108554),
            payment("SPR*SUB*46424", 14615, "2026-08-25", 1000.0,
                    [line(1, plum, 2, 200.0, 13), line(3, nec, 5, 800.0, 79)], fms=108554)]
    status = tracking.payment_status(sales, pays)
    assert status["still_to_come"] == 0.0
    assert status["unattributed"]["count"] == 0

    pays[0].update(gross=3000.0, nett=2550.0)   # no whole days come to this: left alone
    status = tracking.payment_status(sales, pays)
    assert status["still_to_come"] == 3610.0
    assert status["unattributed"] == {"count": 1, "gross": 3000.0, "nett": 2550.0,
                                      "accsales": ["SPR*SUB*46255"]}
