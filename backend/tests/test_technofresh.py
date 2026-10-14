"""The TechnoFresh auto-pull: which days it pulls, what it saves, what it holds.

Nothing here reaches the portal or Supabase: the portal is an httpx mock and
the database calls are replaced with recorders.
"""

import asyncio
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet

import app.main as main
from app import technofresh
from app.schemas import Flag, StatementRow
from app.supabase_auth import User

USER = User(id="u-1", email="robot@example.com", token="tok")
TODAY = date(2026, 10, 8)          # a Thursday


def _held(report, day, status):
    return {"report": report, "day": day, "status": status}


# --- which days ---------------------------------------------------------------

def test_first_run_pulls_only_yesterday():
    assert technofresh.days_to_pull("payments", [], TODAY) == [date(2026, 10, 7)]


def test_missed_days_are_caught_up_one_by_one():
    held = [_held("payments", "2026-10-04", "done")]
    assert technofresh.days_to_pull("payments", held, TODAY) == [
        date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]


def test_a_day_already_done_is_not_pulled_again():
    held = [_held("payments", "2026-10-07", "done")]
    assert technofresh.days_to_pull("payments", held, TODAY) == []


def test_sales_still_unpaid_are_pulled_again_until_paid():
    held = [_held("sales", "2026-10-05", "waiting"), _held("sales", "2026-10-06", "done"),
            _held("sales", "2026-10-07", "waiting")]
    assert technofresh.days_to_pull("sales", held, TODAY) == [date(2026, 10, 5), date(2026, 10, 7)]


def test_waiting_days_stop_being_pulled_after_two_weeks():
    held = [_held("sales", "2026-09-20", "waiting"), _held("sales", "2026-10-07", "done")]
    assert technofresh.days_to_pull("sales", held, TODAY) == []


def test_a_failed_day_is_retried_even_after_later_days_worked():
    held = [_held("payments", "2026-10-05", "failed"), _held("payments", "2026-10-06", "done"),
            _held("payments", "2026-10-07", "done")]
    assert technofresh.days_to_pull("payments", held, TODAY) == [date(2026, 10, 5)]


def test_a_long_gap_is_capped():
    held = [_held("payments", "2026-01-01", "done")]
    days = technofresh.days_to_pull("payments", held, TODAY)
    assert len(days) == technofresh.CATCH_UP_LIMIT and days[-1] == date(2026, 10, 7)


def test_today_is_never_pulled_by_default():
    # The market posts into the evening, so a day is only read once it is over.
    assert technofresh.days_to_pull("sales", [_held("sales", "2026-10-07", "done")], TODAY) == []


def test_a_day_pulled_on_its_own_date_is_pulled_once_more():
    # 14 October was read at 18:00 on the 14th, before the market had finished.
    same = {**_held("sales", "2026-10-07", "done"), "pulled_on": "2026-10-07"}
    assert technofresh.days_to_pull("sales", [same], TODAY) == [date(2026, 10, 7)]
    later = {**_held("sales", "2026-10-07", "done"), "pulled_on": "2026-10-08"}
    assert technofresh.days_to_pull("sales", [later], TODAY) == []


def test_weekly_runs_on_mondays_only():
    assert technofresh.due_today("daily", TODAY)
    assert not technofresh.due_today("weekly", TODAY)
    assert technofresh.due_today("weekly", date(2026, 10, 12))


# --- the stored password --------------------------------------------------------

def test_password_round_trips_and_is_not_stored_plain(monkeypatch):
    monkeypatch.setenv("TECHNOFRESH_KEY", Fernet.generate_key().decode())
    token = technofresh.encrypt("hunter2")
    assert "hunter2" not in token
    assert technofresh.decrypt(token) == "hunter2"


def test_no_key_says_so(monkeypatch):
    monkeypatch.delenv("TECHNOFRESH_KEY", raising=False)
    with pytest.raises(technofresh.PortalError, match="TECHNOFRESH_KEY"):
        technofresh.encrypt("x")


def test_a_password_from_another_key_says_so(monkeypatch):
    monkeypatch.setenv("TECHNOFRESH_KEY", Fernet.generate_key().decode())
    token = technofresh.encrypt("x")
    monkeypatch.setenv("TECHNOFRESH_KEY", Fernet.generate_key().decode())
    with pytest.raises(technofresh.PortalError, match="Enter it again"):
        technofresh.decrypt(token)


# --- the portal -------------------------------------------------------------------

PDF = b"%PDF-1.4\n% a pulled report\n"


def _portal(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(technofresh.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(technofresh, "PACE_SECONDS", 0)


def test_wrong_password_is_reported(monkeypatch):
    def handler(req):
        if req.url.path == "/reports/":
            return httpx.Response(302, headers={"location": "/user/login"})
        return httpx.Response(200, text="<html>login</html>")
    _portal(monkeypatch, handler)
    with pytest.raises(technofresh.PortalError, match="did not accept"):
        technofresh.Portal("me", "wrong")


def test_login_posts_the_form_and_fetches_one_day(monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.content.decode()))
        if req.url.path == "/reports/view/8/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF)
        return httpx.Response(200, text="<html>ok</html>")
    _portal(monkeypatch, handler)
    with technofresh.Portal("me", "pw") as portal:
        data = portal.fetch(technofresh.SALES, date(2026, 10, 7))
    assert data == PDF
    login = next(s for s in seen if s[0] == "POST" and s[1] == "/user/login")
    assert "username=me" in login[2] and "password=pw" in login[2]
    report = next(s for s in seen if s[1] == "/reports/view/8/pdf")
    assert "from_date=2026-10-07" in report[2] and "to_date=2026-10-07" in report[2]


def test_an_html_page_is_never_taken_for_a_report(monkeypatch):
    def handler(req):
        if req.url.path == "/reports/view/24/pdf":
            return httpx.Response(200, text="<!DOCTYPE html><html>error</html>")
        return httpx.Response(200, text="<html>ok</html>")
    _portal(monkeypatch, handler)
    with technofresh.Portal("me", "pw") as portal:
        with pytest.raises(technofresh.PortalError, match="web page"):
            portal.fetch(technofresh.PAYMENTS, date(2026, 10, 7))


# --- what a pulled sales day saves --------------------------------------------------

def _row(stm, **kw):
    return StatementRow(source_file="x", market_agent="Farmers Trust", stm_no=stm,
                        consignment_id=stm, product="PLUMS", description="Plums", **kw)


def test_sales_day_saves_clean_rows_holds_the_rest(monkeypatch):
    rows = [
        _row(1),                                                         # clean
        _row(2, flags=[Flag(field="description", message="No short code")]),  # held
        _row(None, flags=[Flag(field="stm_no", severity="warning", code="unpaid",
                               message="unpaid")]),                      # unpaid
        _row(4, flags=[Flag(field="stm_no", severity="warning", code="duplicate",
                            message="on book")]),                        # on the book
    ]

    async def fake_read(user, files, known_dns=None):
        return main.ExtractResponse(rows=rows)

    saved = []

    async def fake_persist(user, payload):
        saved.extend(payload)

    async def fake_notes(user, payload):
        pass

    async def fake_pages(report, data):
        return []

    monkeypatch.setattr(main, "_tf_pages", fake_pages)
    monkeypatch.setattr(main, "read_sales_files", fake_read)
    monkeypatch.setattr(main, "persist_statements", fake_persist)
    monkeypatch.setattr(main, "remember_delivery_notes", fake_notes)
    out = asyncio.run(main._tf_sales_day(USER, date(2026, 10, 7), PDF))
    assert [r.stm_no for r in saved] == [1]
    assert out["status"] == "waiting"           # pulled again for the unpaid one
    assert (out["found"], out["saved"], out["held"], out["unpaid"]) == (4, 1, 1, 1)


def test_a_row_saved_before_its_day_was_over_is_replaced(monkeypatch):
    """14 October 2026 was saved at 18:36 that day at R 94 630; the market's
    final report said R 141 320. The next pull replaces such a row, and only
    such a row: one saved after its day was over is kept as saved."""
    from datetime import date as d
    differs = lambda: [Flag(field="stm_no", severity="warning", code="duplicate_differs",
                            message="differs")]
    early_row = _row(7, last_sale=d(2026, 10, 14), cartons_sold=166, sales_total=73040.0,
                     flags=differs())
    late_row = _row(8, last_sale=d(2026, 10, 14), cartons_sold=10, sales_total=900.0,
                    flags=differs())

    async def fake_read(user, files, known_dns=None):
        return main.ExtractResponse(rows=[early_row, late_row])

    async def fake_get(user, path, params=None):
        return [
            # Saved at 18:36 South African time on the 14th: before the day was over.
            {"id": 101, "consignment_id": 7, "sale_day": "2026-10-14", "created_at": "2026-10-14T16:36:00+00:00"},
            # Saved on the morning of the 15th: the final word.
            {"id": 102, "consignment_id": 8, "sale_day": "2026-10-14", "created_at": "2026-10-15T06:54:00+00:00"},
        ]

    deleted, saved = [], []

    async def fake_delete(user, path, params):
        deleted.append(params)
        return []

    async def fake_persist(user, payload):
        saved.extend(payload)

    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(main, "_tf_pages", nothing)
    monkeypatch.setattr(main, "read_sales_files", fake_read)
    monkeypatch.setattr(main, "db_get", fake_get)
    monkeypatch.setattr(main, "db_delete", fake_delete)
    monkeypatch.setattr(main, "persist_statements", fake_persist)
    monkeypatch.setattr(main, "remember_delivery_notes", nothing)
    out = asyncio.run(main._tf_sales_day(USER, d(2026, 10, 14), PDF))
    assert deleted == [{"id": "in.(101)"}]
    assert [r.stm_no for r in saved] == [7]
    assert "1 row saved before the day was over replaced" in out["message"]
    assert "1 already on the book" in out["message"]


def test_a_pdf_of_the_wrong_report_is_refused(monkeypatch):
    monkeypatch.setattr(main, "pdf_to_page_texts", lambda data: ["Some other report entirely"])
    with pytest.raises(technofresh.PortalError, match="not that report"):
        asyncio.run(main._tf_pages(technofresh.SALES, PDF))


def test_cron_refuses_without_the_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    with pytest.raises(main.HTTPException):
        asyncio.run(main.technofresh_cron(authorization="Bearer wrong"))
    monkeypatch.delenv("CRON_SECRET")
    with pytest.raises(main.HTTPException):
        asyncio.run(main.technofresh_cron(authorization="Bearer "))
