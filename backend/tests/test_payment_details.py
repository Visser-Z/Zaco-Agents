"""Payment Details parser tests.

The payment-side report: one block per account-sale payout, with commodity
lines carrying Sales Total, plus the Nett/Gross. Reconciled against the
accumulated Daily Sales Details to supply the Nett.
"""

from pathlib import Path

from app import payment_details as pdd

FIX = Path(__file__).parent / "fixtures"
PD = (FIX / "payment_details_week32.txt").read_text(encoding="utf-8")
DAILY = (FIX / "daily_sales_day.txt").read_text(encoding="utf-8")
NETT = (FIX / "nett_adjustments_july.txt").read_text(encoding="utf-8")


def test_format_detection():
    assert pdd.is_payment_details(PD)
    assert not pdd.is_payment_details(DAILY)   # sales report
    assert not pdd.is_payment_details(NETT)    # shares column names, but no Commodity/title


def _recs():
    return pdd.parse_payment_details([PD], "pd.pdf")


def test_one_record_per_account_sale():
    recs = _recs()
    assert [r["stm_no"] for r in recs] == [392825, 392828, 392831]
    assert [r["dn"] for r in recs] == [15061, 14585, 14585]
    assert all(r["market_agent"] == "Farmers Trust" for r in recs)


def test_money_columns():
    r = next(r for r in _recs() if r["stm_no"] == 392828)
    assert r["nett"] == 2304.40
    assert r["gross"] == 2720.00
    assert r["deductions"] == 361.35
    assert r["vat"] == 54.25


def test_multi_line_block_keeps_every_commodity_line():
    # 392825 has two nectarine lines that sum to the block's gross.
    r = next(r for r in _recs() if r["stm_no"] == 392825)
    assert [l["sales_total"] for l in r["lines"]] == [760.00, 80.00]
    assert [l["sold"] for l in r["lines"]] == [6, 1]
    assert round(sum(l["sales_total"] for l in r["lines"]), 2) == r["gross"]


def test_line_totals_reconcile_to_gross_across_report():
    # Every commodity line captured, none double-counted: line totals == gross.
    recs = _recs()
    line_sum = round(sum(l["sales_total"] for r in recs for l in r["lines"]), 2)
    gross_sum = round(sum(r["gross"] for r in recs), 2)
    assert line_sum == gross_sum


def test_date_range():
    assert pdd.date_range(PD) == ("2026-08-01", "2026-08-05")
    assert pdd.date_range("no range here") == (None, None)


# --- the second export layout -------------------------------------------
# Some exports put the line number, commodity, quantities and total all on one
# line instead of wrapping. A file uses one layout throughout. Getting this
# wrong is silent: headers still parse, so reconciliation reports everything
# "unpaid" rather than failing.

INLINE = """\
    Report:   Payment Details
    Date Range: 2026/07/01 - 2026/07/31
    TSHWANE MARKET                         Farmers Trust (Pre)
     FMS ID  Supplier Ref AccSale Number Date
                                            Payments Deductions Vat  Payments Ref
     6379636 20026*14565 & 14980 JOH*SUB*5644102/1 2026-07-13 R 5112.71 R 771.56 R 115.73 R 6000.00
     Line No              Commodity                Delivered Sold     Sales Total
    01    GRAPES THOMPSON SEEDLESS CLASS 1 NO SIZE PUNNET 5.00 kg 10 10 R 4000.00
    02    GRAPES RALLI CLASS 1 NO SIZE PUNNET 500 gms 19      4        R 1600.00
    04    NECTARINES DIAMOND BRIGHT CLASS 1 NO SIZE CARTON 8.00 kg 32 1 R 400.00
     203454   20026*14977 PRE*BT*389181 2026-07-13 R 0.00 R 34.56 R 5.44 R 40.00 EFT
     Line No              Commodity                Delivered Sold     Sales Total
    01    NECTARINES OTHER CLASS 1 LARGE MULTI LAYER TRAYER 5.00 kg 42 32 R 160.00
    03    CHERRIES OTHER CLASS 1 LARGE STANDARD TRAY 3.00 kg 41 25     R -120.00
     203450   20026*14954 PRE*BT*388189 2026-07-06 R 6919.09 R 1070.33 R 160.58 R 8150.00
    Total                                   R 19238.10 R 2984.15 R 447.75 R 22670.00
"""


def _inline():
    return pdd.parse_payment_details([INLINE], "inline.pdf")


def test_inline_layout_is_parsed():
    recs = _inline()
    assert [r["stm_no"] for r in recs] == [5644102, 389181, 388189]
    first = recs[0]
    assert [l["sales_total"] for l in first["lines"]] == [4000.0, 1600.0, 400.0]
    # A commodity name containing its own numbers must survive intact.
    assert first["lines"][1]["product"] == "GRAPES RALLI CLASS 1 NO SIZE PUNNET 500 gms"
    assert first["lines"][1]["delivered"] == 19 and first["lines"][1]["sold"] == 4


def test_negative_commodity_lines_are_kept():
    """A credit or return is a negative Sales Total. Dropping it silently
    inflates the product's takings: here 160 - 120 must equal the R40 gross."""
    rec = next(r for r in _inline() if r["stm_no"] == 389181)
    assert [l["sales_total"] for l in rec["lines"]] == [160.0, -120.0]
    assert round(sum(l["sales_total"] for l in rec["lines"]), 2) == rec["gross"] == 40.0


def test_block_with_no_breakdown_keeps_its_money():
    """Some account sales print no commodity lines at all. The record must
    still carry its gross/nett so the amount can be reported as unattributed
    rather than quietly disappearing."""
    rec = next(r for r in _inline() if r["stm_no"] == 388189)
    assert rec["lines"] == []
    assert rec["gross"] == 8150.0 and rec["nett"] == 6919.09


# --- layouts mixed inside one block, and other producers' codes ----------

MIXED = """\
    Report:   Payment Details
     6385669  20026*14576 JOH*SUB*5650429/1 2026-07-22 R 22,018.31 R 3,357.96 R 503.73 R 25,880.00 EFT
     Line No              Commodity                Delivered Sold     Sales Total
          NECTARINES OTHER CLASS 1 NO SIZE CARTON 8.00 kg
    6385669                                          57      57        R 4,560.00
    6385669 GRANADILLAS NO VARIETY NOT GRADED NO SIZE OTHER CONTAINERS 3.00 kg 32 12 R 1,800.00
          GRAPES STARLIGHT CLASS 2 NO SIZE PUNNET 5.00 kg 15 15        R 1,500.00
     203441   14013*30559 PRE*BT*387516 2026-07-01 R 51.37 R 7.50 R 1.13 R 60.00
     Line No              Commodity                Delivered Sold     Sales Total
          EXOTIC CITRUS SONET CLASS 1 MEDIUM CARTON 10.00 kg
    203441                                           2        2        R 60.00
"""


def test_layouts_mixed_within_one_block_are_all_captured():
    """A long commodity name wraps onto its own line while a short one stays
    inline, so both shapes occur inside a single account sale. Matching one
    shape and stopping lost R8 800 from a real July block, silently."""
    rec = next(r for r in pdd.parse_payment_details([MIXED], "m.pdf")
               if r["stm_no"] == 5650429)
    assert [l["sales_total"] for l in rec["lines"]] == [4560.0, 1800.0, 1500.0]
    assert rec["lines"][1]["product"].startswith("GRANADILLAS")
    assert rec["lines"][2]["product"].startswith("GRAPES STARLIGHT")


def test_another_producers_code_is_still_an_account_sale():
    """Supplier refs are not all Zaco's own 20026: produce carried for another
    producer uses theirs (14013*30559). Requiring 20026 made that header
    invisible, so the account sale vanished AND its commodity lines were
    absorbed into the record above it — inflating that one and losing this."""
    recs = pdd.parse_payment_details([MIXED], "m.pdf")
    assert [r["stm_no"] for r in recs] == [5650429, 387516]
    other = recs[1]
    assert other["dn"] == 30559 and other["gross"] == 60.0
    assert [l["sales_total"] for l in other["lines"]] == [60.0]
    # The block above must not have swallowed it.
    assert sum(l["sales_total"] for l in recs[0]["lines"]) == 7860.0


def test_product_string_preserved_for_normalisation():
    r = next(r for r in _recs() if r["stm_no"] == 392828)
    assert r["lines"][0]["product"] == "NECTARINES OTHER CLASS 1 LARGE MULTI LAYER TRAYER 5.00 kg"


# --- telling the reports apart, and telling empty from unreadable ----------

EMPTY_PAYMENT = """\
    Report                                              Zaco Agents (Pty) Ltd (20026)
    Report:   Payment Details                               Run Date: 2026/09/02 20:01:14
    Market:   ALL
    Agent:    ALL
    Date Range: 2026/08/01 - 2026/08/01

    Grand Total                                R 0.00  R 0.00   R 0.00   R 0.00
"""


def test_a_day_with_no_payments_is_still_a_payment_report():
    """Six of eight real August exports look like this: a header, a Grand Total
    of zero, and no column headings, because there was nothing to head. Rejected
    as unreadable it tells the operator their file is broken when the truth is
    the day was quiet."""
    from app import payment_details as pd
    assert pd.is_payment_details(EMPTY_PAYMENT) is True
    assert pd.parse_payment_details([EMPTY_PAYMENT], "empty.pdf") == []
    assert pd.date_range(EMPTY_PAYMENT) == ("2026-08-01", "2026-08-01")


def test_a_payment_report_is_not_read_as_an_adjustments_report():
    """They share three column headings, and the sales import tests for an
    adjustments report first, so without its own title a Payment Details PDF
    dropped there contributed Netts nobody asked for."""
    from app import nett_adjustments as na, payment_details as pd
    with open("tests/fixtures/payment_details_week32.txt", encoding="utf-8") as fh:
        text = fh.read()
    assert pd.is_payment_details(text) is True
    assert na.is_nett_adjustments(text) is False


def test_an_adjustments_report_is_still_recognised():
    from app import nett_adjustments as na, payment_details as pd
    with open("tests/fixtures/nett_adjustments_july.txt", encoding="utf-8") as fh:
        text = fh.read()
    assert na.is_nett_adjustments(text) is True
    assert pd.is_payment_details(text) is False
