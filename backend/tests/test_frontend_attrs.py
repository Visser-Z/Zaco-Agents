"""Guards on the hand-written frontend that a Python test can still check.

The page builds HTML by string interpolation, so a value meant for a
double-quoted onclick attribute has to be escaped for HTML as well as
quoted for JavaScript. Getting that wrong closes the attribute early and
truncates the handler, which fails silently: the button renders, looks
right, and does nothing when clicked. That shipped once, so it is pinned
here.
"""

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_js_string_helper_escapes_for_the_attribute(source: str) -> None:
    """`q()` must escape, not just JSON-quote.

    JSON.stringify alone yields "NAME", whose opening quote ends the
    onclick attribute it sits in.
    """
    match = re.search(r"function q\(v\) \{ return ([^;]+); \}", source)
    assert match, "q() helper is gone or was reshaped; re-check its call sites"
    assert match.group(1).startswith("esc("), (
        "q() must wrap its JSON literal in esc(), or every handler built "
        f"with it is truncated at the first quote; found: {match.group(1)}"
    )


def test_no_inline_handler_interpolates_a_raw_json_literal(source: str) -> None:
    """Nothing may drop a bare JSON.stringify into an inline handler."""
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r'on\w+="[^"]*\$\{JSON\.stringify', line)
    ]
    assert not offenders, offenders
