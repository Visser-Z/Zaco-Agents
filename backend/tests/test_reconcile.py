"""Reconciliation logic: match accumulated Daily Sales to Payment Details by
Supplier Ref + Product, and distribute the Nett back onto the daily rows."""

import asyncio
from datetime import date
from pathlib import Path

import app.main as main
from app import payment_details as pdd, reconcile
from app.supabase_auth import User

USER = User(id="u-1", email="op@example.com", token="tok")

PD = (Path(__file__).parent / "fixtures" / "payment_details_week32.txt").read_text(encoding="utf-8")
PAY = pdd.parse_payment_details([PD], "pd.pdf")   # 14585 nect 2720/2304.40, 14585 white 400/228.85, 15061 nect 840/688.67


def test_normalise_aligns_the_two_spellings():
    a = reconcile.normalise_product("NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg)")
    b = reconcile.normalise_product("NECTARINES OTHER CLASS 1 LARGE MULTI LAYER TRAYER 5.00 kg")
    assert a == b == "NECTARINES OTHER CLASS 1 LARGE MULTI LAYER TRAYER"
    # commodity with a comma (Daily Summary style) still aligns
    assert reconcile.normalise_product("GRAPES WHITE SEEDLESS, CLASS 2 NO SIZE (PUNNET 5kg)") \
        == reconcile.normalise_product("GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET 5.00 kg")


def _daily():
    return [
        # 14585 nectarines fully sold across TWO consignments -> should reconcile
        {"dn": 14585, "consignment": 1, "product": "NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg)", "sales_total": 2000.0},
        {"dn": 14585, "consignment": 2, "product": "NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg)", "sales_total": 720.0},
        # 14585 grapes white fully sold in one consignment
        {"dn": 14585, "consignment": 3, "product": "GRAPES WHITE SEEDLESS, CLASS 2 NO SIZE (PUNNET 5kg)", "sales_total": 400.0},
        # 15061 nectarines only partly sold so far (payment gross is 840)
        {"dn": 15061, "consignment": 4, "product": "NECTARINES OTHER CLASS 1 MEDIUM (MULTI LAYER TRAYER 11kg)", "sales_total": 500.0},
        # sold, but not in any payment run yet
        {"dn": 99999, "consignment": 5, "product": "PLUMS", "sales_total": 100.0},
    ]


def test_reconcile_statuses():
    by = {(r["dn"], r["product"].split()[0]): r for r in reconcile.reconcile(_daily(), PAY)}
    nect = next(r for r in reconcile.reconcile(_daily(), PAY) if r["dn"] == 14585 and "NECTARINES" in r["product"])
    assert nect["status"] == "matched"
    assert nect["daily_total"] == 2720.0 and nect["payment_gross"] == 2720.0
    assert nect["payment_nett"] == 2304.40

    partial = next(r for r in reconcile.reconcile(_daily(), PAY) if r["dn"] == 15061)
    assert partial["status"] == "outstanding"      # 500 sold of 840 paid

    unpaid = next(r for r in reconcile.reconcile(_daily(), PAY) if r["dn"] == 99999)
    assert unpaid["status"] == "unpaid"            # sold, no payment line


def test_fill_netts_apportions_across_consignments_of_a_matched_group():
    rows = _daily()
    filled = reconcile.fill_netts(rows, PAY)
    r1 = next(r for r in rows if r["consignment"] == 1)   # 2000 of 2720
    r2 = next(r for r in rows if r["consignment"] == 2)   # 720 of 2720
    r3 = next(r for r in rows if r["consignment"] == 3)   # grapes white, 400 of 400
    assert round(r1["nett_total"] + r2["nett_total"], 2) == 2304.40
    assert r1["nett_total"] == round(2304.40 * 2000 / 2720, 2)
    assert r3["nett_total"] == 228.85
    assert filled == 3                                     # the 3 fully-matched rows


def test_fill_skips_unreconciled_groups():
    rows = _daily()
    reconcile.fill_netts(rows, PAY)
    partial = next(r for r in rows if r["consignment"] == 4)   # 15061, only partly sold
    unpaid = next(r for r in rows if r["consignment"] == 5)
    assert "nett_total" not in partial
    assert "nett_total" not in unpaid


def test_normalise_drops_a_stray_trailing_number():
    """Rows saved before the parser fix still carry the bled-in number. The
    normaliser has to forgive it or they can never reconcile."""
    assert reconcile.normalise_product("GRAPES STARLIGHT CLASS 2 NO SIZE (PUNNET 5kg) 10") \
        == reconcile.normalise_product("GRAPES STARLIGHT CLASS 2 NO SIZE PUNNET 5.00 kg")
    # A trailing number that is not a stray column value stays put.
    assert reconcile.normalise_product("SOME PRODUCT CLASS 1").endswith("CLASS 1")


def test_unattributed_reports_payments_with_no_breakdown():
    records = [
        {"stm_no": 1, "gross": 100.0, "nett": 80.0, "lines": [{"product": "X", "sales_total": 100.0}]},
        {"stm_no": 388189, "gross": 8150.0, "nett": 6919.09, "lines": []},
    ]
    out = reconcile.unattributed(records)
    assert out == {"count": 1, "gross": 8150.0, "nett": 6919.09, "accsales": [388189]}


def test_payment_with_no_lines_contributes_nothing_to_a_group():
    # It has no product, so it cannot match -- but it must not corrupt or
    # inflate any other group either.
    records = [{"stm_no": 9, "dn": 14954, "gross": 8150.0, "nett": 6919.09, "lines": []}]
    daily = [{"dn": 14954, "product": "CHERRIES", "sales_total": 500.0}]
    result = reconcile.reconcile(daily, records)
    assert [r["status"] for r in result] == ["unpaid"]
    assert result[0]["payment_gross"] == 0.0


def test_one_payment_split_across_the_rows_it_settles():
    """An account sale settles every product on it with one Nett, and the rows
    take it in the ratio of their gross -- which is exactly what the operator's
    own book does: two product rows on one statement carry Netts in the ratio of
    their sales values, to six decimal places."""
    recs = [{"accsale": "A", "dn": 1, "gross": 3000.0, "nett": 2550.0, "lines": []}]
    rows = [
        {"dn": 1, "product": "PLUMS", "payment_refs": "A=1000.00"},
        {"dn": 1, "product": "PEARS", "payment_refs": "A=2000.00"},
    ]
    assert reconcile.fill_netts_by_reference(rows, recs) == 2
    assert [r["nett_total"] for r in rows] == [850.0, 1700.0]
    assert sum(r["nett_total"] for r in rows) == 2550.0


def test_the_split_lands_on_the_payment_to_the_cent():
    """Three equal rows do not divide into cents. The largest absorbs the
    remainder so the statement adds up, instead of missing by a cent and reading
    as a discrepancy."""
    recs = [{"accsale": "A", "dn": 1, "gross": 300.0, "nett": 100.0, "lines": []}]
    rows = [{"dn": 1, "payment_refs": "A=100.00"} for _ in range(3)]
    reconcile.fill_netts_by_reference(rows, recs)
    assert sum(r["nett_total"] for r in rows) == 100.0


def test_a_part_paid_reference_only_gives_out_the_share_it_covers():
    """The sales side holds two of the four products on this account sale, the
    others having sold outside the export. Those rows must take their share of
    the Nett, not the whole statement's."""
    recs = [{"accsale": "A", "dn": 1, "gross": 551.0, "nett": 445.30, "lines": []}]
    rows = [{"dn": 1, "payment_refs": "A=180.00"}, {"dn": 1, "payment_refs": "A=360.00"}]
    reconcile.fill_netts_by_reference(rows, recs)
    assert round(sum(r["nett_total"] for r in rows), 2) == 436.41   # 540 of 551


# --- endpoint helper: reading accumulated daily rows from the history --------

def test_accumulated_daily_maps_and_scopes_by_supplier_and_date(monkeypatch):
    captured = {}
    async def fake_db_get(user, path, params=None):
        captured["path"] = path
        captured["params"] = params
        return [{"supplier_ref": 14585, "product": "NECTARINES", "sales_total": 2720.0,
                 "stm_no": 118069401, "group_date": "2026-08-04"}]
    monkeypatch.setattr(main, "db_get", fake_db_get)

    rows = asyncio.run(main._accumulated_daily(USER, {14585}, "2026-08-01", "2026-08-05"))
    assert rows == [{"dn": 14585, "product": "NECTARINES", "sales_total": 2720.0,
                     "stm_no": 118069401, "payment_refs": None}]
    assert captured["path"] == "statements"
    assert captured["params"]["supplier_ref"] == "in.(14585)"
    # The window comes from PAYMENT dates, so it must reach back well before
    # them: the sales being settled happened earlier, sometimes weeks earlier.
    # Ending at `hi` is right; starting at `lo` would hide them.
    assert captured["params"]["and"].endswith("group_date.lte.2026-08-05)")
    start = captured["params"]["and"].split("group_date.gte.")[1].split(",")[0]
    assert start < "2026-08-01"
    assert (date.fromisoformat("2026-08-01") - date.fromisoformat(start)).days == main.LOOKBACK_DAYS


def test_accumulated_daily_no_suppliers_skips_query():
    # No supplier refs -> nothing to reconcile, no DB call.
    assert asyncio.run(main._accumulated_daily(USER, set(), "2026-08-01", "2026-08-05")) == []


# --- mixed histories ------------------------------------------------------

def _pdf_row(dn, product, total, stm):
    return {"dn": dn, "product": product, "sales_total": total,
            "stm_no": stm, "payment_refs": None}


def _pay(dn, accsale, product, gross, nett):
    return {"dn": dn, "accsale": accsale, "gross": gross, "nett": nett,
            "lines": [{"product": product, "sales_total": gross}]}


def test_one_referenced_row_does_not_strip_the_rest_of_the_history():
    """A single CSV-sourced row used to switch the whole run to reference
    matching, where PDF rows have nothing to match on. On a real August round
    that reported R107 580 of matched sales as R0."""
    rows = [_pdf_row(14584, "GRAPES CRIMSON SEEDLESS CLASS 2", 1000.0, 1),
            _pdf_row(14585, "GRAPES WHITE SEEDLESS CLASS 2", 400.0, 2)]
    pays = [_pay(14584, "PRE*BT*1", "GRAPES CRIMSON SEEDLESS CLASS 2", 1000.0, 850.0),
            _pay(14585, "PRE*BT*2", "GRAPES WHITE SEEDLESS CLASS 2", 400.0, 340.0)]
    clean = reconcile.reconcile_any(rows, pays)
    assert [r["status"] for r in clean] == ["matched", "matched"]

    rows[0] = dict(rows[0], payment_refs="PRE*BT*1=1000.00")
    mixed = reconcile.reconcile_any(rows, pays)
    assert sorted(r["status"] for r in mixed) == ["matched", "matched"]


def test_a_payment_claimed_by_reference_is_not_matched_again_by_product():
    """Otherwise the same money would be reported twice, once per strategy."""
    rows = [dict(_pdf_row(14584, "GRAPES", 1000.0, 1), payment_refs="PRE*BT*1=1000.00"),
            _pdf_row(14584, "GRAPES", 1000.0, 2)]
    pays = [_pay(14584, "PRE*BT*1", "GRAPES", 1000.0, 850.0)]
    out = reconcile.reconcile_any(rows, pays)
    assert sum(r["payment_gross"] for r in out) == 1000.0


def test_a_history_with_no_references_still_matches_on_product():
    rows = [_pdf_row(14584, "GRAPES", 1000.0, 1)]
    pays = [_pay(14584, "PRE*BT*1", "GRAPES", 1000.0, 850.0)]
    assert [r["status"] for r in reconcile.reconcile_any(rows, pays)] == ["matched"]


# --- rows without a supplier ref ------------------------------------------

def test_a_history_mixing_rows_with_and_without_a_ref_still_reconciles():
    """supplier_ref is nullable. Ordering the match keys compared None against
    an int and raised, which took the whole Tracking page down rather than
    losing one row. Found by fuzzing, not by a real file."""
    rows = [
        {"dn": 14588, "product": "GRAPES", "sales_total": 100.0},
        {"dn": None, "product": "PLUMS", "sales_total": 50.0},
        {"dn": 3, "product": "CHERRIES", "sales_total": 25.0},
    ]
    out = reconcile.reconcile(rows, [])
    assert [r["dn"] for r in out] == [3, 14588, None]      # missing ref sorts last
    assert {r["status"] for r in out} == {"unpaid"}
    assert sum(r["daily_total"] for r in out) == 175.0


def test_the_mixed_matcher_survives_it_too():
    """reconcile_any routes the ref-less rows through reconcile, so the same
    history must not raise there either."""
    rows = [
        {"dn": 14588, "product": "GRAPES", "sales_total": 100.0, "payment_refs": "A=100.00"},
        {"dn": None, "product": "PLUMS", "sales_total": 50.0, "payment_refs": None},
    ]
    out = reconcile.reconcile_any(rows, [])
    assert sum(r["daily_total"] for r in out) == 150.0
