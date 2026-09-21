"""Dropping the same report twice, or overlapping reports, saves each thing once.

The operator drops a day's payment report and later the week's, which carries
the same payments again; or drops one file twice by mistake. Neither may save a
payment twice, fail with an error, or keep a copy that was read wrongly.
"""

import asyncio
from datetime import date

import app.main as main
from app import payment_details as pdd
from app.schemas import StatementRow
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


def _pay(acc, gross, lines, fms="743012", **kw):
    return {"accsale": acc, "fms_id": fms, "dn": 4979691, "supplier_ref": "20026*4979691",
            "stm_no": 1, "market_agent": "Grow Port Natal", "date": "2026-09-02",
            "nett": gross * 0.85, "gross": gross, "deductions": 0.0, "vat": 0.0,
            "lines": lines, **kw}


def _line(total, line_no=1, product="GRAPES SUGRAONE CLASS 1"):
    return {"line_no": line_no, "product": product, "delivered": 360, "sold": 10,
            "sales_total": total}


def test_the_same_payment_in_two_files_is_one_payment():
    day = _pay("DUR*13*201869", 31160.0, [_line(31160.0)])
    week = _pay("DUR*13*201869", 31160.0, [_line(31160.0)])
    other = _pay("DUR*13*202568", 126580.0, [_line(71100.0), _line(55480.0, 2)], fms="743396")
    out = pdd.dedupe([day, other, week])
    assert [r["accsale"] for r in out] == ["DUR*13*201869", "DUR*13*202568"]


def test_the_copy_that_adds_up_wins_over_one_that_absorbed_lines():
    """The old parser saved DUR*13*201869 with its neighbour's two lines on it.
    Dropped again, the clean read must replace it, whichever arrives first."""
    absorbed = _pay("DUR*13*201869", 31160.0,
                    [_line(31160.0), _line(71100.0, 1), _line(55480.0, 2)])
    clean = _pay("DUR*13*201869", 31160.0, [_line(31160.0)])
    assert pdd.dedupe([absorbed, clean])[0]["lines"] == clean["lines"]
    assert pdd.dedupe([clean, absorbed])[0]["lines"] == clean["lines"]


def test_saving_refreshes_a_payment_already_held(monkeypatch):
    posted = []

    async def fake_post(user, path, payload, upsert=False, on_conflict=None, resolution=None):
        posted.append((path, payload, on_conflict, resolution))
        return []
    monkeypatch.setattr(main, "db_post", fake_post)

    same = _pay("DUR*13*201869", 31160.0, [_line(31160.0)])
    assert asyncio.run(main.persist_payments(USER, [same, dict(same)])) is None
    [(path, payload, on_conflict, resolution)] = posted
    assert (path, on_conflict, resolution) == ("payments", "accsale", "merge-duplicates")
    assert [r["accsale"] for r in payload] == ["DUR*13*201869"]
    assert payload[0]["fms_id"] == "743012"


def test_a_payment_still_saves_before_the_fms_migration(monkeypatch):
    """Without migration 0021 the fms_id column does not exist. The payment is
    saved without it rather than not at all."""
    calls = []

    async def fake_post(user, path, payload, upsert=False, on_conflict=None, resolution=None):
        calls.append(payload)
        if any("fms_id" in r for r in payload):
            raise main.HTTPException(400, 'column "fms_id" of relation "payments" does not exist')
        return []
    monkeypatch.setattr(main, "db_post", fake_post)

    assert asyncio.run(main.persist_payments(USER, [_pay("A*B*1", 100.0, [_line(100.0)])])) is None
    assert len(calls) == 2 and "fms_id" not in calls[1][0]


def _row(day, total, **kw):
    base = dict(source_file="dailysales.pdf", market_agent="Grow Port Natal",
                market="DURBAN MARKET", stm_no=185549102, consignment_id=185549102,
                dn=1855491, product="GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)",
                cartons_sold=10, price=380.0, sales_total=total, qty_received=480,
                qty_amended=360, qty_avail=0, last_sale=day, date=date(2026, 9, 15),
                date_received=date(2026, 9, 15), invoice_date=day)
    base.update(kw)
    return StatementRow(**base)


def test_a_day_already_on_the_book_is_refreshed_not_saved_twice(monkeypatch):
    """A week's report dropped over days already loaded from their own reports
    carries those days again. In one write they are one row each."""
    posted = []

    async def fake_post(user, path, payload, upsert=False, on_conflict=None, resolution=None):
        posted.append((payload, resolution))
        return []
    monkeypatch.setattr(main, "db_post", fake_post)

    rows = [_row(date(2026, 9, 17), 12070.0), _row(date(2026, 9, 18), 9480.0),
            _row(date(2026, 9, 17), 12070.0, qty_avail=0, source_file="week.pdf")]
    assert asyncio.run(main.persist_statements(USER, rows)) is None
    [(payload, resolution)] = posted
    assert resolution == "merge-duplicates"
    assert sorted(r["last_sale"] for r in payload) == ["2026-09-17", "2026-09-18"]
    # The later copy is the one kept.
    assert next(r for r in payload if r["last_sale"] == "2026-09-17")["source_file"] == "week.pdf"
    assert payload[0]["qty_amended"] == 360 and payload[0]["qty_avail"] == 0


def test_sales_still_save_before_the_stock_migration(monkeypatch):
    calls = []

    async def fake_post(user, path, payload, upsert=False, on_conflict=None, resolution=None):
        calls.append(payload)
        if any("qty_amended" in r for r in payload):
            raise main.HTTPException(400, 'column "qty_amended" of relation "statements" does not exist')
        return []
    monkeypatch.setattr(main, "db_post", fake_post)

    assert asyncio.run(main.persist_statements(USER, [_row(date(2026, 9, 17), 12070.0)])) is None
    assert "qty_amended" not in calls[-1][0] and "qty_avail" not in calls[-1][0]
