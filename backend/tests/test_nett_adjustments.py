"""Nett Payment Adjustments parser + Nett-matching tests.

This report carries no products, only the Nett/Gross per account sale. Its role
is to fill the Nett onto sales rows that came from a Daily Sales report, keyed
on statement number (column E). One account sale can split across several
adjustment lines, which must be summed.
"""

from pathlib import Path

from app import nett_adjustments as na
from app.main import fill_netts
from app.schemas import Flag, StatementRow

FIX = Path(__file__).parent / "fixtures"
NETT = (FIX / "nett_adjustments_july.txt").read_text(encoding="utf-8")
DAILY = (FIX / "daily_sales_day.txt").read_text(encoding="utf-8")
OLD = (FIX / "statement_387517.txt").read_text(encoding="utf-8")


def test_format_detection():
    assert na.is_nett_adjustments(NETT)
    assert not na.is_nett_adjustments(DAILY)   # sales reports must not match
    assert not na.is_nett_adjustments(OLD)


def _parsed():
    return na.parse_nett_adjustments([NETT], "nett.pdf")


def test_farmers_trust_single_lines():
    m = _parsed()
    assert m[387517]["nett"] == 2238.01
    assert m[387517]["gross"] == 2700.0
    assert m[387517]["dn"] == 14847
    assert m[388486]["nett"] == 7668.0       # matches the workbook's own figure
    assert m[387516]["dn"] == 30559          # "14013*30559"


def test_split_adjustment_lines_are_summed():
    # 5644102 appears as /1../4 on four dates (date glued to the accsale, no space).
    rec = _parsed()[5644102]
    assert rec["lines"] == 4
    assert rec["nett"] == 8696.82            # 5112.71 + 3083.32 + 513.75 - 12.96
    assert rec["gross"] == 10400.0           # 6000 + 3800 + 600 + 0


def test_total_and_grand_total_lines_are_ignored():
    m = _parsed()
    # Only real account sales -- the R 22 060.00 grand total is not a statement.
    assert set(m) == {387517, 387516, 388486, 5644102}


def _sales_row(stm_no, nett=None, with_warning=False):
    flags = []
    if with_warning:
        flags.append(Flag(field="nett_total", severity="warning", message="Enter Nett by hand."))
    return StatementRow(source_file="day.pdf", stm_no=stm_no, nett_total=nett,
                        cartons_sold=1, price=10.0, flags=flags)


def test_fill_netts_matches_by_statement_number():
    rows = [
        _sales_row(387517, with_warning=True),   # blank Nett, flagged -> filled
        _sales_row(388486, with_warning=True),
        _sales_row(999999, with_warning=True),   # no match -> untouched
    ]
    result = fill_netts(rows, _parsed())

    assert rows[0].nett_total == 2238.01
    assert rows[1].nett_total == 7668.0
    assert rows[2].nett_total is None
    # The filled rows lose the "enter Nett by hand" warning; the unmatched keeps it.
    assert not any(f.field == "nett_total" for f in rows[0].flags)
    assert any(f.field == "nett_total" for f in rows[2].flags)

    assert result.matched == 2
    assert result.unmatched_rows == 1
    assert result.report_statements == 4
    assert result.unused == 2                # 387516 and 5644102 matched no row


def test_report_nett_overrides_a_provisional_value():
    # The adjustments report is the final figure, so it wins over an existing one.
    rows = [_sales_row(387517, nett=0.0)]
    fill_netts(rows, _parsed())
    assert rows[0].nett_total == 2238.01
