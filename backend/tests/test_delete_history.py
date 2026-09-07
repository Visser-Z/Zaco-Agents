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
        asyncio.run(main.delete_history(scope=None, month=None, week=None, user=USER))
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
    def _in_window(r, params):
        w = params.get("and") or ""
        if "group_date.gte." not in w:
            return False
        lo = w.split("group_date.gte.")[1].split(",")[0]
        hi = w.split("group_date.lte.")[1].rstrip(")")
        return r["group_date"] is not None and lo <= r["group_date"] <= hi

    async def fake_delete(user, path, params):
        calls.append((path, params))
        if path != "statements":
            return [{"accsale": "A"}]
        if "id" in params:
            ids = {x for x in params["id"].removeprefix("in.(").rstrip(")").split(",") if x}
            gone = [r for r in store if str(r["id"]) in ids]
        else:
            gone = [r for r in store if _in_window(r, params)]
        for r in gone:
            store.remove(r)
        return gone

    async def fake_get(user, path, params):
        if path != "statements":
            return []
        rows = store
        if params.get("group_date") == "is.null":
            rows = [r for r in rows if r["group_date"] is None]
        elif params.get("and"):
            rows = [r for r in rows if _in_window(r, params)]
        return list(rows)
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(scope=None, month="2026-07", week=None, user=USER))
    assert result["deleted"] == 2            # both July rows, group_date or not
    assert result["remaining"] == 0
    assert result["from"] == "2026-07-01" and result["to"] == "2026-07-31"
    assert [r["id"] for r in store] == [3]   # August untouched

    by_table = dict(calls)
    stmt_calls = [p for t, p in calls if t == "statements"]
    assert any("group_date.gte.2026-07-01" in (p.get("and") or "") for p in stmt_calls)
    assert any("id" in p for p in stmt_calls)   # the undated stray, by id
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
        if path != "statements":
            return []
        if params.get("group_date") == "is.null":
            return [r for r in store if r["group_date"] is None]
        return list(store)              # the dated-window read
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(scope=None, month="2026-07", week=None, user=USER))
    assert result["deleted"] == 0
    assert result["remaining"] == 1


def test_a_history_larger_than_the_read_cap_still_deletes(monkeypatch):
    """The dated rows must go by a filter the database applies, never by
    reading their ids first.

    PostgREST caps how many rows a read returns and gives no order unless one
    is asked for. Selecting ids and deleting those means that on a history
    bigger than the cap the rows to delete may not be in the page that comes
    back, and the delete removes nothing while reporting success.
    """
    CAP = 100
    store = [{"id": i, "group_date": "2026-07-04", "invoice_date": None,
              "date_received": None, "created_at": None} for i in range(1, 501)]

    def _in_window(r, params):
        w = params.get("and") or ""
        if "group_date.gte." not in w or r["group_date"] is None:
            return False
        lo = w.split("group_date.gte.")[1].split(",")[0]
        hi = w.split("group_date.lte.")[1].rstrip(")")
        return lo <= r["group_date"] <= hi

    async def fake_delete(user, path, params):
        if path != "statements":
            return []
        gone = ([r for r in store if str(r["id"]) in
                 set(params["id"].removeprefix("in.(").rstrip(")").split(","))]
                if "id" in params else [r for r in store if _in_window(r, params)])
        for r in gone:
            store.remove(r)
        return gone

    async def fake_get(user, path, params):
        if path != "statements":
            return []
        rows = store
        if params.get("group_date") == "is.null":
            rows = [r for r in rows if r["group_date"] is None]
        elif params.get("and"):
            rows = [r for r in rows if _in_window(r, params)]
        return list(rows[:CAP])          # the server's row cap

    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(scope=None, month="2026-07", week=None, user=USER))
    assert result["deleted"] == 500, "the whole period must go, not one page of it"
    assert result["remaining"] == 0
    assert store == []
