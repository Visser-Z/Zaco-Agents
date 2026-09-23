"""Where to send it: each destination scored on rand back per carton sent."""

from app import scorecard

GRAPES = "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"


def _row(cid, market, agent, sold, value, day, received=None, qty=None, product=GRAPES):
    return {"consignment_id": cid, "product": product, "market": market, "market_agent": agent,
            "cartons_sold": sold, "price": value / sold if sold else 0, "sales_total": value,
            "last_sale": day, "date_received": received or day, "qty_received": qty or sold,
            "qty_amended": qty}


def _pay(acc, gross, nett, day):
    return {"accsale": acc, "gross": gross, "nett": nett, "date": day, "lines": []}


# Tshwane: R400 a carton, agent keeps 15%, 80% of what is sent sells.
# Durban:  R450 a carton, agent keeps 20%, 50% of what is sent sells.
ROWS = [
    _row(101, "TSHWANE MARKET", "Farmers Trust", 80, 32000.0, "2026-08-10", "2026-08-01", 100),
    _row(102, "TSHWANE MARKET", "Farmers Trust", 40, 16000.0, "2026-08-20", "2026-08-10", 50),
    _row(201, "DURBAN MARKET", "Grow Port Natal", 50, 22500.0, "2026-08-12", "2026-08-01", 100),
    _row(202, "DURBAN MARKET", "Grow Port Natal", 25, 11250.0, "2026-08-22", "2026-08-10", 50),
]
PAYS = [_pay("PRE*BT*1", 10000.0, 8500.0, "2026-08-15"),
        _pay("DUR*13*1", 10000.0, 8000.0, "2026-08-16")]


def _product(out, name=GRAPES):
    return next(p for f in out["fruits"] for p in f["products"] if p["product"] == name)


def test_rand_back_per_carton_sent_is_price_less_the_cut_times_what_sold():
    p = _product(scorecard.build(ROWS, PAYS, months=3))
    by = {d["market"]: d for d in p["destinations"]}
    assert (by["TSHWANE MARKET"]["price"], by["TSHWANE MARKET"]["agent_cut"],
            by["TSHWANE MARKET"]["sell_through"]) == (400.0, 0.15, 0.8)
    assert by["TSHWANE MARKET"]["back_per_carton"] == 272.0        # 400 x 0.85 x 0.8
    assert by["DURBAN MARKET"]["back_per_carton"] == 180.0         # 450 x 0.80 x 0.5


def test_the_higher_price_is_not_the_better_destination_on_its_own():
    """Durban fetches R50 more a carton, and returns R92 less per carton sent."""
    v = _product(scorecard.build(ROWS, PAYS, months=3))["verdict"]
    assert (v["kind"], v["market"], v["runner_up"]) == ("best", "TSHWANE MARKET", "DURBAN MARKET")
    assert v["lead_per_carton"] == 92.0


def test_one_destination_is_nothing_to_compare():
    rows = [r for r in ROWS if r["market"] == "TSHWANE MARKET"]
    assert _product(scorecard.build(rows, PAYS))["verdict"]["kind"] == "only"


def test_one_consignment_is_an_anecdote_not_a_comparison():
    rows = ROWS[:2] + ROWS[2:3]            # Durban has one consignment
    assert _product(scorecard.build(rows, PAYS))["verdict"]["kind"] == "thin"


def test_fruit_still_on_the_floor_does_not_count_as_unsold():
    """A consignment that arrived days ago is not a failure to sell."""
    fresh = _row(103, "TSHWANE MARKET", "Farmers Trust", 5, 2000.0, "2026-08-22", "2026-08-20", 200)
    def tshwane(rows):
        p = _product(scorecard.build(rows, PAYS))
        return next(d for d in p["destinations"] if d["market"] == "TSHWANE MARKET")
    with_fresh, without = tshwane(ROWS + [fresh]), tshwane(ROWS)
    assert with_fresh["sell_through"] == without["sell_through"] == 0.8
    assert with_fresh["sell_through_from"] == without["sell_through_from"]


def test_an_unknown_cut_is_the_books_usual_rate_and_says_so():
    pays = [PAYS[0]]                        # nothing paid from Durban
    p = _product(scorecard.build(ROWS, pays))
    dbn = next(d for d in p["destinations"] if d["market"] == "DURBAN MARKET")
    assert dbn["agent_cut"] == 0.15 and dbn["agent_cut_known"] is False


def test_the_same_agency_at_two_markets_is_two_destinations():
    """Subtropico sells at Joburg TFresh and at Springs on different terms."""
    rows = [_row(301, "JOBURG MKT - TFRESH", "Subtropico", 20, 8000.0, "2026-08-10", "2026-08-01", 20),
            _row(401, "SPRINGS MARKET", "Subtropico", 20, 6000.0, "2026-08-10", "2026-08-01", 20)]
    pays = [_pay("JOH*SUB*1", 1000.0, 900.0, "2026-08-12"),
            _pay("SPR*SUB*1", 1000.0, 700.0, "2026-08-12")]
    cut = scorecard.build(rows, pays)["agent_cut"]
    assert cut["JOBURG MKT - TFRESH"]["rate"] == 0.1 and cut["SPRINGS MARKET"]["rate"] == 0.3


def test_products_are_grouped_by_fruit_and_tiny_lines_left_out():
    plums = _row(501, "TSHWANE MARKET", "Farmers Trust", 3, 300.0, "2026-08-10",
                 product="PLUMS FORTUNE CLASS 2 LARGE (ECONOMIC PACK 8kg)")
    out = scorecard.build(ROWS + [plums], PAYS)
    assert [f["label"] for f in out["fruits"]] == ["Grapes"]
    assert out["fruits"][0]["clear"] == 1


def test_an_empty_book_is_an_empty_answer():
    assert scorecard.build([], []) == {"window": None, "fruits": [], "caveats": []}


# --- the Claude keys ------------------------------------------------------

def test_each_job_has_its_own_key_and_falls_back_to_the_shared_one(monkeypatch):
    from app import assistant
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_INTEL", "ANTHROPIC_API_KEY_DOCS"):
        monkeypatch.delenv(name, raising=False)
    assert not assistant.configured() and not assistant.docs_configured()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shared")
    assert assistant.api_key() == assistant.docs_api_key() == "shared"
    monkeypatch.setenv("ANTHROPIC_API_KEY_INTEL", "intel")
    monkeypatch.setenv("ANTHROPIC_API_KEY_DOCS", "docs")
    assert (assistant.api_key(), assistant.docs_api_key()) == ("intel", "docs")


def test_the_model_reads_the_comparison_it_was_given():
    """The written recommendation is built on the computed figures, verdict
    first, so the model is never left to work out a number itself."""
    from app import assistant
    text = assistant.where_context(scorecard.build(ROWS, PAYS))
    assert "send to TSHWANE MARKET via Farmers Trust" in text
    assert "R 92,00 more than DURBAN MARKET" in text
    assert "R 272,00 back per carton sent" in text


def test_the_written_recommendation_needs_a_key(monkeypatch):
    import pytest
    from app import assistant
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_INTEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(assistant.AssistantError):
        assistant.where_brief(ROWS, PAYS, 3)


def test_a_rejected_key_says_what_is_wrong_with_it_without_printing_it(monkeypatch):
    """A 401 is usually the wrong string in the box. The message has to be
    enough to spot that, and must never carry the key itself."""
    from app import assistant
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_INTEL", "ZACO_PROCURMENT")
    said = assistant.key_rejected()
    assert "does not look like an API key" in said and "ANTHROPIC_API_KEY_INTEL" in said
    assert "ZACO_PROCURMENT" not in said.replace("ANTHROPIC_API_KEY_INTEL", "")

    monkeypatch.setenv("ANTHROPIC_API_KEY_INTEL", "sk-ant-" + "x" * 95)
    said = assistant.key_rejected()
    assert "102 characters" in said and "x" * 10 not in said
