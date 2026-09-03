"""Duplicate detection: warn when a statement is already in the saved history.

The check keys on the statement number (stm_no), not the Delivery ID (dn),
because one delivery legitimately spans several save rounds. These tests stub
the database read so no Supabase is needed.
"""

import asyncio

import app.main as main
from app.schemas import StatementRow
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


def _row(stm_no, dn=1000, **kw):
    return StatementRow(source_file="f.pdf", market_agent="Farmers Trust",
                        stm_no=stm_no, dn=dn, **kw)


def _run_with_existing(rows, existing, monkeypatch):
    async def fake_db_get(user, path, params=None):
        return existing
    monkeypatch.setattr(main, "db_get", fake_db_get)
    asyncio.run(main.flag_duplicates(USER, rows))


def test_matching_statement_gets_a_nonblocking_warning(monkeypatch):
    rows = [_row(118170501), _row(999999999)]
    existing = [{"stm_no": 118170501, "market_agent": "Farmers Trust", "created_at": "2026-08-01T10:00:00+00:00"}]
    _run_with_existing(rows, existing, monkeypatch)

    dup, fresh = rows
    flag = next(f for f in dup.flags if f.field == "stm_no")
    assert flag.severity == "warning"
    assert "Already saved on 2026-08-01" in flag.message
    assert "Farmers Trust" in flag.message
    assert not dup.blocking            # a warning must not block the append
    assert fresh.flags == []           # the unseen statement is untouched


def test_delivery_id_reuse_is_not_flagged(monkeypatch):
    # Same delivery (dn), different statement numbers arriving in a later round:
    # this is the normal multi-round workflow and must NOT be flagged.
    rows = [_row(200000002, dn=5000), _row(200000003, dn=5000)]
    existing = [{"stm_no": 200000001, "market_agent": "Farmers Trust", "created_at": "2026-08-01T10:00:00Z"}]
    _run_with_existing(rows, existing, monkeypatch)
    assert all(r.flags == [] for r in rows)


def test_no_user_skips_the_check(monkeypatch):
    called = False
    async def fake_db_get(*a, **k):
        nonlocal called; called = True; return []
    monkeypatch.setattr(main, "db_get", fake_db_get)
    rows = [_row(1)]
    asyncio.run(main.flag_duplicates(None, rows))
    assert not called and rows[0].flags == []


def test_no_statement_numbers_skips_the_query(monkeypatch):
    called = False
    async def fake_db_get(*a, **k):
        nonlocal called; called = True; return []
    monkeypatch.setattr(main, "db_get", fake_db_get)
    rows = [StatementRow(source_file="f.pdf", stm_no=None)]
    asyncio.run(main.flag_duplicates(USER, rows))
    assert not called
