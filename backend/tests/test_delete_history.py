"""Period bounds used by the delete-history action.

The Nett splitting rule that used to be tested here alongside the workbook
writeback now lives entirely in ``reconcile``, and is covered by
``test_nett_split.py``.
"""

import asyncio
from datetime import date

import app.main as main
from app import analytics
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


# --- period bounds for delete --------------------------------------------

def test_period_bounds_month():
    assert analytics.period_bounds(month="2026-07") == ("2026-07-01", "2026-07-31")
    assert analytics.period_bounds(month="2026-02") == ("2026-02-01", "2026-02-28")


def test_period_bounds_week_and_precedence():
    lo, hi = analytics.period_bounds(week="2026-W31")
    assert date.fromisoformat(lo).isoweekday() == 1    # Monday
    assert date.fromisoformat(hi).isoweekday() == 7    # Sunday
    # week wins when both are given
    assert analytics.period_bounds(month="2026-07", week="2026-W31") == (lo, hi)


def test_period_bounds_none():
    assert analytics.period_bounds() == (None, None)


# --- delete endpoint guards ----------------------------------------------

def test_delete_requires_a_period(monkeypatch):
    called = False
    async def fake_delete(*a, **k):
        nonlocal called; called = True; return []
    monkeypatch.setattr(main, "db_delete", fake_delete)
    import pytest
    with pytest.raises(Exception):
        asyncio.run(main.delete_history(month=None, week=None, user=USER))
    assert not called


def test_delete_takes_the_rows_the_period_actually_shows(monkeypatch):
    """Statements go by id, chosen the way the pages date them; payments go by
    their own date column.

    A row with no group_date is still dated -- the pages fall back to the
    invoice date -- so filtering the delete on group_date alone left rows that
    August would show again on the next refresh.
    """
    store = [
        {"id": 1, "group_date": "2026-07-04", "invoice_date": None,
         "date_received": None, "created_at": None},
        {"id": 2, "group_date": None, "invoice_date": "2026-07-09",   # July, no group_date
         "date_received": None, "created_at": None},
        {"id": 3, "group_date": "2026-08-02", "invoice_date": None,   # a different month
         "date_received": None, "created_at": None},
    ]
    calls = []
    async def fake_delete(user, path, params):
        calls.append((path, params))
        if path != "statements":
            return [{"accsale": "A"}]
        ids = {x for x in params["id"].removeprefix("in.(").rstrip(")").split(",") if x}
        gone = [r for r in store if str(r["id"]) in ids]
        for r in gone:
            store.remove(r)
        return gone
    async def fake_get(user, path, params):
        return list(store) if path == "statements" else []
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(month="2026-07", week=None, user=USER))
    assert result["deleted"] == 2            # both July rows, group_date or not
    assert result["remaining"] == 0
    assert result["from"] == "2026-07-01" and result["to"] == "2026-07-31"
    assert [r["id"] for r in store] == [3]   # August untouched

    by_table = dict(calls)
    assert by_table["statements"]["id"].startswith("in.(")
    assert by_table["payments"]["and"] ==         "(paid_on.gte.2026-07-01,paid_on.lte.2026-07-31)"


def test_rows_it_could_not_remove_are_reported(monkeypatch):
    """Row-level security answers a refused delete with 200 and an empty list.
    Reported as a success, that is a period which looks deleted and is still
    on Tracking a moment later."""
    store = [{"id": 1, "group_date": "2026-07-04", "invoice_date": None,
              "date_received": None, "created_at": None}]
    async def fake_delete(user, path, params):
        return []                       # RLS removes nothing
    async def fake_get(user, path, params):
        return list(store) if path == "statements" else []
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(month="2026-07", week=None, user=USER))
    assert result["deleted"] == 0
    assert result["remaining"] == 1
