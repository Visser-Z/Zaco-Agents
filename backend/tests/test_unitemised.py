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
