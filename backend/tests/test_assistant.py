"""Assistant context-building tests.

The prompt is assembled by pure code, so everything that determines the quality
and honesty of an answer is testable without an API key. Only the network call
itself is untested here.
"""

import app.assistant as assistant


def _row(**kw) -> dict:
    base = {
        "market_agent": "Farmers Trust",
        "market": "TSHWANE MARKET",
        "description": "IMP Nect",
        "product": "NECTARINES OTHER",
        "cartons_sold": 10,
        "price": 50.0,
        "nett_total": None,
        "group_date": "2026-07-27",
    }
    base.update(kw)
    return base


def test_empty_history_says_so():
    text = assistant.build_context([])
    assert "No sales have been recorded" in text


def test_totals_are_precomputed_not_left_to_the_model():
    # 10 x 50 + 4 x 25 = 600. The model must never have to derive this.
    rows = [_row(cartons_sold=10, price=50.0), _row(cartons_sold=4, price=25.0)]
    text = assistant.build_context(rows)
    assert "Sales value: R 600,00" in text
    assert "Consignments: 2" in text


def test_context_carries_aggregates_and_rows():
    rows = [
        _row(product="NECTARINES OTHER", market="TSHWANE MARKET", market_agent="Farmers Trust"),
        _row(product="PLUMS", market="JOBURG MKT", market_agent="Subtropico"),
    ]
    text = assistant.build_context(rows)
    for section in ("Products by sales value", "Markets by sales value",
                    "Agents by sales value", "Month by month",
                    "Individual consignments"):
        assert section in text, section
    # Both the rolled-up label and the underlying row are present.
    assert "NECTARINES OTHER" in text and "JOBURG MKT" in text
    assert "2026-07-27" in text


def test_rows_are_capped_but_totals_still_cover_everything():
    rows = [_row(cartons_sold=1, price=1.0) for _ in range(assistant.MAX_ROWS + 50)]
    text = assistant.build_context(rows)
    # Totals span every row...
    assert f"Consignments: {len(rows)}" in text
    # ...while the verbatim rows are bounded, and say so.
    assert f"({assistant.MAX_ROWS} rows)" in text
    assert f"most recent {assistant.MAX_ROWS} of {len(rows)}" in text


def test_south_african_number_formatting():
    # R 12 500,00 -- space thousands separator, comma decimal.
    rows = [_row(cartons_sold=250, price=50.0)]
    assert "R 12 500,00" in assistant.build_context(rows)


def test_prompt_forbids_inventing_cost_prices():
    """The honesty guarantee: no purchase prices exist anywhere in the data, so
    the assistant must decline to state profit rather than estimate it. If this
    ever gets edited out, the assistant starts making up margins."""
    prompt = assistant.SYSTEM.lower()
    assert "never estimate a cost price" in prompt
    assert "purchase prices" in prompt


def test_configured_follows_the_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not assistant.configured()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert assistant.configured()


def test_ask_without_a_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import pytest
    with pytest.raises(assistant.AssistantError, match="ANTHROPIC_API_KEY"):
        assistant.ask("what sold best?", [_row()])


def test_the_written_plan_is_handed_the_room_to_grow_with_the_figures():
    """The write-up cannot find growth of its own, so the context has to carry
    it: the extra cartons, what they are worth, and why the line earned them."""
    from datetime import date

    from app import procurement
    from tests.test_procurement import PAYS, ROWS, GRAPES, TODAY

    plan = procurement.build(ROWS, PAYS, today=TODAY)
    text = assistant.plan_context(plan)
    grow = next(l for l in plan["lines"] if l["product"] == GRAPES)["headroom"]
    assert "Room to grow:" in text
    assert f"ROOM TO GROW: {grow['cartons']} cartons" in text
    assert "sold every carton sent" in text
    assert f"worth about {assistant._rand(grow['worth'])} more" in text


def test_the_plan_prompt_tells_the_model_not_to_invent_growth():
    assert "Never suggest more of something the plan does not say there is room for" \
        in assistant.PLAN_SYSTEM


# --- what is owed, which the rows alone cannot say ------------------------

SETTLE_ROWS = [
    {"consignment_id": 100, "dn": 1855491, "product": "GRAPES SWEET CELEBRATION",
     "market": "DURBAN MARKET", "market_agent": "Grow Port Natal", "cartons_sold": 100,
     "price": 400.0, "sales_total": 40000.0, "last_sale": "2026-09-03",
     "date_received": "2026-09-01", "group_date": "2026-09-01", "qty_received": 100},
    {"consignment_id": 200, "dn": 14370, "product": "GRAPES RALLI",
     "market": "JOBURG MKT - BR / MAR", "market_agent": "Grow Marco", "cartons_sold": 50,
     "price": 200.0, "sales_total": 10000.0, "last_sale": "2026-09-05",
     "date_received": "2026-09-04", "group_date": "2026-09-04", "qty_received": 50},
]
SETTLE_PAYS = [
    {"accsale": "JOH*MAR*1", "dn": 14370, "date": "2026-09-12", "gross": 10000.0,
     "nett": 8500.0, "lines": [{"product": "GRAPES RALLI", "sales_total": 10000.0}]},
]


def test_the_block_carries_what_is_owed_market_by_market():
    """Durban sold R 40 000,00 and nothing has been paid against it. The rows
    cannot say that; the settlement can, and now does."""
    text = assistant.settlement_context(SETTLE_ROWS, SETTLE_PAYS)
    assert "Payment and what is still owed" in text
    assert "DURBAN MARKET | Grow Port Natal | 40 000,00 | 1" in text
    assert "GRAPES SWEET CELEBRATION" in text
    # Joburg was paid in full, so it is not on the outstanding list at all.
    assert "JOBURG MKT" not in text.split("### Still owed, all months together, by market")[1]


def test_the_block_says_what_a_blank_nett_does_not_mean():
    """The wrong answer this fixes: asked what Durban still owed, the
    assistant said it could not tell because the nett column was blank, while
    Tracking showed R 107 740,00 outstanding on the same screen."""
    text = assistant.settlement_context(SETTLE_ROWS, SETTLE_PAYS)
    assert "NOT whether a consignment has been paid" in text
    assert "It is NOT whether the consignment has been paid" in assistant.SYSTEM
    assert "Never reason about payment from the nett column" in assistant.SYSTEM


def test_the_settlement_travels_with_every_question():
    block = assistant.data_block(SETTLE_ROWS, SETTLE_PAYS)
    assert "Payment and what is still owed" in block
    assert "Still to come: R 40 000,00" in block


def test_paid_and_owed_agree_with_the_tracking_tab():
    """Both read the same settlement, so a figure quoted in the chat is the
    figure on the screen behind it."""
    from app import tracking

    status = tracking.payment_status(SETTLE_ROWS, SETTLE_PAYS)
    text = assistant.settlement_context(SETTLE_ROWS, SETTLE_PAYS)
    assert assistant._rand(status["still_to_come"]) in text
    assert assistant._rand(status["total_paid"]) in text


def test_an_empty_book_has_no_settlement_block():
    assert assistant.settlement_context([], []) == ""


# --- each month on its own, the way the tab shows it -----------------------

# One Durban consignment that sold across the turn of the month: R 30 000,00
# in August and R 20 000,00 in September, with R 25 000,00 paid against it.
SPANNING = [
    {"consignment_id": 300, "dn": 1855491, "product": "GRAPES SUGRAONE",
     "market": "DURBAN MARKET", "market_agent": "Grow Port Natal", "cartons_sold": 75,
     "price": 400.0, "sales_total": 30000.0, "last_sale": "2026-08-29",
     "date_received": "2026-08-27", "group_date": "2026-08-27", "qty_received": 125},
    {"consignment_id": 300, "dn": 1855491, "product": "GRAPES SUGRAONE",
     "market": "DURBAN MARKET", "market_agent": "Grow Port Natal", "cartons_sold": 50,
     "price": 400.0, "sales_total": 20000.0, "last_sale": "2026-09-02",
     "date_received": "2026-08-27", "group_date": "2026-08-27", "qty_received": 125},
]
SPAN_PAID = [{"accsale": "DUR*13*1", "dn": 1855491, "date": "2026-09-05", "gross": 25000.0,
              "nett": 21250.0,
              "lines": [{"product": "GRAPES SUGRAONE", "sales_total": 25000.0}]}]


def test_a_consignment_that_crossed_the_month_is_owed_in_both():
    """The wrong answer this fixes: asked what September still owed, the chat
    had only the all-time lines, each dated by its first sale, so a Durban
    consignment that started in August and sold on into September counted
    wholly as August and September came out far too low."""
    months = {m["month"]: m for m in assistant.month_settlement(SPANNING, SPAN_PAID)}
    assert months["2026-08"]["sold"] == 30000.0
    assert months["2026-09"]["sold"] == 20000.0
    # Paid oldest first: August's R 30 000,00 takes the R 25 000,00 first.
    assert months["2026-08"]["owed"] == 5000.0
    assert months["2026-09"]["owed"] == 20000.0


def test_the_months_add_up_to_the_whole():
    from app import tracking

    months = assistant.month_settlement(SPANNING, SPAN_PAID)
    whole = tracking.payment_status(SPANNING, SPAN_PAID)["still_to_come"]
    assert round(sum(m["owed"] for m in months), 2) == whole == 25000.0


def test_each_month_is_the_figure_the_tab_shows_with_that_month_open():
    from app import analytics, tracking

    for m in assistant.month_settlement(SPANNING, SPAN_PAID):
        lo, hi = analytics.period_bounds(month=m["month"])
        tab = tracking.payment_status(SPANNING, SPAN_PAID, frozenset(), lo, hi)
        assert m["owed"] == tab["still_to_come"]
        assert m["paid"] == tab["total_paid"]


def test_the_chat_is_told_to_answer_a_month_from_the_month_table():
    text = assistant.settlement_context(SPANNING, SPAN_PAID)
    assert "### Month by month" in text
    assert "2026-09 | 20 000,00 |" in text
    assert "- 2026-09: DURBAN MARKET R 20 000,00 (1 line) = R 20 000,00" in text
    assert "never filter or add these lines up" in text
    assert "Never work a month's figure out by adding up outstanding lines" in assistant.SYSTEM


def test_a_return_comes_off_what_its_delivery_still_owes_oldest_first():
    """A return booked in September against a delivery whose August sales are
    unpaid: the market keeps one balance per delivery, so the return comes off
    the oldest money still owed on it. September is not left below zero, and
    the months still add up to the same total."""
    ret = {
        "consignment_id": 300, "dn": 1855491, "product": "GRAPES SUGRAONE",
        "market": "DURBAN MARKET", "market_agent": "Grow Port Natal", "cartons_sold": -60,
        "price": 400.0, "sales_total": -24000.0, "last_sale": "2026-09-10",
        "date_received": "2026-08-27", "group_date": "2026-08-27", "qty_received": 125}
    before = {m["month"]: m for m in assistant.month_settlement(SPANNING, [])}
    after = {m["month"]: m for m in assistant.month_settlement(SPANNING + [ret], [])}
    assert after["2026-09"]["owed"] == before["2026-09"]["owed"]
    assert after["2026-08"]["owed"] == before["2026-08"]["owed"] - 24000.0
    assert "below zero" not in assistant.settlement_context(SPANNING + [ret], [])


# --- a debt, or a question: the chat knows which ---------------------------

def test_every_outstanding_line_tells_the_chat_why_it_is_owed():
    rows = [SETTLE_ROWS[0]]
    pays = [{"accsale": "DUR*13*9", "dn": 1855491, "date": "2026-09-10", "gross": 24000.0,
             "nett": 20400.0, "lines": [{"product": "GRAPES SWEET CELEBRATION",
                                          "sales_total": 24000.0, "sold": 60}]}]
    text = assistant.settlement_context(rows, pays)
    line = [l for l in text.split("\n") if "1855491 | GRAPES SWEET CELEBRATION" in l][0]
    assert line.endswith("| 100 | 60 | Cartons unpaid")
    assert "R 16 000,00 is money genuinely owed" in text


def test_the_month_table_splits_what_to_chase_from_what_to_check():
    text = assistant.settlement_context(SPANNING, SPAN_PAID)
    assert "of which to chase (R) | of which to check (R)" in text


def test_the_chat_is_told_a_price_query_is_not_a_debt():
    assert "questions, not debts" in assistant.SYSTEM
    assert "give the money to chase and name the rest separately" in assistant.SYSTEM
