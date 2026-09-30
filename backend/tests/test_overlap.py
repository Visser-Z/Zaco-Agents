"""Dropping days already read is harmless: saved days stay as they are, new
days are added, and where a report disagrees with the book it says so.

This is how a problem is diagnosed: pull a week's report over days already on
the book and see where the two differ. That only works if the second drop
cannot change or double what is there.
"""

import asyncio
from datetime import date

import app.main as main
from app import overlap
from app.schemas import StatementRow
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


def day(cid, when, cartons, value, stm=None, src="week.pdf"):
    return StatementRow(source_file=src, market_agent="Farmers Trust", stm_no=stm or cid,
                        consignment_id=cid, dn=14621, last_sale=date.fromisoformat(when),
                        cartons_sold=cartons, sales_total=value)


def saved(cid, when, cartons, value, stm=None):
    return {"stm_no": stm or cid, "consignment_id": cid, "sale_day": when,
            "cartons_sold": cartons, "sales_total": value,
            "market_agent": "Farmers Trust", "created_at": "2026-09-28T10:00:00+00:00"}


def codes(row):
    return [f.code for f in row.flags if f.code]


def run(rows, existing, monkeypatch):
    seen = {}

    async def fake_db_get(user, path, params=None):
        seen.update(params or {})
        return existing
    monkeypatch.setattr(main, "db_get", fake_db_get)
    asyncio.run(main.flag_duplicates(USER, rows))
    return seen


def test_a_day_on_the_book_is_left_alone_and_a_new_day_goes_in(monkeypatch):
    rows = [day(118512402, "2026-09-16", 74, 7400.0), day(118512402, "2026-09-28", 104, 10400.0)]
    run(rows, [saved(118512402, "2026-09-16", 74, 7400.0)], monkeypatch)
    assert codes(rows[0]) == [overlap.ALREADY]
    assert codes(rows[1]) == []


def test_a_report_that_disagrees_with_the_book_says_so(monkeypatch):
    rows = [day(118512402, "2026-09-16", 80, 8000.0)]
    run(rows, [saved(118512402, "2026-09-16", 74, 7400.0)], monkeypatch)
    flag = rows[0].flags[0]
    assert flag.code == overlap.DIFFERS and flag.severity == "warning"
    assert "74 cartons for R 7,400.00" in flag.message
    assert "80 cartons for R 8,000.00" in flag.message
    assert "saved figures are kept" in flag.message


def test_the_same_day_under_another_statement_number_is_still_the_same_day(monkeypatch):
    """The CSV files a day under its account sale, the PDF under its
    consignment: keyed on the statement they were two days, counted twice."""
    rows = [day(118512402, "2026-09-16", 74, 7400.0, stm=400350)]
    params = run(rows, [saved(118512402, "2026-09-16", 74, 7400.0)], monkeypatch)
    assert "consignment_id.in.(118512402)" in params["or"]
    assert codes(rows[0]) == [overlap.ALREADY]


def test_several_rows_of_one_day_on_the_book_are_compared_as_one(monkeypatch):
    rows = [day(118512402, "2026-09-16", 74, 7400.0)]
    run(rows, [saved(118512402, "2026-09-16", 30, 3000.0, stm=1),
               saved(118512402, "2026-09-16", 44, 4400.0, stm=2)], monkeypatch)
    assert codes(rows[0]) == [overlap.ALREADY]


def test_a_day_read_twice_in_one_drop_is_kept_once_the_fuller_copy():
    part = day(118512402, "2026-09-16", 50, 5000.0, src="16 Sep midday.pdf")
    full = day(118512402, "2026-09-16", 74, 7400.0, src="week.pdf")
    other = day(118512402, "2026-09-17", 10, 1000.0, src="week.pdf")
    kept, dropped = overlap.collapse_batch([part, full, other])
    assert kept == [full, other]
    assert dropped == [part]
    assert codes(part) == [overlap.REPEAT]
    assert "week.pdf" in part.flags[0].message


def test_different_days_and_consignments_are_never_collapsed():
    rows = [day(1, "2026-09-16", 5, 50.0), day(2, "2026-09-16", 5, 50.0),
            day(1, "2026-09-17", 5, 50.0)]
    kept, dropped = overlap.collapse_batch(rows)
    assert kept == rows and dropped == []
