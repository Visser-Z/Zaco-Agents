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


# --- the request each model is sent ---------------------------------------
# Haiku 4.5 and the newer families take thinking in different, mutually
# exclusive forms. The assistant used to send the adaptive form with an effort
# level unconditionally, which Haiku rejects, so every question failed the
# moment the model was switched. Nothing pinned the request shape until now.

def test_haiku_is_sent_a_request_it_accepts(stub, monkeypatch):
    monkeypatch.delenv("ZACON_ASSISTANT_MODEL", raising=False)
    client = stub()
    asyncio.run(assistant.analyse([_row()]))
    assert client.calls
    for kw in client.calls:
        assert kw["model"] == "claude-haiku-4-5"
        assert "output_config" not in kw, "Haiku 4.5 rejects an effort level"
        assert "betas" not in kw and "fallbacks" not in kw
        thinking = kw.get("thinking")
        assert thinking in (None, {"type": "enabled",
                                   "budget_tokens": assistant.THINKING_BUDGET})
        if thinking:
            assert thinking["budget_tokens"] < kw["max_tokens"]
    # The four narrow analysts run without thinking; only the synthesis thinks.
    assert sum(1 for kw in client.calls if kw.get("thinking")) == 1


def test_switching_the_model_reshapes_the_request(stub, monkeypatch):
    """Changing ZACON_ASSISTANT_MODEL must not break the call: Opus 5 rejects a
    fixed budget as firmly as Haiku rejects adaptive thinking."""
    monkeypatch.setenv("ZACON_ASSISTANT_MODEL", "claude-opus-5")
    client = stub()
    asyncio.run(assistant.analyse([_row()]))
    for kw in client.calls:
        assert kw["model"] == "claude-opus-5"
        assert kw["thinking"] == {"type": "adaptive"}
        assert kw["output_config"]["effort"] in ("low", "medium")


def test_a_rejected_request_is_an_assistant_error_not_a_crash(monkeypatch):
    """A 400 used to escape the handler as a raw 500, because the retry after
    it re-sent the identical request from inside the except block."""
    import anthropic
    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ZACON_ASSISTANT_MODEL", raising=False)

    class _Rejecting:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=self._create)
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

        def _create(self, **kw):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                "thinking is not supported", response=httpx.Response(400, request=req), body=None)

    monkeypatch.setattr(anthropic, "Anthropic", _Rejecting)
    with pytest.raises(assistant.AssistantError):
        assistant.ask("What sold best?", [_row()])
