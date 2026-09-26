"""The order sheet: the one thing here that leaves the building."""

from datetime import date

import pdfplumber
import pytest

from app import order_sheet, procurement
from tests.test_procurement import PAYS, ROWS, TODAY


@pytest.fixture(scope="module")
def plan() -> dict:
    return procurement.build(ROWS, PAYS, today=TODAY)


def read(pdf: bytes) -> str:
    """Everything on the page, as the person receiving it would read it."""
    with pdfplumber.open(__import__("io").BytesIO(pdf)) as doc:
        return "\n".join(page.extract_text() or "" for page in doc.pages)


def test_it_is_a_pdf_with_the_order_on_it(plan: dict) -> None:
    pdf = order_sheet.build(plan, today=date(2026, 9, 26))
    assert pdf[:5] == b"%PDF-"
    text = read(pdf)
    assert "Procurement order" in text
    assert "Zaco Agents (Pty) Ltd" in text
    assert "26 September 2026" in text
    # A month's order names its month; a shorter one says the span instead.
    assert "the next month (October 2026)" in " ".join(text.split())


def test_every_line_to_order_is_on_it_under_its_market(plan: dict) -> None:
    text = read(order_sheet.build(plan))
    for line in plan["lines"]:
        if line["take_on"] > 0:
            assert line["product"][:28] in text.replace("\n", " ")
            assert (line["market"] or "") in text


def test_the_figures_are_the_plan_s_own(plan: dict) -> None:
    """Nothing is recomputed here, so the paper cannot drift from the screen."""
    text = read(order_sheet.build(plan)).replace(" ", " ")
    t = plan["totals"]
    assert f"{t['cartons']:,} cartons".replace(",", " ") in text
    assert order_sheet.rand(t["expected_value"]).replace(" ", " ") in text


def test_it_says_what_the_figures_are_not(plan: dict) -> None:
    """It travels without anyone attached to explain it."""
    text = read(order_sheet.build(plan)).replace("\n", " ")
    assert "consignment" in text
    assert "never a margin" in text


def test_the_buyer_s_name_goes_on_it_when_there_is_one(plan: dict) -> None:
    named = read(order_sheet.build(plan, prepared_for="Piet Marais"))
    assert "For: Piet Marais" in named.replace("\n", " ")
    blank = read(order_sheet.build(plan))
    assert "For:" in blank


def test_room_to_grow_and_the_test_loads_travel_with_the_order(plan: dict) -> None:
    text = read(order_sheet.build(plan)).replace("\n", " ")
    assert "ROOM TO GROW" in text
    if any(l.get("trial") for l in plan["lines"]):
        assert "Worth a try" in text
    grown = [l for l in plan["lines"] if l.get("headroom") and l["take_on"] > 0]
    for line in grown:
        assert f"+{line['headroom']['cartons']}" in text


def test_an_empty_plan_is_still_a_sheet() -> None:
    """A month with nothing to order says so rather than printing a blank."""
    empty = procurement.build([], [], today=TODAY)
    text = read(order_sheet.build(empty))
    assert "Procurement order" in text
    assert "Nothing to order" in text


def test_the_file_is_named_after_the_month(plan: dict) -> None:
    assert order_sheet.filename(plan) == f"zaco-procurement-{plan['month']}.pdf"


def test_money_reads_the_way_this_business_writes_it() -> None:
    assert order_sheet.rand(158820).replace(" ", " ") == "R 158 820,00"
    assert order_sheet.rand(None) == "R 0,00"


def test_the_month_is_spelled_out() -> None:
    assert order_sheet.month_label("2026-10") == "October 2026"
    assert order_sheet.month_label(None) == ""
