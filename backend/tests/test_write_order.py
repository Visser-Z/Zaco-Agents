"""DN order on the sheet, and the grouping that depends on it.

Rows arrive in the order the files were read. On a real four-file round the DNs
came out 14828, 14815, ..., 14799, 14776, 14828, ... and 6 of 9 DNs were split
into separate blocks further down. The group-by was never broken; nothing sorted.
"""

from datetime import date

from app import workbook
from app.schemas import StatementRow


def _row(dn, stm=1, product="IMP Nect", sold=10, invoice=date(2026, 6, 1)):
    return StatementRow(source_file="june.csv", dn=dn, stm_no=stm, description=product,
                        cartons_sold=sold, price=100.0, invoice_date=invoice)


def dns(rows):
    return [r.dn for r in workbook.write_order(rows)]


def test_dns_come_out_ascending():
    assert dns([_row(14954), _row(14776), _row(14828), _row(14798)]) == [14776, 14798, 14828, 14954]


def test_the_sort_is_numeric_not_alphabetical():
    """As text, "14815" sorts before "148" and after "1476". The DN column holds
    numbers and has to be ordered as numbers."""
    assert dns([_row(14815), _row(148), _row(1476), _row(9)]) == [9, 148, 1476, 14815]


def test_every_dn_ends_up_in_one_unbroken_block():
    """This is the grouping fault: the same DN appearing in two places on the
    sheet, because its rows came from two different weekly files."""
    scattered = [_row(14828, 1), _row(14815, 2), _row(14776, 3),
                 _row(14828, 4), _row(14815, 5), _row(14828, 6)]
    order = dns(scattered)
    assert order == [14776, 14815, 14815, 14828, 14828, 14828]
    for dn in set(order):
        first, last = order.index(dn), len(order) - 1 - order[::-1].index(dn)
        assert last - first + 1 == order.count(dn), f"DN {dn} is split apart"


def test_one_products_rows_stay_together_within_a_dn():
    rows = [_row(14815, 1, "Strawberries"), _row(14815, 2, "Imp Cherries 5kg"),
            _row(14815, 3, "Strawberries"), _row(14815, 4, "Imp Cherries 5kg")]
    assert [r.description for r in workbook.write_order(rows)] == [
        "Imp Cherries 5kg", "Imp Cherries 5kg", "Strawberries", "Strawberries"]


def test_a_products_rows_run_in_settlement_order_so_the_stock_reads_down():
    """Opening stock is a running balance. Out of order it reads as nonsense: 5
    on the floor before the 120 it came out of."""
    rows = [_row(14815, 384100, invoice=date(2026, 6, 8)),
            _row(14815, 386757, invoice=date(2026, 6, 26)),
            _row(14815, 383735, invoice=date(2026, 6, 5))]
    assert [r.stm_no for r in workbook.write_order(rows)] == [383735, 384100, 386757]


def test_rows_with_no_dn_go_last_rather_than_pooling_under_a_blank():
    order = dns([_row(None), _row(14828), _row(None), _row(14776)])
    assert order == [14776, 14828, None, None]


def test_the_sheet_is_written_in_that_order():
    wb = workbook.new_workbook()
    workbook.append_rows(wb, [_row(14954, 1), _row(14776, 2), _row(14828, 3)],
                         market_agent="Farmers Trust")
    ws = wb["Sheet1"]
    assert [ws[f"A{r}"].value for r in (2, 3, 4)] == [14776, 14828, 14954]


def test_appending_leaves_rows_already_in_the_sheet_where_they_are():
    """Re-ordering existing rows would mean rewriting the operator's own data and
    its formulas. A save must not do that, so only the new batch is sorted."""
    wb = workbook.new_workbook()
    workbook.append_rows(wb, [_row(14954, 1)], market_agent="Farmers Trust")
    workbook.append_rows(wb, [_row(14776, 2), _row(14828, 3)], market_agent="Farmers Trust")
    ws = wb["Sheet1"]
    assert [ws[f"A{r}"].value for r in (2, 3, 4)] == [14954, 14776, 14828]
