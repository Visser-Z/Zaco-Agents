"""Analyst-panel tests.

Several specialists examine the same history from different angles, then a
final pass weighs their findings. The API itself is stubbed, so what's under
test is the orchestration: that every analyst gets the FULL dataset (not a
slice), that they run concurrently, that one failing doesn't sink the panel,
and that the synthesis actually receives what they found.
"""

import asyncio
import types

import pytest

import app.assistant as assistant


def _row(**kw) -> dict:
    base = {
        "market_agent": "Farmers Trust", "market": "TSHWANE MARKET",
        "description": "IMP Nect", "cartons_sold": 10, "price": 50.0,
        "qty_received": 20, "group_date": "2026-07-27",
        "date_received": "2026-07-27", "last_sale": "2026-07-29",
    }
    base.update(kw)
    return base


class _Resp:
    """Minimal stand-in for a Messages response."""
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop_reason


class _StubClient:
    """Records every call so the orchestration can be inspected."""
    def __init__(self, fail_keys=(), refuse=False):
        self.calls = []
        self.fail_keys = fail_keys
        self.refuse = refuse
        self.concurrent = 0
        self.max_concurrent = 0
        outer = self

        class _Messages:
            async def create(self, **kw):
                outer.concurrent += 1
                outer.max_concurrent = max(outer.max_concurrent, outer.concurrent)
                try:
                    await asyncio.sleep(0)          # let siblings start
                    outer.calls.append(kw)
                    prompt = kw["messages"][0]["content"]
                    for key in outer.fail_keys:
                        if key in prompt:
                            raise RuntimeError("analyst blew up")
                    if outer.refuse:
                        return _Resp("", stop_reason="refusal")
                    return _Resp(f"FINDING<{prompt[:24]}>")
                finally:
                    outer.concurrent -= 1

        self.messages = _Messages()
        self.beta = types.SimpleNamespace(messages=_Messages())

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    made = {}

    def _factory(fail_keys=(), refuse=False):
        client = _StubClient(fail_keys, refuse)
        made["client"] = client
        import anthropic
        monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: client)
        return client

    return _factory


def test_panel_returns_findings_and_a_recommendation(stub):
    client = stub()
    out = asyncio.run(assistant.analyse([_row()]))
    assert [f["key"] for f in out["findings"]] == [a["key"] for a in assistant.ANALYSTS]
    assert all(f["finding"] for f in out["findings"])
    assert out["recommendation"].startswith("FINDING<")
    assert out["rows_considered"] == 1
    # One call per analyst, plus the synthesis.
    assert len(client.calls) == len(assistant.ANALYSTS) + 1


def test_every_analyst_sees_the_whole_dataset(stub):
    """The specialisation is in the question, not the evidence. Handing each
    analyst a slice would make them reason from a partial picture."""
    client = stub()
    rows = [_row(description="IMP Nect"), _row(description="Imp Plums"),
            _row(description="Imp Cherries 5kg")]
    asyncio.run(assistant.analyse(rows))
    for call in client.calls:
        blob = "\n".join(b["text"] for b in call["system"])
        for code in ("IMP Nect", "Imp Plums", "Imp Cherries 5kg"):
            assert code in blob, f"{code} missing from an analyst's context"


def test_analysts_run_concurrently(stub):
    """They must overlap, or the panel cannot finish inside a serverless request."""
    client = stub()
    asyncio.run(assistant.analyse([_row()]))
    assert client.max_concurrent >= len(assistant.ANALYSTS)


def test_one_failing_analyst_does_not_sink_the_panel(stub):
    client = stub(fail_keys=("price achieved per carton",))
    out = asyncio.run(assistant.analyse([_row()]))
    failed = [f for f in out["findings"] if f["failed"]]
    assert len(failed) == 1 and failed[0]["key"] == "price"
    assert out["recommendation"]                      # still advises
    # The synthesis is told only what actually came back.
    synth = client.calls[-1]["messages"][0]["content"]
    assert "What prices held up" not in synth
    assert "How stock moved" in synth


def test_synthesis_receives_the_findings(stub):
    client = stub()
    asyncio.run(assistant.analyse([_row()]))
    synth = client.calls[-1]["messages"][0]["content"]
    for a in assistant.ANALYSTS:
        assert a["title"] in synth


def test_all_analysts_failing_is_an_error_not_a_blank_answer(stub):
    stub(fail_keys=tuple(a["brief"][:20] for a in assistant.ANALYSTS))
    with pytest.raises(assistant.AssistantError, match="analysts"):
        asyncio.run(assistant.analyse([_row()]))


def test_refusal_is_surfaced_rather_than_returned_empty(stub):
    stub(refuse=True)
    with pytest.raises(assistant.AssistantError):
        asyncio.run(assistant.analyse([_row()]))


def test_no_history_is_a_clear_message(stub):
    stub()
    with pytest.raises(assistant.AssistantError, match="no saved sales"):
        asyncio.run(assistant.analyse([]))


def test_panel_needs_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(assistant.AssistantError, match="ANTHROPIC_API_KEY"):
        asyncio.run(assistant.analyse([_row()]))
