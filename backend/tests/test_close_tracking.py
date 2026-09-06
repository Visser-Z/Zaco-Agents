"""Closing a Tracking line, end to end through the endpoint.

Migration 0016 adds a `dismissals` table keyed on (kind, ref). These tests
drive the real handlers against a stub that answers the way PostgREST does,
so the record shape, the conflict key and the delete filter are all exercised
-- the parts a pure tracking.compute test never reaches.
"""

import asyncio

import app.main as main
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


class _DB:
    """Just enough PostgREST: a table of rows, upsert by key, filtered delete."""

    def __init__(self, statements=(), payments=()):
        self.rows = {"statements": list(statements), "payments": list(payments),
                     "dismissals": []}
        self.posts = []

    async def get(self, user, table, params):
        return list(self.rows.get(table, []))

    async def post(self, user, table, records, **kw):
        self.posts.append({"table": table, "records": records, **kw})
        for r in records:
            key = (r.get("kind"), r.get("ref"))
            if not any((x.get("kind"), x.get("ref")) == key for x in self.rows[table]):
                self.rows[table].append(r)
        return records

    async def delete(self, user, table, params):
        keep, gone = [], []
        for r in self.rows[table]:
            hit = all(str(params.get(k, "")) == f"eq.{r.get(k)}" for k in ("kind", "ref"))
            (gone if hit else keep).append(r)
        self.rows[table] = keep
        return gone


def _wire(monkeypatch, db):
    monkeypatch.setattr(main, "db_get", db.get)
    monkeypatch.setattr(main, "db_post", db.post)
    monkeypatch.setattr(main, "db_delete", db.delete)


def _sale(dn, product, cartons, price, day, sent):
    return {"market_agent": "Farmers Trust", "dn": dn, "supplier_ref": dn,
            "product": product, "description": None, "cartons_sold": cartons,
            "price": price, "sales_total": cartons * price, "qty_received": sent,
            "group_date": day, "date_received": day, "last_sale": day,
            "stm_no": 1, "consignment_id": 1, "nett_total": None, "payment_refs": None}


def _track():
    """get_tracking as the router calls it: FastAPI resolves the Query defaults
    at request time, so they have to be supplied when calling it directly."""
    return asyncio.run(main.get_tracking(date_from=None, date_to=None, month=None,
                                         week=None, user=USER))


def test_closing_a_line_writes_the_record_and_takes_it_off_the_list(monkeypatch):
    db = _DB(statements=[_sale(14588, "GRAPES", 10, 100.0, "2026-08-01", 600)])
    _wire(monkeypatch, db)

    before = _track()
    owed = before["payments"]["outstanding"]
    assert len(owed) == 1, owed
    ref = owed[0]["ref"]

    asyncio.run(main.close_tracking_item(kind="owed", ref=ref, note=None, user=USER))

    post = db.posts[-1]
    assert post["table"] == "dismissals"
    assert post["on_conflict"] == "kind,ref"          # the table's primary key
    assert post["records"] == [
        {"kind": "owed", "ref": ref, "note": None, "created_by": "u-1"}]

    after = _track()
    assert after["payments"]["outstanding"] == []
    # the money is still reported, never silently dropped
    assert after["payments"]["closed_value"] == before["payments"]["still_to_come"]
    assert [r["ref"] for r in after["payments"]["closed"]] == [ref]


def test_reopening_puts_it_back(monkeypatch):
    db = _DB(statements=[_sale(14588, "GRAPES", 10, 100.0, "2026-08-01", 600)])
    _wire(monkeypatch, db)
    ref = _track()["payments"]["outstanding"][0]["ref"]

    asyncio.run(main.close_tracking_item(kind="owed", ref=ref, note=None, user=USER))
    assert _track()["payments"]["outstanding"] == []

    asyncio.run(main.reopen_tracking_item(kind="owed", ref=ref, user=USER))
    back = _track()["payments"]
    assert [r["ref"] for r in back["outstanding"]] == [ref]
    assert back["closed"] == []


def test_a_slow_line_and_an_owed_line_close_independently(monkeypatch):
    """Both kinds can name the same consignment, so the kind has to be part of
    the key or closing one would close the other."""
    db = _DB(statements=[_sale(14588, "GRAPES", 10, 100.0, "2026-08-01", 600)])
    _wire(monkeypatch, db)
    t = _track()
    owed_ref = t["payments"]["outstanding"][0]["ref"]
    slow_ref = t["slow_stock"]["items"][0]["ref"]
    assert owed_ref == slow_ref                       # same consignment, same ref

    asyncio.run(main.close_tracking_item(kind="slow", ref=slow_ref, note=None, user=USER))
    after = _track()
    assert after["slow_stock"]["flagged"] == 0        # the slow line closed
    assert len(after["payments"]["outstanding"]) == 1  # the owed line did not


def test_an_unknown_kind_is_refused(monkeypatch):
    db = _DB()
    _wire(monkeypatch, db)
    try:
        asyncio.run(main.close_tracking_item(kind="whatever", ref="x", note=None, user=USER))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("an unknown kind should be refused")
    assert db.posts == []
