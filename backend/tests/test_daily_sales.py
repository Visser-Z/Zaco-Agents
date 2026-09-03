"""Daily Sales Detail (consignment report) parser tests.

Fixture is the real 'Day report.pdf' text. This is the newer format that lists
consignments and their sale dockets; the old account-sales parser must not
touch it, and vice versa.
"""
from datetime import date
from pathlib import Path

import pytest

from app import daily_sales
from app.extraction import extract_statements
from app.schemas import StatementRow

DAY = (Path(__file__).parent / "fixtures" / "daily_sales_day.txt").read_text(encoding="utf-8")
OLD = (Path(__file__).parent / "fixtures" / "statement_387517.txt").read_text(encoding="utf-8")


def test_format_detection():
    assert daily_sales.is_daily_sales(DAY)
    assert not daily_sales.is_daily_sales(OLD)   # old account-sales must not match


@pytest.fixture
def rows() -> list[StatementRow]:
    return daily_sales.parse_daily_sales([DAY], "Day report.pdf")


def test_one_row_per_consignment(rows):
    assert len(rows) == 3
    assert [r.stm_no for r in rows] == [118170501, 118170503, 118246501]
    assert [r.dn for r in rows] == [1181705, 1181705, 1182465]   # Z stripped to int


def test_supplier_ref_and_exact_sales_total():
    # Newer exports fill in Supplier Ref (the DN Payment Details is keyed on) and
    # we keep the exact sales value for reconciliation.
    block = (
        "    TSHWANE MARKET      Farmers Trust (Pre)\n"
        "    Delivery ID: 1180694Z Supplier Ref: 20026*14847 Qty Sent: 898 Qty Amended To: 898 Qty Avail: 0\n"
        "    Consignment ID: 118069401Z Comment:\n"
        "    Product:  GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)\n"
        "      Date Sold   Docket Number Qty Sold Market Avg    Price       Sales Value\n"
        "    2026-08-04 PRE*B6H04S71439*01Z 100      R 112.29        R 50.00    R 5000.00\n"
        "                                   100                                 R 5000.00\n"
    )
    r = daily_sales.parse_daily_sales([block], "d.pdf")[0]
    assert r.supplier_ref == 14847        # DN shared with Payment Details
    assert r.sales_total == 5000.0        # exact, for reconciliation
    assert r.dn == 14847                  # column A ← Supplier Ref (matches payment docs)


def _product_of(product_line: str) -> str:
    block = (
        "    TSHWANE MARKET      Farmers Trust (Pre)\n"
        "    Delivery ID: 1180694Z Supplier Ref: 20026*14847 Qty Sent: 10 Qty Avail: 0\n"
        "    Consignment ID: 118069401Z Comment:\n"
        f"    Product:  {product_line}\n"
        "      Date Sold   Docket Number Qty Sold Market Avg    Price       Sales Value\n"
        "    2026-08-04 PRE*B6H04S71439*01Z 10      R 1.00        R 5.00     R 50.00\n"
    )
    return daily_sales.parse_daily_sales([block], "d.pdf")[0].product


def test_stray_column_number_is_stripped_from_the_product():
    """Some exports bleed a neighbouring column's number onto the Product line.
    Left in, the same product reads as two different ones: its totals split and
    it stops matching the payment report."""
    assert _product_of("GRAPES STARLIGHT CLASS 2 NO SIZE (PUNNET 5kg) 10") == \
        "GRAPES STARLIGHT CLASS 2 NO SIZE (PUNNET 5kg)"
    assert _product_of("NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg) 66") == \
        "NECTARINES OTHER CLASS 1 LARGE (MULTI LAYER TRAYER 5kg)"


def test_supplier_ref_for_another_producers_code():
    """Not every supplier ref starts with Zaco's own 20026. Requiring it lost
    the reconciliation key on produce carried for another producer, so those
    consignments could never match their payment."""
    block = (
        "    TSHWANE MARKET      Farmers Trust (Pre)\n"
        "    Delivery ID: 1176362Z Supplier Ref: 14013*14798 Qty Sent: 170 Qty Avail: 0\n"
        "    Consignment ID: 117636201Z Comment:\n"
        "    Product:  ORANGES NAVEL / OTHER CLASS 2 LARGE (BANANA BOX 15kg)\n"
        "      Date Sold   Docket Number Qty Sold Market Avg    Price       Sales Value\n"
        "    2026-06-24 PRE*B6F24S77201*01Z 170     R 0.00        R 60.00    R 10200.00\n"
    )
    r = daily_sales.parse_daily_sales([block], "d.pdf")[0]
    assert r.supplier_ref == 14798
    assert r.dn == 14798


def test_genuine_product_names_are_never_truncated():
    """Only a bare number straight after the container bracket is stripped. A
    size, a weight, or a name that legitimately ends in a digit must survive."""
    for name in [
        "CHERRIES OTHER CLASS 1 LARGE (STANDARD TRAY 4.5kg)",
        "GRAPES RALLI CLASS 1 NO SIZE PUNNET 500 gms",
        "GRANADILLAS NO VARIETY NOT GRADED NO SIZE BOX 10kg",
        "NECTARINES OTHER CLASS 1 NO SIZE (CARTON 8kg)",
        "SOME PRODUCT WITHOUT BRACKETS CLASS 1",
    ]:
        assert _product_of(name) == name, name


def test_market_is_captured_separately(rows):
    # The "MARKET  Agent (Pre)" header carries both; the market is kept for
    # per-market analytics rather than discarded.
    assert [r.market for r in rows] == ["TSHWANE MARKET"] * 3
    assert all(r.market_agent == "Farmers Trust" for r in rows)


def test_confident_columns(rows):
    r = rows[0]  # nectarines consignment
    assert r.market_agent == "Farmers Trust"
    assert r.product == "NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 11kg"
    assert r.qty_received == 71      # G <- Qty Sent
    assert r.opening_stock == 70     # H <- Qty Avail
    assert r.cartons_sold == 1       # J <- Qty Sold
    assert r.price == 50.0           # L <- Sales Value / Qty Sold
    assert r.status == "27.07"       # T <- Date Sold DD.MM


def test_price_is_sales_value_over_qty(rows):
    # 71 sent, sold 1 @ R50 -> Gross (M = J x L) recovers the R50 sales value
    r = rows[0]
    assert round(r.cartons_sold * r.price, 2) == 50.0


def test_cancelled_consignment_nets_to_zero(rows):
    # Block 3: 10 sold then -10 cancelled -> subtotal 0, no real sale
    r = rows[2]
    assert r.cartons_sold == 0
    assert r.price == 0.0


def test_the_return_is_kept_as_its_own_figure(rows):
    """Netting to zero is right, but "sold 10 and 10 came back" and "never sold
    anything" are not the same month, and the net alone cannot tell them apart.
    Both halves are held positive beside the net."""
    r = rows[2]
    assert r.cartons_sold == 0            # net, as the workbook column holds it
    assert r.cartons_returned == 10       # positive, not -10
    assert r.returns_total == 2000.0      # positive, though the PDF prints R -2,000.00


def test_a_consignment_with_no_returns_says_so(rows):
    """Zero, not None: the report showed the dockets and none of them came back.
    None is reserved for a source that could not tell."""
    r = rows[0]
    assert r.cartons_returned == 0
    assert r.returns_total == 0.0


def test_nett_is_left_for_the_operator(rows):
    for r in rows:
        assert r.nett_total is None
        flag = next(f for f in r.flags if f.field == "nett_total")
        assert flag.severity == "warning"
        assert not r.blocking


def test_routes_through_extract_statements():
    """The public entry point must auto-detect and use the daily-sales parser.

    Asserts on the routing itself rather than on a relative fixture path, which
    only held when pytest happened to be run from `backend/`.
    """
    from app.extraction import statements_from_pages

    rows = statements_from_pages([DAY], "x.pdf")
    assert rows and all(r.source_file.startswith("x.pdf") for r in rows)
    # Delivery/Consignment IDs prove the daily-sales parser handled it, not the
    # account-sales one.
    assert [r.stm_no for r in rows] == [118170501, 118170503, 118246501]


# --- one row per consignment per TRADING DAY ------------------------------

def test_a_consignment_is_identified_as_a_consignment():
    """Without this the field stays empty, every row becomes its own delivery,
    and Qty Sent is counted once per row instead of once per consignment. A
    consignment of 71 selling across three daily reports then claims 213 cartons
    were sent and reports a third of its real sell-through."""
    rows = daily_sales.parse_daily_sales([DAY], "day.txt")
    assert all(r.consignment_id is not None for r in rows)
    assert all(r.consignment_id == r.stm_no for r in rows)


def test_the_same_consignment_on_two_days_is_two_rows_not_a_duplicate():
    """The reports are loaded a trading day at a time and the PDF prints no
    account sale number, so column E carries the Consignment ID and every day of
    a consignment repeats it. Identity therefore has to include the day, or the
    second day is read as the first one saved twice and thrown away."""
    import app.main as main
    from app.supabase_auth import User

    user = User(id="u", email="op@x.com", token="t")
    monday = daily_sales.parse_daily_sales([DAY], "mon.txt")[0]
    wednesday = daily_sales.parse_daily_sales([DAY], "wed.txt")[0]
    monday.market_agent = wednesday.market_agent = "Farmers Trust"
    monday.date = date(2026, 8, 3)
    wednesday.date = date(2026, 8, 5)

    keys = set()
    for row in (monday, wednesday):
        rec = main._statement_record(row, user)
        keys.add((rec["market_agent"], rec["stm_no"], rec["consignment_id"], rec["group_date"]))
    assert len(keys) == 2, "the two days collapsed onto one key"
