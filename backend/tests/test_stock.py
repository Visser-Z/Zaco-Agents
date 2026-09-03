"""Opening stock as a running balance across a consignment's account sales.

The rule was read off the operator's own book: 33 of 35 rows checked opened at
what the row before them left behind, and both exceptions were consignments that
had begun selling the month before the export being checked.
"""

from datetime import date

from app import stock
from app.schemas import StatementRow


def _row(stm: int, sold: int, *, consignment: int = 900, sent: int = 100, day: int = 1):
    return StatementRow(
        source_file="june.csv",
        dn=14211,
        stm_no=stm,
        consignment_id=consignment,
        product="PLUMS ANGELINO CLASS 1 LARGE (MULTI LAYER TRAYER 5.5kg)",
        qty_received=sent,
        cartons_sold=sold,
        invoice_date=date(2026, 6, day),
    )


def test_each_account_sale_opens_where_the_last_one_closed():
    rows = [_row(1, 60, day=1), _row(2, 30, day=8), _row(3, 5, day=15)]
    stock.carry_forward(rows)
    assert [r.opening_stock for r in rows] == [100, 40, 10]
    assert [r.baby_stock for r in rows] == [40, 10, 5]


def test_the_order_is_when_it_settled_not_the_order_it_was_read():
    """Files arrive in whatever order they were dropped, and a weekly export can
    be loaded after the one that follows it. The balance has to follow the
    account sales' own dates, or a later run would open at the full delivery."""
    rows = [_row(3, 5, day=15), _row(1, 60, day=1), _row(2, 30, day=8)]
    stock.carry_forward(rows)
    assert [(r.stm_no, r.opening_stock) for r in sorted(rows, key=lambda r: r.stm_no)] == [
        (1, 100), (2, 40), (3, 10),
    ]


def test_what_sold_in_earlier_rounds_is_taken_off_the_opening():
    """A consignment straddling two weekly exports must not restart at the full
    delivery in the second: this is exactly the case the historical sheet
    disagreed on until earlier sales were accounted for."""
    rows = [_row(5, 10, day=20)]
    stock.carry_forward(rows, {900: 85})
    assert rows[0].opening_stock == 15
    assert rows[0].baby_stock == 5


def test_consignments_do_not_borrow_each_others_stock():
    rows = [_row(1, 60, consignment=900, sent=100), _row(2, 5, consignment=901, sent=20)]
    stock.carry_forward(rows)
    assert [r.opening_stock for r in rows] == [100, 20]


def test_a_row_with_no_consignment_id_is_left_alone():
    """The PDF exports print no consignment id, and rows recorded before the
    split were one row per consignment already. Pooling everything with a blank
    id would carry stock between unrelated deliveries."""
    row = _row(1, 60)
    row.consignment_id = None
    row.opening_stock = 77
    assert stock.carry_forward([row]) == 0
    assert row.opening_stock == 77


def test_selling_more_than_was_on_the_floor_is_flagged_not_hidden():
    """The operator's own sheet has these (a delivery amended later, a return
    booked against the wrong consignment). Baby Stock is computed now, so a
    negative leftover would otherwise appear in the sheet unexplained."""
    rows = [_row(1, 60, day=1), _row(2, 50, day=8)]
    stock.carry_forward(rows)
    assert rows[1].baby_stock == -10
    assert stock.flag_impossible_stock(rows) == 1
    assert not rows[0].flags
    assert "Sold 50 out of 40" in rows[1].flags[0].message
    # A warning, not an error: it is a fact about the data, and blocking the
    # import would leave the operator no way to record what actually happened.
    assert rows[1].flags[0].severity == "warning"
    assert not rows[1].blocking
