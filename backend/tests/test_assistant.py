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
    assert "JOBURG MKT" not in text.split("### Still owed, by market")[1]


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
