"""When the database is behind the code, say which migration is missing.

PostgREST answers with raw Postgres text ("column statements.consignment_id does
not exist") or a bare 404. Passed through, that reads as a broken app rather than
one pending setup step -- which is exactly how it was read.
"""

import asyncio

import app.main as main
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


class _Boom(Exception):
    def __init__(self, detail):
        self.detail = detail


def test_a_missing_column_names_its_migration():
    exc = _Boom('column statements.consignment_id does not exist')
    assert main._pending_migration(exc) == "0010_statements_consignment_id.sql"


def test_a_missing_column_on_a_later_migration_names_that_one():
    assert main._pending_migration(
        _Boom("column statements.cartons_returned does not exist")) == "0012_statements_returns.sql"


def test_an_ordinary_failure_is_not_blamed_on_a_migration():
    """A network blip or an RLS refusal must not be reported as pending setup, or
    the operator runs a migration that was never the problem."""
    assert main._pending_migration(_Boom("violates row-level security policy")) is None
    assert main._pending_migration(_Boom("connection reset")) is None


def test_history_write_says_what_to_do_and_keeps_nothing_back(monkeypatch):
    """The workbook is the deliverable and must be returned regardless, and the
    message has to name the fix rather than quote Postgres."""
    async def fake_post(*a, **kw):
        raise _Boom('column statements.consignment_id does not exist')
    monkeypatch.setattr(main, "db_post", fake_post)

    from app.schemas import StatementRow
    row = StatementRow(source_file="june.csv", stm_no=1, market_agent="Farmers Trust")
    warning = asyncio.run(main.persist_statements(USER, [row]))
    assert "0010_statements_consignment_id.sql" in warning
    assert "Excel file saved normally" in warning
    assert "no data has been lost" in warning


def test_the_write_is_never_retried_without_the_new_column(monkeypatch):
    """Falling back to the older unique key would keep the first product on an
    account sale and drop the rest silently, which is the whole reason the key
    had to change. One attempt, then an honest warning."""
    calls = []

    async def fake_post(*a, **kw):
        calls.append(kw.get("on_conflict"))
        raise _Boom('column statements.consignment_id does not exist')
    monkeypatch.setattr(main, "db_post", fake_post)

    from app.schemas import StatementRow
    rows = [StatementRow(source_file="june.csv", stm_no=1, market_agent="FT", consignment_id=i)
            for i in (900, 901)]
    asyncio.run(main.persist_statements(USER, rows))
    assert calls == ["market_agent,stm_no,consignment_id,group_date"]


def test_schema_gaps_reports_each_missing_migration_once(monkeypatch):
    """One probe per migration, and a database missing two is told about both."""
    async def fake_get(user, table, params=None):
        if (params or {}).get("select") in ("cartons_returned", "consignment_id"):
            raise _Boom("404")
        return []
    monkeypatch.setattr(main, "db_get", fake_get)
    assert asyncio.run(main.schema_gaps(USER)) == [
        "0012_statements_returns.sql", "0010_statements_consignment_id.sql"]


def test_schema_gaps_is_silent_on_a_database_that_is_up_to_date(monkeypatch):
    async def fake_get(user, table, params=None):
        return []
    monkeypatch.setattr(main, "db_get", fake_get)
    assert asyncio.run(main.schema_gaps(USER)) == []


def test_schema_gaps_skipped_without_a_user():
    """Local development has no Supabase at all; probing it would warn about
    every migration on a machine that needs none of them."""
    assert asyncio.run(main.schema_gaps(None)) == []
