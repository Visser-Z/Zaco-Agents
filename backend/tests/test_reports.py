"""A report for a date range: what sold, what came in, what it is worth."""

from app import reports


def _sale(day, product, cartons, price, returned=0, ret_value=0.0, agent="Farmers Trust"):
    return {"market_agent": agent, "product": product, "description": product,
            "cartons_sold": cartons, "price": price, "group_date": day,
            "cartons_returned": returned, "returns_total": ret_value,
            "supplier_ref": 14588, "dn": 14588, "consignment_id": 1,
            "qty_received": 100, "nett_total": None, "payment_refs": None}


def _pay(day, gross, nett, product="GRAPES"):
    return {"accsale": f"A{day}{gross}", "dn": 14588, "date": day, "gross": gross,
            "nett": nett, "stm_no": 1,
            "lines": [{"product": product, "sales_total": gross}]}


def test_the_range_is_inclusive_at_both_ends():
    sales = [_sale("2026-08-01", "GRAPES", 10, 100.0),
             _sale("2026-08-05", "GRAPES", 10, 100.0),
             _sale("2026-08-09", "GRAPES", 10, 100.0)]
    out = reports.build(sales, [], "2026-08-01", "2026-08-05")
    assert out["totals"]["statements"] == 2
    assert [d["date"] for d in out["by_day"]] == ["2026-08-01", "2026-08-05"]


def test_a_single_day_is_from_equals_to():
    sales = [_sale("2026-08-05", "GRAPES", 10, 100.0),
             _sale("2026-08-06", "GRAPES", 99, 100.0)]
    out = reports.build(sales, [], "2026-08-05", "2026-08-05")
    assert out["totals"]["cartons_sold"] == 10
    assert out["money"]["sold"] == 1000.0


def test_sales_and_payments_are_selected_on_their_own_dates():
    """A payment settles earlier sales, so the two must not be forced to agree.
    Both are reported for the range and the difference is left visible."""
    sales = [_sale("2026-08-01", "GRAPES", 10, 100.0)]      # sold on the 1st
    pays = [_pay("2026-08-05", 1000.0, 900.0)]              # paid on the 5th
    only_sale = reports.build(sales, pays, "2026-08-01", "2026-08-01")
    assert only_sale["money"]["sold"] == 1000.0
    assert only_sale["money"]["payments_received"] == 0.0   # not paid yet, that day

    only_pay = reports.build(sales, pays, "2026-08-05", "2026-08-05")
    assert only_pay["money"]["sold"] == 0.0
    assert only_pay["money"]["payments_received"] == 1000.0
    assert only_pay["money"]["payments_nett"] == 900.0
    assert only_pay["money"]["deductions"] == 100.0
    assert only_pay["money"]["deduction_rate"] == 0.1


def test_products_are_totalled_and_ordered_by_what_they_earned():
    sales = [_sale("2026-08-01", "SMALL", 1, 10.0),
             _sale("2026-08-01", "BIG", 100, 100.0),
             _sale("2026-08-02", "BIG", 50, 100.0)]
    out = reports.build(sales, [], "2026-08-01", "2026-08-31")
    names = [p["product"] for p in out["by_product"]]
    assert names[0] == "BIG"
    big = out["by_product"][0]
    assert (big["cartons"], big["value"], big["days_sold"], big["price"]) == (150, 15000.0, 2, 100.0)
    # the product totals reconcile to the money figure
    assert round(sum(p["value"] for p in out["by_product"]), 2) == out["money"]["sold"]


def test_returns_are_reported_beside_the_net_not_folded_into_it():
    sales = [_sale("2026-08-01", "GRAPES", 8, 100.0, returned=2, ret_value=200.0)]
    t = reports.build(sales, [], "2026-08-01", "2026-08-01")["totals"]
    assert (t["cartons_sold"], t["cartons_returned"], t["cartons_gross"]) == (8, 2, 10)
    assert t["return_rate"] == 0.2


def test_an_empty_range_is_a_valid_report_not_an_error():
    out = reports.build([_sale("2026-08-01", "GRAPES", 10, 100.0)], [], "2026-09-01", "2026-09-30")
    assert out["totals"]["statements"] == 0
    assert out["by_product"] == [] and out["by_day"] == []
    assert out["money"]["sold"] == 0.0
    assert out["range"]["first_sale"] is None


def test_undated_rows_never_land_in_a_range():
    sales = [_sale("2026-08-01", "GRAPES", 10, 100.0), _sale(None, "GRAPES", 99, 100.0)]
    out = reports.build(sales, [], "2026-08-01", "2026-08-31")
    assert out["totals"]["statements"] == 1


def test_figures_arriving_as_strings_report_the_same():
    def rows(text):
        n = (lambda v: str(v)) if text else (lambda v: v)
        return [{**_sale("2026-08-01", "GRAPES", n(10), n(100.0))}]
    assert reports.build(rows(True), [], "2026-08-01", "2026-08-01")["money"]["sold"] == \
           reports.build(rows(False), [], "2026-08-01", "2026-08-01")["money"]["sold"] == 1000.0


# --- what a row is worth, and which day it belongs to ---------------------

def test_a_line_is_worth_what_the_statement_says_not_cartons_times_price():
    """From the live book: 13 cartons of PLUMS ELDORADO sold for R740,00. The
    statement prints no unit price -- R56,92 is the average worked out from
    those two figures and rounded -- so 13 x R56,92 comes back as R739,96. The
    report showed the line at R1 359,96 where the dockets said R1 360,00."""
    sales = [
        dict(_sale("2026-09-03", "PLUMS ELDORADO", 13, 56.92), sales_total=740.00),
        dict(_sale("2026-09-04", "PLUMS ELDORADO", 2, 60.00), sales_total=120.00),
        dict(_sale("2026-09-07", "PLUMS ELDORADO", 5, 100.00), sales_total=500.00),
    ]
    line = reports.by_product(sales)[0]
    assert line["cartons"] == 20
    assert line["value"] == 1360.00
    # And the per-carton figure divides the total exactly, so the two columns
    # the operator reads across agree with each other.
    assert line["price"] == 68.00
    assert round(line["price"] * line["cartons"], 2) == line["value"]


def test_a_row_with_no_exact_value_still_falls_back_to_cartons_times_price():
    """History recorded before the sales value was captured must still count."""
    line = reports.by_product([_sale("2026-09-03", "GRAPES", 10, 100.0)])[0]
    assert line["value"] == 1000.0


def test_a_sale_belongs_to_the_month_it_sold_in_not_the_one_it_arrived_in():
    """A June delivery that sells in July is July's trade. Scoped on the
    delivery date it landed on June's report and July's never saw it -- on this
    book R224 940 of it in one month."""
    sales = [dict(_sale("2026-06-28", "GRAPES", 10, 100.0), last_sale="2026-07-02")]

    june = reports.build(sales, [], "2026-06-01", "2026-06-30")
    july = reports.build(sales, [], "2026-07-01", "2026-07-31")
    assert june["totals"]["statements"] == 0
    assert july["totals"]["statements"] == 1
    assert [d["date"] for d in july["by_day"]] == ["2026-07-02"]


def test_days_traded_counts_selling_days_not_delivery_days():
    """One load, sent once, sold down over three days is three days of trade."""
    sales = [dict(_sale("2026-09-03", "GRAPES", 5, 100.0), last_sale=d)
             for d in ("2026-09-03", "2026-09-04", "2026-09-07")]
    assert reports.by_product(sales)[0]["days_sold"] == 3
    assert [d["date"] for d in reports.by_day(sales)] == [
        "2026-09-03", "2026-09-04", "2026-09-07"]
