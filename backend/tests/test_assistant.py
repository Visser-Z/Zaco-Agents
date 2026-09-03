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
