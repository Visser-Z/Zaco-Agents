"""The agent's API key is a secret and must never reach the browser.

/api/health is deliberately open -- the login screen reads it before anyone has
signed in -- so anything returned from it is public. The key is read on the
server and used only in server-to-server calls; the endpoint may say whether it
is configured and nothing more.
"""

import app.config as config
import app.main as main


def test_health_never_returns_the_key(monkeypatch):
    monkeypatch.setattr(config, "MARKET_API_KEY", "sk-live-do-not-leak-me")
    monkeypatch.setattr(config, "MARKET_API_BASE", "https://agent.example/api")

    body = main.health()
    rendered = repr(body)
    assert "sk-live-do-not-leak-me" not in rendered
    assert "market_api_key" not in {k.lower() for k in body}
    assert body["market_api"] is True          # configured, said as a boolean


def test_health_says_when_the_api_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "MARKET_API_KEY", "")
    monkeypatch.setattr(config, "MARKET_API_BASE", "")
    assert main.health()["market_api"] is False


def test_readiness_needs_both_halves(monkeypatch):
    """A base with no key, or a key with no base, is not usable and must not
    report as ready -- that would send unauthenticated calls at the agent."""
    monkeypatch.setattr(config, "MARKET_API_BASE", "https://agent.example/api")
    monkeypatch.setattr(config, "MARKET_API_KEY", "")
    assert config.market_api_ready() is False

    monkeypatch.setattr(config, "MARKET_API_BASE", "")
    monkeypatch.setattr(config, "MARKET_API_KEY", "sk-live")
    assert config.market_api_ready() is False


def test_the_key_is_not_in_the_repo():
    """A key pasted into a tracked file is a key published to the git history."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    example = root / "backend" / ".env.example"
    if example.exists():
        text = example.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("MARKET_API_KEY"):
                assert line.strip() in ("MARKET_API_KEY=", "MARKET_API_KEY="), (
                    f".env.example must not carry a real key: {line!r}")


def test_health_reports_which_commit_is_serving(monkeypatch):
    """A push landing on the remote is not the same as a build serving it.
    Twice now a fix was reported as deployed while the old build was still
    being served, so the commit is published and can be checked."""
    import app.config as cfg
    monkeypatch.setattr(cfg, "BUILD_SHA", "abc1234")
    assert main.health()["build"] == "abc1234"


def test_an_unknown_build_says_so_rather_than_guessing(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "BUILD_SHA", "")
    assert main.health()["build"] == "unknown"
