"""Deleting a period must take Tracking's view of it with it.

Insights reads the statements. Tracking reads the statements, the recorded
payments and the closed-item records. Deleting only the statements left
Tracking still reporting the period: outstanding money against sales that no
longer existed, and lines still marked closed.
"""

import asyncio

import app.main as main
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")


class _DB:
    """A PostgREST-shaped stub: filtered reads and filtered deletes."""

    def __init__(self, statements, payments, dismissals):
        self.rows = {"statements": list(statements), "payments": list(payments),
                     "dismissals": list(dismissals)}

    @staticmethod
    def _between(value, params, column):
        window = params.get("and") or ""
        if f"{column}.gte." not in window:
            return True
        lo = window.split(f"{column}.gte.")[1].split(",")[0]
        hi = window.split(f"{column}.lte.")[1].rstrip(")")
        return value is not None and lo <= str(value) <= hi

    async def get(self, user, table, params):
        # Deep copies, as a real read does: _saved_payments renames paid_on to
        # date on the rows it hands back, and sharing the dicts would rewrite
        # the stored table.
        rows = self.rows.get(table, [])
        if table == "statements":
            if params.get("group_date") == "is.null":
                rows = [r for r in rows if r.get("group_date") is None]
            elif params.get("and"):
                rows = [r for r in rows if self._between(r.get("group_date"), params, "group_date")]
        return [dict(r) for r in rows]

    async def delete(self, user, table, params):
        keep, gone = [], []
        ids = None
        if "id" in params:                       # id=in.(1,2,3)
            ids = {x for x in params["id"].removeprefix("in.(").rstrip(")").split(",") if x}
        column = "group_date" if table == "statements" else "paid_on"
        for r in self.rows[table]:
            if ids is not None:
                hit = str(r.get("id")) in ids
            elif table == "dismissals":
                hit = all(str(params.get(k, "")) == f"eq.{r.get(k)}" for k in ("kind", "ref"))
            else:
                hit = self._between(r.get(column), params, column)
            (gone if hit else keep).append(r)
        self.rows[table] = keep
        return gone


def _wire(monkeypatch, db):
    monkeypatch.setattr(main, "db_get", db.get)
    monkeypatch.setattr(main, "db_delete", db.delete)


_next_id = [0]


def _sale(day):
    _next_id[0] += 1
    return {"id": _next_id[0],
            "market_agent": "Farmers Trust", "dn": 14588, "supplier_ref": 14588,
            "product": "GRAPES", "description": None, "cartons_sold": 10, "price": 100.0,
            "sales_total": 1000.0, "qty_received": 600, "group_date": day,
            "date_received": day, "last_sale": day, "stm_no": 1, "consignment_id": 1,
            "nett_total": None, "payment_refs": None}


def _payment(day):
    return {"accsale": "PRE*BT*1", "stm_no": 1, "market_agent": "Farmers Trust",
            "supplier_ref": "14588", "dn": 14588, "paid_on": day, "nett": 900.0,
            "gross": 1000.0, "lines": [{"product": "GRAPES", "sales_total": 1000.0}]}


def _track():
    return asyncio.run(main.get_tracking(date_from=None, date_to=None, month=None,
                                         week=None, user=USER))


def test_deleting_a_month_clears_it_from_tracking_too(monkeypatch):
    db = _DB([_sale("2026-08-01")], [_payment("2026-08-05")],
             [{"kind": "slow", "ref": "14588:GRAPES"}])
    _wire(monkeypatch, db)

    before = _track()
    assert before["payments"]["total_paid"] == 900.0
    assert before["sales_by_day"]["days"]
    assert db.rows["payments"] and db.rows["dismissals"]

    out = asyncio.run(main.delete_history(month="2026-08", week=None, user=USER))
    assert out["deleted"] == 1
    assert out["payments_deleted"] == 1        # the payment went with the sales
    assert out["closed_cleared"] == 1          # the orphaned closed record went too

    after = _track()
    assert after["sales_by_day"]["days"] == []
    assert after["payments"]["outstanding"] == []
    assert after["payments"]["total_paid"] == 0
    assert after["payments"]["payments_recorded"] == 0
    assert after["slow_stock"]["items"] == []
    assert db.rows == {"statements": [], "payments": [], "dismissals": []}


def test_another_month_is_left_alone(monkeypatch):
    db = _DB([_sale("2026-08-01"), _sale("2026-07-02")],
             [_payment("2026-08-05"), _payment("2026-07-06")], [])
    _wire(monkeypatch, db)

    out = asyncio.run(main.delete_history(month="2026-08", week=None, user=USER))
    assert (out["deleted"], out["payments_deleted"]) == (1, 1)
    assert [r["group_date"] for r in db.rows["statements"]] == ["2026-07-02"]
    assert [r["paid_on"] for r in db.rows["payments"]] == ["2026-07-06"]


def test_a_closed_line_still_backed_by_history_is_kept(monkeypatch):
    """Only orphans are cleared. A dismissal whose line survives in another
    period must not be dropped, or closing it would be undone by an unrelated
    deletion."""
    db = _DB([_sale("2026-08-01"), _sale("2026-07-02")], [],
             [{"kind": "slow", "ref": "14588:GRAPES"}])
    _wire(monkeypatch, db)
    asyncio.run(main.delete_history(month="2026-08", week=None, user=USER))
    assert db.rows["dismissals"] == [{"kind": "slow", "ref": "14588:GRAPES"}]


def test_payments_that_survive_the_delete_are_reported(monkeypatch):
    """Row-level security refused the payments delete and answered 200 with an
    empty list, so the sales went and the money stayed. Tracking then reported
    the period from payments alone, which is what "I deleted it and can still
    see it" looked like."""
    db = _DB([_sale("2026-08-01")], [_payment("2026-08-05")], [])
    real_delete = db.delete

    async def refuse_payments(user, table, params):
        if table == "payments":
            return []                      # what an admin-only policy returns
        return await real_delete(user, table, params)

    monkeypatch.setattr(main, "db_get", db.get)
    monkeypatch.setattr(main, "db_delete", refuse_payments)

    out = asyncio.run(main.delete_history(month="2026-08", week=None, user=USER))
    assert out["deleted"] == 1
    assert out["payments_deleted"] == 0
    assert out["payments_remaining"] == 1      # said out loud, not swallowed


def test_a_clean_delete_reports_nothing_left_behind(monkeypatch):
    db = _DB([_sale("2026-08-01")], [_payment("2026-08-05")], [])
    _wire(monkeypatch, db)
    out = asyncio.run(main.delete_history(month="2026-08", week=None, user=USER))
    assert (out["remaining"], out["payments_remaining"]) == (0, 0)
