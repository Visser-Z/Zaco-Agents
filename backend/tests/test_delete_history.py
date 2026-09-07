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


def test_delete_scopes_to_the_period(monkeypatch):
    """Both tables are scoped to the same window, each by its own date column."""
    calls = []
    async def fake_delete(user, path, params):
        calls.append((path, params))
        return [{"id": 1}, {"id": 2}]
    async def fake_get(user, path, params):
        return []                      # no dismissals to prune
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "db_get", fake_get)

    result = asyncio.run(main.delete_history(month="2026-07", week=None, user=USER))
    assert result == {"deleted": 2, "payments_deleted": 2, "closed_cleared": 0,
                      "from": "2026-07-01", "to": "2026-07-31"}

    windows = dict(calls)
    assert windows["statements"]["and"] ==         "(group_date.gte.2026-07-01,group_date.lte.2026-07-31)"
    assert windows["payments"]["and"] ==         "(paid_on.gte.2026-07-01,paid_on.lte.2026-07-31)"
