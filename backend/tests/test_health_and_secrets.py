"""What /api/health may say, and what must never leave the server.

/api/health is deliberately open -- the login screen reads it before anyone has
signed in -- so anything returned from it is public. The Anthropic key is read
on the server and used only in server-to-server calls; the endpoint may say
whether the assistant is configured and nothing more.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import app.main as main

# Built by concatenation so this file never itself contains a key-shaped string.
FAKE_KEY = "sk-" + "ant-" + "api03-" + "x" * 40
KEY_SHAPE = re.compile("sk-" + "ant-" + r"[A-Za-z0-9_\-]{20,}")


def test_health_never_returns_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    body = main.health()
    assert FAKE_KEY not in repr(body)
    assert body["assistant"] is True          # configured, said as a boolean


def test_health_says_when_the_assistant_is_not_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main.health()["assistant"] is False


def test_no_committed_file_carries_an_anthropic_key():
    """A key in a tracked file is a key published to the git history, and has
    to be rotated. .env is ignored; this catches the paste that is not."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    root = Path(__file__).resolve().parents[2]
    names = subprocess.run([git, "ls-files"], cwd=root, capture_output=True,
                           text=True, check=True).stdout.splitlines()
    leaks = []
    for name in names:
        path = root / name
        if path.suffix.lower() in {".png", ".jpg", ".pdf", ".xlsx", ".ico", ".exe", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if KEY_SHAPE.search(text):
            leaks.append(name)
    assert not leaks, f"an Anthropic key is committed in: {leaks}"


def test_health_reports_which_commit_is_serving(monkeypatch):
    """A push landing on the remote is not the same as a build serving it.
    Twice a fix was reported as deployed while the old build was still being
    served, so the commit is published and can be checked."""
    import app.config as cfg
    monkeypatch.setattr(cfg, "BUILD_SHA", "abc1234")
    assert main.health()["build"] == "abc1234"


def test_an_unknown_build_says_so_rather_than_guessing(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "BUILD_SHA", "")
    assert main.health()["build"] == "unknown"
