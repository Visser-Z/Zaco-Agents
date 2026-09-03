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
    captured = {}
    async def fake_delete(user, path, params):
        captured["path"] = path; captured["params"] = params
        return [{"id": 1}, {"id": 2}]
    monkeypatch.setattr(main, "db_delete", fake_delete)
    result = asyncio.run(main.delete_history(month="2026-07", week=None, user=USER))
    assert result == {"deleted": 2, "from": "2026-07-01", "to": "2026-07-31"}
    assert captured["path"] == "statements"
    assert captured["params"]["and"] == "(group_date.gte.2026-07-01,group_date.lte.2026-07-31)"
