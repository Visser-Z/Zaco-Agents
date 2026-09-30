"""Payments the matcher cannot be sure of, and what a person decides about them.

On the live book when this was built there were none: every rand was placed
either to the cent or on a delivery that had started selling by the day it was
paid. A first draft flagged sixteen, and on inspection every one was noise,
most of them deliveries paid to the cent whose payment simply pre-dated the
book's last sales date. So these tests pin both halves: what must be flagged,
and what must not be, because a list of false alarms teaches people to stop
reading it.
"""

from app import tracking
from app.main import _apply_checks

MARKET, AGENT = "TSHWANE MARKET", "Farmers Trust"
CRIMSON = "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"
PLUMS = "PLUMS ELDORADO CLASS 1 NO SIZE (DOMPEL JUMBLE 5kg)"


def sale(cid, dn, sent, sold, value, day, arrived=None, product=CRIMSON):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": MARKET,
            "market_agent": AGENT, "cartons_sold": sold, "price": value / sold,
            "sales_total": value, "last_sale": day, "date_received": arrived or day,
            "group_date": arrived or day, "qty_received": sent}


def pay(accsale, dn, day, gross, delivered=None, sold=None, product=CRIMSON):
    line = {"product": product, "sales_total": gross}
    if delivered is not None:
        line["delivered"] = delivered
    if sold is not None:
        line["sold"] = sold
    return {"accsale": accsale, "dn": dn, "date": day, "gross": gross,
            "nett": round(gross * 0.85, 2), "lines": [line]}


def kinds(sales, payments):
    return [(f["kind"], f["accsale"]) for f in tracking.payment_flags(sales, payments)["items"]]


# --- what must be flagged ------------------------------------------------------

def test_a_payment_on_a_note_with_nothing_to_take_it():
    """The market typed a delivery note that has no sales of the product: the
    money has nowhere to go, and there is a sale of it elsewhere still owing."""
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*1", 142, "2026-09-10", 20000.0)]
    flags = tracking.payment_flags(sales, payments)
    assert kinds(sales, payments) == [("unplaced", "PRE*BT*1")]
    item = flags["items"][0]
    assert "delivery note 142 has no unpaid sales" in item["message"]
    assert [c["dn"] for c in item["candidates"]] == [14621]
    assert item["candidates"][0]["owed"] == 20000.0


def test_a_carton_count_that_belongs_to_another_delivery():
    """Filed under 14621, but it says 336 cartons were delivered: that is the
    size of 14630's load, not 14621's."""
    sales = [sale(1462101, 14621, 120, 120, 24000.0, "2026-09-02"),
             sale(1463001, 14630, 336, 100, 20000.0, "2026-09-03")]
    payments = [pay("PRE*BT*2", 14621, "2026-09-10", 20000.0, delivered=336)]
    flags = tracking.payment_flags(sales, payments)
    assert kinds(sales, payments) == [("elsewhere", "PRE*BT*2")]
    assert "the size of delivery note 14630, not of 14621" in flags["items"][0]["message"]


def test_a_payment_made_before_its_only_delivery_sold_anything():
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*3", 14621, "2026-08-20", 20000.0)]
    flags = tracking.payment_flags(sales, payments)
    assert kinds(sales, payments) == [("loose", "PRE*BT*3")]
    assert "did not start selling until 2026-09-02" in flags["items"][0]["message"]


# --- what must NOT be flagged -------------------------------------------------

def test_a_payment_a_few_days_ahead_of_the_book_is_not_doubt():
    """The noise from the first draft: a delivery that started selling on the
    17th, paid to the cent, one payment dated a day before the book's last
    sales day for it."""
    sales = [sale(4979601, 4979691, 200, 150, 30000.0, "2026-08-20", arrived="2026-08-17"),
             sale(4979601, 4979691, 200, 50, 10000.0, "2026-09-01", arrived="2026-09-01")]
    payments = [pay("DUR*13*1", 4979691, "2026-08-22", 30000.0),
                pay("DUR*13*2", 4979691, "2026-08-31", 10000.0)]
    assert kinds(sales, payments) == []


def test_a_sibling_line_on_the_same_note_is_not_a_wrong_note():
    """Two consignments of one product on one delivery note: the count points
    at the other line, but the note is right, and that is not a mismatch."""
    sales = [sale(1458602, 14586, 11, 11, 3300.0, "2026-08-04"),
             sale(1458603, 14586, 30, 30, 9000.0, "2026-08-05")]
    payments = [pay("PRE*BT*4", 14586, "2026-08-10", 12300.0, delivered=30)]
    assert kinds(sales, payments) == []


def test_a_small_count_matching_another_load_is_coincidence():
    sales = [sale(1462101, 14621, 8, 8, 1600.0, "2026-09-02"),
             sale(1463001, 14630, 5, 5, 1000.0, "2026-09-03")]
    payments = [pay("PRE*BT*5", 14621, "2026-09-10", 1600.0, delivered=5)]
    assert kinds(sales, payments) == []


def test_a_clean_book_has_nothing_to_check():
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*6", 14621, "2026-09-10", 20000.0, delivered=100, sold=100)]
    out = tracking.payment_flags(sales, payments)
    assert out == {"items": [], "count": 0, "value": 0.0, "decided": 0}


# --- what a person decides ----------------------------------------------------

def test_keeping_a_flagged_payment_stops_the_flag_and_changes_nothing_else():
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*3", 14621, "2026-08-20", 20000.0)]
    before = tracking.payment_status(sales, payments)["still_to_come"]
    _apply_checks(payments, [{"accsale": "PRE*BT*3", "product": CRIMSON, "decision": "keep"}])
    assert kinds(sales, payments) == []
    assert tracking.payment_status(sales, payments)["still_to_come"] == before


def test_linking_moves_the_money_to_the_sale_a_person_chose():
    """The payment was filed under 142. A person says it was 14621's: 14621
    comes off the outstanding list, everywhere, and the flag goes."""
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*1", 142, "2026-09-10", 20000.0)]
    assert tracking.payment_status(sales, payments)["still_to_come"] == 20000.0
    _apply_checks(payments, [{"accsale": "PRE*BT*1", "product": CRIMSON,
                              "decision": "link", "consignment_id": 1462101}])
    assert tracking.payment_status(sales, payments)["still_to_come"] == 0.0
    assert kinds(sales, payments) == []
    assert tracking.payment_flags(sales, payments)["decided"] == 1


def test_a_link_is_matched_on_the_product_however_the_report_spelled_it():
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*1", 142, "2026-09-10", 20000.0,
                    product="GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE PUNNET 5.00 kg")]
    _apply_checks(payments, [{"accsale": "pre*bt*1", "product": CRIMSON,
                              "decision": "link", "consignment_id": 1462101}])
    assert payments[0]["lines"][0]["linked_consignment"] == 1462101


def test_a_decision_about_one_product_leaves_the_other_lines_alone():
    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02"),
             sale(1462102, 14621, 50, 50, 5000.0, "2026-09-02", product=PLUMS)]
    payments = [{"accsale": "PRE*BT*9", "dn": 142, "date": "2026-09-10", "gross": 25000.0,
                 "nett": 21250.0, "lines": [{"product": CRIMSON, "sales_total": 20000.0},
                                            {"product": PLUMS, "sales_total": 5000.0}]}]
    _apply_checks(payments, [{"accsale": "PRE*BT*9", "product": CRIMSON,
                              "decision": "link", "consignment_id": 1462101}])
    lines = payments[0]["lines"]
    assert lines[0].get("linked_consignment") == 1462101
    assert "linked_consignment" not in lines[1]
    # The plums are still unplaced, so still flagged.
    assert kinds(sales, payments) == [("unplaced", "PRE*BT*9")]


def test_the_chat_is_told_what_is_waiting_to_be_checked():
    from app import assistant

    sales = [sale(1462101, 14621, 100, 100, 20000.0, "2026-09-02")]
    payments = [pay("PRE*BT*1", 142, "2026-09-10", 20000.0)]
    text = assistant.settlement_context(sales, payments)
    assert "Payments waiting to be checked by hand on the Tracking tab: 1" in text
    assert "PRE*BT*1" in text and "No sale to put it on" in text
    clean = assistant.settlement_context(sales, [pay("PRE*BT*6", 14621, "2026-09-10", 20000.0)])
    assert "Payments waiting to be checked by hand: none" in clean
