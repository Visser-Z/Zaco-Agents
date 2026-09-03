"""Extraction tests.

The fixture is a transcription of statement 387517's layout. Once a real PDF is
available, regenerate it with `pdfplumber` (page.extract_text(layout=True)) and
these tests become the regression suite for the real thing.
"""

from datetime import date
from pathlib import Path

import pytest

from app.extraction import apply_group_dates, extract_from_text
from app.schemas import StatementRow

FIXTURE = Path(__file__).parent / "fixtures" / "statement_387517.txt"


@pytest.fixture
def row() -> StatementRow:
    return extract_from_text(FIXTURE.read_text(encoding="utf-8"), "AS_387517.pdf")[0]


def test_no_blocking_flags(row: StatementRow):
    assert [f.message for f in row.flags if f.severity == "error"] == []


def test_identifiers(row: StatementRow):
    # REFNO drives DN; ACCOUNT SALES NO drives STM No -- and must not pick up
    # the PREVIOUS ACCOUNT SALES NO (386759) sitting a few lines above.
    assert row.dn == 14847
    assert row.stm_no == 387517


def test_quantities(row: StatementRow):
    assert row.qty_received == 76      # QUANTITY RECEIVED
    assert row.opening_stock == 54     # QUANTITY B/F
    assert row.cartons_sold == 54      # price-table QUANTITY total


def test_money(row: StatementRow):
    assert row.price == pytest.approx(50.00)     # AVER.PRICE
    assert row.nett_total == pytest.approx(2238.01)  # NETT AMOUNT


def test_product_and_dates(row: StatementRow):
    assert row.product == "NEOT 1L MA50 36 T2 NECTARINE OTHER"
    assert row.date_received == date(2026, 6, 19)
    assert row.invoice_date == date(2026, 7, 1)
    assert row.status == "01.07"       # column T is DD.MM


def test_group_date_uses_earliest_in_dn_group(row: StatementRow):
    """Column D is the earliest DATE RECEIVED across the DN, not this row's own.

    Statement 387517 was received on 19/06, but it shares DN 14847 with a
    statement received on 18/06 -- so both rows must date to 18 June.
    """
    sibling = StatementRow(
        source_file="AS_386729.pdf", dn=14847, date_received=date(2026, 6, 18)
    )
    rows = [row, sibling]
    apply_group_dates(rows)
    assert row.date == date(2026, 6, 18)
    assert sibling.date == date(2026, 6, 18)


def test_group_dates_are_per_dn():
    a = StatementRow(source_file="a", dn=1, date_received=date(2026, 6, 10))
    b = StatementRow(source_file="b", dn=1, date_received=date(2026, 6, 12))
    c = StatementRow(source_file="c", dn=2, date_received=date(2026, 7, 1))
    apply_group_dates([a, b, c])
    assert a.date == b.date == date(2026, 6, 10)
    assert c.date == date(2026, 7, 1)


def test_gross_amount_cross_check_passes_on_real_statement(row: StatementRow):
    """54 cartons x 50.00 = 2700.00 = the statement's GROSS AMOUNT: no warning."""
    assert not any(f.field == "price" and "GROSS AMOUNT" in f.message for f in row.flags)


def test_gross_amount_mismatch_is_flagged():
    """If cartons x price disagrees with the printed GROSS AMOUNT, warn."""
    text = FIXTURE.read_text(encoding="utf-8").replace("GROSS AMOUNT 2700.00", "GROSS AMOUNT 9999.00")
    row = extract_from_text(text, "AS_bad.pdf")[0]
    warnings = [f for f in row.flags if "GROSS AMOUNT" in f.message]
    assert len(warnings) == 1
    assert warnings[0].severity == "warning"   # advisory: review, not blocked


# --- multi-statement PDFs -------------------------------------------------
# One PDF can bundle several account-sales statements (and one statement can
# span pages). Prepared ahead of a real multi-order sample: these tests build
# multi-page documents out of the real single-statement fixture.

from app.extraction import split_statements


def _second_statement() -> str:
    """A distinct statement forged from the real fixture's layout."""
    t = FIXTURE.read_text(encoding="utf-8")
    t = t.replace("387517", "388486").replace("BT387517", "BT388486")
    t = t.replace("NEOT 1L MA50 36 T2 NECTARINE OTHER", "GRAP 4.5KG RED GLOBE T2 GRAPE PINK")
    t = t.replace("DATE RECEIVED : 19/06/2026", "DATE RECEIVED : 21/06/2026")
    return t


def test_two_statements_in_one_pdf_yield_two_rows():
    pages = [FIXTURE.read_text(encoding="utf-8"), _second_statement()]
    blocks = split_statements(pages)
    assert len(blocks) == 2

    rows = [extract_from_text(b, f"bundle.pdf ({i})")[0] for i, b in enumerate(blocks)]
    assert rows[0].stm_no == 387517
    assert rows[0].product == "NEOT 1L MA50 36 T2 NECTARINE OTHER"
    assert rows[1].stm_no == 388486
    assert rows[1].product == "GRAP 4.5KG RED GLOBE T2 GRAPE PINK"
    assert rows[1].date_received == date(2026, 6, 21)
    # Both parse their own price table rather than bleeding into each other.
    assert rows[0].cartons_sold == rows[1].cartons_sold == 54


def test_statement_spanning_two_pages_stays_one_row():
    """Page 2 repeats the same ACCOUNT SALES NO: still one statement."""
    base = FIXTURE.read_text(encoding="utf-8")
    page2 = "    ACCOUNT SALES NO : 387517\n    (continuation)\n"
    assert len(split_statements([base, page2])) == 1


def test_continuation_page_without_header_attaches_to_previous():
    base = FIXTURE.read_text(encoding="utf-8")
    tail = "    carried forward totals ...\n"
    blocks = split_statements([base, tail, _second_statement()])
    assert len(blocks) == 2
    assert "carried forward" in blocks[0]


def test_cover_page_attaches_to_first_statement():
    blocks = split_statements(["    ZACO WEEKLY BUNDLE\n", FIXTURE.read_text(encoding="utf-8")])
    assert len(blocks) == 1
    assert "ACCOUNT SALES NO" in blocks[0]


def test_single_statement_pdf_still_one_row():
    assert len(split_statements([FIXTURE.read_text(encoding="utf-8")])) == 1


# --- multi-product statements (real fixture: statement 379971) ------------
# One statement, six MARKET GRN product sections, table 4 split over a page
# break, and a single statement-level NETT AMOUNT (10847.13).

FIXTURE_MULTI = Path(__file__).parent / "fixtures" / "statement_379971.txt"


@pytest.fixture
def multi() -> list[StatementRow]:
    return extract_from_text(FIXTURE_MULTI.read_text(encoding="utf-8"), "AS_379971.pdf")


def test_six_products_become_six_rows(multi):
    assert len(multi) == 6
    assert [r.product for r in multi] == [
        "PLAN 2A MA53 PLUM ANGELINO",
        "PLAN 2A PE80 PLUM ANGELINO",
        "GRSF 2 PE80 GRAPE SUGRA35",
        "GRCS 2 PE80 GRAPE CRIMSON SEEDLESS",
        "GRSF 2 PB8 GRAPE SUGRA35",
        "GRRG 2 PD50 GRAPE RED GLOBE",
    ]


def test_statement_fields_shared_across_rows(multi):
    for r in multi:
        assert r.stm_no == 379971
        assert r.dn == 14013
        assert r.date_received == date(2026, 5, 7)
        assert r.status == "13.05"


def test_per_section_quantities_and_prices(multi):
    got = [(r.qty_received, r.opening_stock, r.cartons_sold, r.price) for r in multi]
    assert got == [
        (111, 111, 111, 50.0),
        (11, 11, 11, 100.0),
        (62, 5, 5, 30.0),
        (41, 41, 41, 94.15),   # the section whose table spans the page break
        (3, 1, 1, 180.0),
        (69, 16, 16, 127.5),
    ]


def test_nett_apportioned_from_printed_costs(multi):
    """Each row's nett is recreated from the statement's own COSTS table.

    No per-product nett is printed, so we redistribute each printed deduction
    over the rows it applies to (by sales value) and subtract from gross. The
    shares must sum back to the statement's printed NETT AMOUNT exactly.
    """
    total = round(sum(r.nett_total for r in multi), 2)
    assert total == 10847.13, "apportioned shares must reconcile to the printed nett"
    for r in multi:
        assert r.nett_total > 0
        flag = next(f for f in r.flags if f.field == "nett_total")
        assert flag.severity == "warning"         # advisory estimate, append allowed
        assert not r.blocking


def test_product_levy_lands_only_on_its_product(multi):
    """PLUMS LEVY must reduce plum rows more than grape rows, per rand of sales.

    The two plum rows carry market fees + commission + bank + the plums levy;
    the grape rows carry everything except the levy. So the plum rows keep a
    smaller fraction of their gross value.
    """
    def keep_ratio(r, gross):
        return r.nett_total / gross
    # gross values from the statement, in row order
    gross = [5550.0, 1100.0, 150.0, 3860.0, 180.0, 2040.0]
    plum_ratio = keep_ratio(multi[0], gross[0])       # PLAN MA53
    grape_ratio = keep_ratio(multi[3], gross[3])      # GRCS crimson
    assert plum_ratio < grape_ratio, "the levied plum row keeps less per rand than a grape row"


def test_apportionment_falls_back_when_costs_do_not_reconcile():
    """If the printed deductions cannot explain the nett, rows go in at 0."""
    text = FIXTURE_MULTI.read_text(encoding="utf-8")
    # Corrupt the nett so it no longer matches gross minus the printed costs.
    text = text.replace("NETT AMOUNT 10847.13", "NETT AMOUNT 99999.99")
    rows = extract_from_text(text, "AS_bad.pdf")
    assert all(r.nett_total == 0.0 for r in rows)
    assert all(any(f.field == "nett_total" for f in r.flags) for r in rows)


def test_statement_level_cross_checks_pass(multi):
    """TOTAL SOLD (185) and GROSS AMOUNT (12880.00) agree with the sections."""
    assert sum(r.cartons_sold for r in multi) == 185
    # The only warnings should be the per-row nett notices, not misread totals.
    warnings = [f for r in multi for f in r.flags
                if f.severity == "warning" and f.field != "nett_total"]
    assert warnings == []


def test_single_product_statement_still_gets_its_nett(row):
    """The single-product path keeps NETT AMOUNT on the row, unflagged."""
    assert row.nett_total == 2238.01
    assert not any(f.field == "nett_total" for f in row.flags)
