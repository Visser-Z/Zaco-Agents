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
    """Just enough PostgREST: filtered reads, filtered deletes, keyed upserts."""

    def __init__(self, statements=(), payments=(), dismissals=()):
        self.rows = {"statements": list(statements), "payments": list(payments),
                     "dismissals": list(dismissals)}
        self.posts = []

    @staticmethod
    def _window(row, params):
        """`and=(col.gte.X,col.lte.Y)` -- whichever column it names."""
        window = params.get("and") or ""
        if ".gte." not in window:
            return True
        column = window.lstrip("(").split(".gte.")[0]
        lo = window.split(".gte.")[1].split(",")[0]
        hi = window.split(".lte.")[1].rstrip(")")
        value = row.get(column)
        return value is not None and lo <= str(value) <= hi

    @staticmethod
    def _op(row, column, expr):
        """The operators these endpoints use, on one column."""
        value = row.get(column)
        if expr == "is.null":
            return value is None
        if expr == "not.is.null":
            return value is not None
        if expr.startswith("in.("):
            return str(value) in {x for x in expr[4:].rstrip(")").split(",") if x}
        if expr.startswith("eq."):
            return str(value) == expr[3:]
        if expr.startswith("gt."):
            try:
                return value is not None and float(value) > float(expr[3:])
            except (TypeError, ValueError):
                return False
        raise AssertionError(f"stub does not model filter {expr!r}")

    @classmethod
    def _selects(cls, row, params):
        """Every filter ANDed together, as PostgREST does."""
        for key, expr in params.items():
            if key in ("select", "limit", "and", "order"):
                continue
            if not cls._op(row, key, expr):
                return False
        return cls._window(row, params)

    async def get(self, user, table, params):
        # Deep copies, as a real read does: _saved_payments renames paid_on to
        # date on the rows it hands back, and sharing the dicts would rewrite
        # the stored table.
        return [dict(r) for r in self.rows.get(table, []) if self._selects(r, params)]

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
            (gone if self._selects(r, params) else keep).append(r)
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

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
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

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
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
    asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
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

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert out["deleted"] == 1
    assert out["payments_deleted"] == 0
    assert out["payments_remaining"] == 1      # said out loud, not swallowed


def test_a_clean_delete_reports_nothing_left_behind(monkeypatch):
    db = _DB([_sale("2026-08-01")], [_payment("2026-08-05")], [])
    _wire(monkeypatch, db)
    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert (out["remaining"], out["payments_remaining"]) == (0, 0)


def test_a_payment_received_in_another_month_goes_with_its_sales(monkeypatch):
    """An August sale settled in September carries a September paid_on, so a
    delete scoped to August never touched it. Tracking then reported it as
    money paid with nothing left to have paid for -- for ever."""
    db = _DB([_sale("2026-08-01")], [_payment("2026-09-03")], [])
    _wire(monkeypatch, db)

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert out["deleted"] == 1
    assert out["payments_deleted"] == 1, "the orphaned September payment must go too"
    assert db.rows["payments"] == []

    after = _track()
    assert after["payments"]["total_paid"] == 0
    assert after["payments"]["unmatched"] == []


def test_a_payment_still_settling_a_surviving_sale_is_kept(monkeypatch):
    """Only payments matching nothing are cleared. One that still settles a sale
    in another period must survive, or deleting August would strip July's."""
    july, august = _sale("2026-07-02"), _sale("2026-08-01")
    db = _DB([july, august], [_payment("2026-09-03")], [])
    _wire(monkeypatch, db)

    asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert len(db.rows["payments"]) == 1, "July's sale still needs its payment"
    assert [r["group_date"] for r in db.rows["statements"]] == ["2026-07-02"]


def test_clearing_the_whole_book_empties_all_three_tables(monkeypatch):
    db = _DB([_sale("2026-07-02"), _sale("2026-08-01")],
             [_payment("2026-08-05")], [{"kind": "slow", "ref": "14588:GRAPES"}])
    _wire(monkeypatch, db)

    out = asyncio.run(main.delete_history(scope="all", month=None, week=None, user=USER))
    assert out["scope"] == "all"
    assert (out["deleted"], out["payments_deleted"], out["closed_cleared"]) == (2, 1, 1)
    assert (out["remaining"], out["payments_remaining"]) == (0, 0)
    assert db.rows == {"statements": [], "payments": [], "dismissals": []}

    after = _track()
    assert after["sales_by_day"]["days"] == []
    assert after["payments"]["outstanding"] == []
    assert after["payments"]["total_paid"] == 0


def test_a_blank_period_is_still_an_error_not_a_delete_all(monkeypatch):
    """The only way to empty the book is to ask for it by name. A missing
    filter must never become one."""
    db = _DB([_sale("2026-08-01")], [], [])
    _wire(monkeypatch, db)
    try:
        asyncio.run(main.delete_history(scope=None, month=None, week=None, user=USER))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("a blank period must be refused")
    assert db.rows["statements"], "nothing may have been deleted"


def test_a_sale_off_a_load_sent_the_previous_month_is_deleted_with_its_own_month(monkeypatch):
    """A row's period is the day it SOLD. group_date is the day its load was
    sent, and a load sent on 31 July sells into August, so a delete written on
    group_date alone left August rows behind that August had just counted."""
    august_sale = {**_sale("2026-07-31"), "last_sale": "2026-08-03"}
    july_sale = {**_sale("2026-07-20"), "last_sale": "2026-07-20"}
    db = _DB([august_sale, july_sale], [], [])
    _wire(monkeypatch, db)

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert out["deleted"] == 1, "the August sale must go even though its load left in July"
    assert out["remaining"] == 0
    assert [r["last_sale"] for r in db.rows["statements"]] == ["2026-07-20"]


def test_a_row_with_no_sale_date_falls_back_down_the_chain(monkeypatch):
    """History recorded before the sale date was captured is still placed, by
    the consignment date and then the invoice date, and must delete with it."""
    by_group = {k: v for k, v in _sale("2026-08-02").items()}
    by_group["last_sale"] = None
    by_invoice = {k: v for k, v in _sale("2026-08-02").items()}
    by_invoice.update(last_sale=None, group_date=None, invoice_date="2026-08-05")
    other = {**_sale("2026-09-01"), "last_sale": "2026-09-01"}
    db = _DB([by_group, by_invoice, other], [], [])
    _wire(monkeypatch, db)

    out = asyncio.run(main.delete_history(scope=None, month="2026-08", week=None, user=USER))
    assert out["deleted"] == 2
    assert out["remaining"] == 0
    assert len(db.rows["statements"]) == 1
