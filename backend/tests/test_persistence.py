"""Tests for mapping reviewed rows onto `statements` inserts.

The mapping is the data-integrity boundary between the app and the history
table: a row that can't be keyed must be skipped, dates must be ISO strings,
and the gross-value inputs must survive intact.
"""

import asyncio
from datetime import date

from app.main import _statement_record, _iso, persist_statements
from app.schemas import StatementRow
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


def _row(**kw) -> StatementRow:
    base = dict(
        source_file="Day report.pdf",
        market_agent="Farmers Trust",
        market="TSHWANE MARKET",
        stm_no=118170501,
        dn=1181705,
        product="NECTARINES OTHER",
        description="IMP Nect",
        cartons_sold=1,
        price=50.0,
        nett_total=None,
        date_received=date(2026, 7, 27),
        invoice_date=date(2026, 7, 27),
        date=date(2026, 7, 27),
        status="27.07",
    )
    base.update(kw)
    return StatementRow(**base)


def test_record_maps_all_fields_with_iso_dates():
    rec = _statement_record(_row(), USER)
    assert rec["market_agent"] == "Farmers Trust"
    assert rec["market"] == "TSHWANE MARKET"
    assert rec["stm_no"] == 118170501
    assert rec["description"] == "IMP Nect"
    assert rec["nett_total"] is None            # blank on this format, kept null
    assert rec["group_date"] == "2026-07-27"    # StatementRow.date -> group_date
    assert rec["date_received"] == "2026-07-27"
    assert rec["created_by"] == "u-1"


def test_iso_handles_none():
    assert _iso(None) is None
    assert _iso(date(2026, 8, 4)) == "2026-08-04"


def test_row_without_agent_or_stm_no_is_skipped():
    # The table's unique key is (market_agent, stm_no); a row missing either
    # cannot be recorded without corrupting the history, so it is dropped.
    assert _statement_record(_row(market_agent=None), USER) is None
    assert _statement_record(_row(stm_no=None), USER) is None


def test_persist_skipped_without_user():
    # No signed-in user (local dev, no Supabase) -> no write attempted, no error.
    assert asyncio.run(persist_statements(None, [_row()])) is None
