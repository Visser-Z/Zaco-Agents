"""The market's Summary of Deliveries, and every outstanding line checked
against it: the comparison that found each wrong "owed" line by hand in
September 2026, done by the app for every line."""

from app import market_summary, tracking

REPORT = """
    Delivery Reports                                                 Zaco Agents (Pty) Ltd (20026)
    Report:   Summary of Deliveries By Agent                      Run Date: 2026/10/01 11:41:29
    Market:   ALL
    Date Range: 2026/08/01 - 2026/08/31

    Growfresh Port Natal (Dbn)
    GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET)
    Delivery ID   Supplier Ref    Date   Sent Qty Qty Amend To Sold Qty Avail Gross Turnover Amount Paid Unpaid Sales Ave Price
    1855491Z  20026*N             2026-08-27   480     360     360       0   R 133190.00 R 133190.00  R 0.00 R 369.97
    TSHWANE MARKET - Farmers Trust (Pre)
    GRAPES WHITE SEEDLESS CLASS 2 NO SIZE (PUNNET)
    Page 8/9
    Delivery ID   Supplier Ref    Date   Sent Qty Qty Amend To Sold Qty Avail Gross Turnover Amount Paid Unpaid Sales Ave Price
    1185853Z  20026*14628         2026-08-25    90              90       0    R 7200.00   R 7200.00   R 0.00 R 80.00
    Subtropico (Springs (Spr)
    NECTARINES OTHER CLASS 1 MEDIUM (DOMPEL JUMBLE)
    Delivery ID   Supplier Ref    Date   Sent Qty Qty Amend To Sold Qty Avail Gross Turnover Amount Paid Unpaid Sales Ave Price
    2398482Z  20026*14615         2026-08-17    99      79      89     -10   R 5580.00   R 5000.00   R 580.00 R 62.70
    2398482Z  20026*14615         2026-08-17    19              19       0   R 1450.00   R 1450.00   R 0.00 R 76.32
    1342     0     1241      101   R 155680.00 R 154080.00  R 1600.00
"""

WHITE = "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"
NECS = "NECTARINES OTHER CLASS 1 MEDIUM (DOMPEL JUMBLE 7kg)"


def test_the_report_is_read_through_its_spacing_page_breaks_and_odd_columns():
    recs = {(r["delivery_id"], r["product"]): r for r in market_summary.parse(REPORT, "s.pdf")}
    assert market_summary.run_at(REPORT) == "2026-10-01T11:41:29"
    assert market_summary.period(REPORT) == ("2026-08-01", "2026-08-31")
    celeb = recs[(1855491, "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE PUNNET")]
    assert (celeb["qty_sent"], celeb["sold"], celeb["paid"]) == (360, 360, 133190.0)
    # A page break between a product and its headings does not lose the product.
    white = recs[(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET")]
    assert white["agent"] == "TSHWANE MARKET - Farmers Trust (Pre)"
    # Two lines of one product on one delivery are one record, negative Qty Avail and all.
    necs = recs[(2398482, "NECTARINES OTHER CLASS 1 MEDIUM DOMPEL JUMBLE")]
    assert (necs["sold"], necs["gross"], necs["unpaid"]) == (108, 7030.0, 580.0)
    assert len(recs) == 3


def sale(cid, dn, product, value, day, market="TSHWANE MARKET"):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": market,
            "market_agent": "Farmers Trust", "cartons_sold": 10, "price": value / 10,
            "sales_total": value, "last_sale": day, "date_received": day, "group_date": day,
            "qty_received": 90}


def summary(delivery, product, gross, paid, unpaid, run="2026-10-01T11:41:29"):
    return {"delivery_id": delivery, "product": product, "product_name": product,
            "supplier_ref": "14628", "date_sent": "2026-08-25", "gross": gross,
            "paid": paid, "unpaid": unpaid, "run_at": run}


def check(sales, pays, summaries):
    lines = tracking.payment_status(sales, pays)["outstanding"]
    return market_summary.check(sales, pays, summaries, lines), lines


def test_a_line_the_market_says_is_paid_names_the_payment_dates_to_fetch():
    """PRE*BT*397227, R 7 200,00 for 31 August, deleted from the book: the
    market's summary still says paid, and the dates start at the sale."""
    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31")]
    out, lines = check(sales, [], [summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET",
                                          7200.0, 7200.0, 0.0)])
    m = lines[0]["market_view"]
    assert m["verdict"] == "market_paid" and m["difference"] == 7200.0
    assert (m["fetch_from"], m["fetch_to"]) == ("2026-08-31", "2026-10-01")
    assert out["lines"]["market_paid"] == 1 and out["agree"] == 0


def test_a_debt_the_market_agrees_with_is_confirmed():
    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31")]
    out, lines = check(sales, [], [summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET",
                                          7200.0, 0.0, 7200.0)])
    assert lines[0]["market_view"]["verdict"] == "agrees"
    assert out["agree"] == 1


def test_sales_after_the_report_was_printed_are_not_held_against_it():
    """The report ran at 11:41 on 1 October. A sale later that day, and one the
    day after, cannot be in it, so the line still agrees."""
    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31"),
             sale(118585301, 14628, WHITE, 300.0, "2026-10-01"),
             sale(118585301, 14628, WHITE, 500.0, "2026-10-02")]
    out, lines = check(sales, [], [summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET",
                                          7200.0, 0.0, 7200.0)])
    assert lines[0]["market_view"]["verdict"] == "agrees"
    assert "sales_gap" not in lines[0]["market_view"]


def test_a_line_no_summary_covers_names_the_month_to_fetch():
    sales = [sale(117230101, 14220, "STRAWBERRIES NO VARIETY NOT GRADED", 6750.0, "2026-04-16")]
    out, lines = check(sales, [], [summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET",
                                          7200.0, 7200.0, 0.0)])
    assert lines[0]["market_view"] == {"verdict": "unchecked", "reason": "no_summary",
                                  "month": "2026-04", "month_name": "April 2026"}
    assert out["unchecked_months"] == [{"month": "2026-04", "month_name": "April 2026",
                                        "owed": 6750.0}]


def test_a_product_the_market_left_off_its_summary_is_not_sent_for_again():
    """1185124Z's summary listed its Sweet Celebration and not its Grapes Class
    2: fetching that month's summary again would not add it."""
    sales = [sale(118512401, 14621, "GRAPES SWEET CELEBRATION CLASS 2 NO SIZE (PUNNET 5kg)",
                  70140.0, "2026-08-18"),
             sale(118512402, 14621, "GRAPES CLASS 2 NO SIZE (PUNNET 5kg)", 10400.0, "2026-09-28")]
    out, lines = check(sales, [], [summary(1185124, "GRAPES SWEET CELEBRATION CLASS 2 NO SIZE PUNNET",
                                          70140.0, 0.0, 70140.0)])
    by = {l["consignment_id"]: l["market_view"] for l in lines}
    assert by[118512402] == {"verdict": "unchecked", "reason": "not_listed"}
    assert by[118512401]["verdict"] == "agrees"
    assert out["not_listed"] == 10400.0 and out["unchecked_months"] == []


def test_the_latest_run_of_a_summary_wins():
    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31")]
    old = summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET", 7200.0, 0.0, 7200.0,
                  run="2026-09-02T09:00:00")
    new = summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET", 7200.0, 7200.0, 0.0)
    out, lines = check(sales, [], [new, old])
    assert lines[0]["market_view"]["verdict"] == "market_paid"


def test_the_chat_is_told_what_the_market_says_about_each_line():
    from app import assistant

    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31"),
             sale(117230101, 14220, "STRAWBERRIES NO VARIETY NOT GRADED", 6750.0, "2026-04-16")]
    text = assistant.settlement_context(sales, [], summaries=[summary(
        1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET", 7200.0, 7200.0, 0.0)])
    assert "Checked against the market's own Summary of Deliveries" in text
    assert "market says paid as of 2026-10-01: payment missing from the book, fetch " \
           "Payment Details 2026-08-31 to 2026-10-01" in text
    assert "not checked: needs the April 2026 Summary of Deliveries" in text
    assert "deliveries sent in April 2026" in text
    # Without summaries nothing about the market is said.
    assert "market says" not in assistant.settlement_context(sales, [])


def test_the_check_leaves_each_line_its_market_name():
    """A line's `market` is the market it is owed from; the verdict sits beside
    it, never over it, or every line loses where it is owed."""
    sales = [sale(118585301, 14628, WHITE, 7200.0, "2026-08-31")]
    out, lines = check(sales, [], [summary(1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET",
                                          7200.0, 7200.0, 0.0)])
    assert lines[0]["market"] == "TSHWANE MARKET"
    from app import assistant
    text = assistant.settlement_context(sales, [], summaries=[summary(
        1185853, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET", 7200.0, 7200.0, 0.0)])
    assert any(l.startswith("TSHWANE MARKET | Farmers Trust | 14628") for l in text.split("\n"))
