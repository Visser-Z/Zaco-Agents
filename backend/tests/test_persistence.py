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


# --- the grain of the book -------------------------------------------------

def test_a_consignment_selling_on_several_days_is_several_rows(monkeypatch):
    """Keyed on group_date, a week of reports dropped together kept only the
    first day of each consignment: apply_group_dates collapses group_date to
    one value per consignment across a batch, so every later day collided and
    the ignore-duplicates insert dropped it without a word. R78 591,95 of one
    real month went that way. The key is the day it SOLD."""
    import asyncio
    from datetime import date

    import app.main as main
    from app.schemas import StatementRow
    from app.supabase_auth import User

    sent = {}

    async def fake_post(user, table, records, **kw):
        sent["on_conflict"] = kw.get("on_conflict")
        sent["records"] = records
        return records

    monkeypatch.setattr(main, "db_post", fake_post)
    monkeypatch.setattr(main, "remember_delivery_notes",
                        lambda *a, **k: asyncio.sleep(0))

    # One consignment, one collapsed group_date, four different selling days.
    rows = [
        StatementRow(source_file="week.pdf", market_agent="Growfresh Port Natal",
                     stm_no=185549102, consignment_id=185549102,
                     date=date(2026, 9, 3), last_sale=date(2026, 9, day))
        for day in (3, 4, 5, 7)
    ]
    asyncio.run(main.persist_statements(User(id="u", email="o@e.com", token="t"), rows))

    assert sent["on_conflict"] == "market_agent,stm_no,consignment_id,sale_day"
    # Every row still carries its own selling day for the key to separate them.
    assert [r["last_sale"] for r in sent["records"]] == [
        "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-07"]
