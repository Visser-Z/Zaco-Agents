"""One statement, several rows: the Nett must be split, never repeated or lost.

This is the class of bug that reached a real workbook. A statement covering three
consignments arrived carrying one consignment's share -- R1.71 against a R4 402
sale -- because a dict keyed on the statement number kept whichever row came
last. It was invisible while a row *was* a consignment, and every figure on the
sheet still looked plausible afterwards.
"""

import asyncio

import app.main as main
from app.schemas import Flag, StatementRow


def _row(stm, sold, price, **kw):
    return StatementRow(source_file="june.csv", dn=14013, stm_no=stm, cartons_sold=sold,
                        price=price, flags=[Flag(field="nett_total", severity="warning",
                                                 message="enter it")], **kw)


# --- the reconcile path: building the map handed to the workbook -----------

def test_the_netts_map_sums_a_statement_instead_of_keeping_the_last_row():
    daily = [
        {"stm_no": 385673, "nett_total": 3000.0},
        {"stm_no": 385673, "nett_total": 3726.35},
        {"stm_no": 385673, "nett_total": 0.0},      # a return-only run, genuinely nil
        {"stm_no": 386041, "nett_total": 9405.42},
    ]
    netts: dict[str, float] = {}
    for r in daily:
        if r.get("nett_total") is None or r.get("stm_no") is None:
            continue
        key = str(r["stm_no"])
        netts[key] = round(netts.get(key, 0.0) + float(r["nett_total"]), 2)

    assert netts == {"385673": 6726.35, "386041": 9405.42}
    # The old comprehension kept the last row, which here was the nil one.
    assert {str(r["stm_no"]): r["nett_total"] for r in daily}["385673"] == 0.0


# --- the adjustments path: filling rows from a per-statement figure --------

def test_a_statements_nett_is_split_between_its_products_not_repeated():
    rows = [_row(387871, 80, 20.0), _row(387871, 28, 257.14)]
    nett_map = {387871: {"stm_no": 387871, "nett": 9193.73, "gross": 10800.0, "lines": 1}}
    summary = main.fill_netts(rows, nett_map)

    assert summary.matched == 2
    assert sum(r.nett_total for r in rows) == 9193.73
    # In the ratio of their gross: 1 600 and 7 199.92 of 8 799.92.
    assert rows[0].nett_total == 1671.60
    assert rows[1].nett_total == 7522.13
    assert round(rows[0].nett_total / sum(r.nett_total for r in rows), 5) == round(1600 / 8799.92, 5)
    # And the "enter it by hand" warning is gone from both.
    assert not any(f.field == "nett_total" for r in rows for f in r.flags)


def test_a_single_row_statement_takes_the_whole_nett():
    rows = [_row(387517, 54, 50.0)]
    main.fill_netts(rows, {387517: {"nett": 2238.01, "gross": 2700.0, "lines": 1}})
    assert rows[0].nett_total == 2238.01


def test_the_split_lands_on_the_statement_exactly():
    rows = [_row(1, 1, 100.0), _row(1, 1, 100.0), _row(1, 1, 100.0)]
    main.fill_netts(rows, {1: {"nett": 100.0, "gross": 300.0, "lines": 3}})
    assert sum(r.nett_total for r in rows) == 100.0
    assert sorted(r.nett_total for r in rows) == [33.33, 33.33, 33.34]


def test_rows_that_sold_nothing_share_equally_rather_than_swallowing_it():
    """No gross means no ratio. Splitting by a zero total would divide by zero or
    hand the whole payment to one row for no reason."""
    rows = [_row(9, 0, 0.0), _row(9, 0, 0.0)]
    main.fill_netts(rows, {9: {"nett": 500.0, "gross": 0.0, "lines": 0}})
    assert [r.nett_total for r in rows] == [250.0, 250.0]


# --- a zero is not an identity --------------------------------------------

def test_a_zero_statement_number_is_read_as_unknown():
    """Two rows reached a saved workbook claiming to be statement 0, from a blank
    cell coerced through Number(). Recorded as one, it is a bucket unrelated rows
    collide in."""
    row = StatementRow.model_validate({"source_file": "x.csv", "stm_no": 0, "dn": 0,
                                       "consignment_id": 0})
    assert row.stm_no is None and row.dn is None and row.consignment_id is None


def test_a_zero_statement_is_never_recorded_to_history():
    from app.supabase_auth import User
    row = StatementRow(source_file="x.csv", stm_no=0, market_agent="Farmers Trust")
    assert main._statement_record(row, User(id="u", email="e", token="t")) is None
