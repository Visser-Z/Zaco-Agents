"""Guards on the hand-written frontend that a Python test can still check.

The page builds HTML by string interpolation, so a value meant for a
double-quoted inline handler has to be escaped for HTML as well as quoted
for JavaScript. Getting that wrong closes the attribute early and
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


def test_no_inline_handler_interpolates_a_raw_json_literal(source: str) -> None:
    """JSON.stringify yields "NAME", whose opening quote ends the attribute."""
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r'on\w+="[^"]*\$\{[^}]*JSON\.stringify', line)
    ]
    assert not offenders, offenders


def test_any_js_string_helper_escapes_for_the_attribute(source: str) -> None:
    """If a helper exists to quote a value for an inline handler, it escapes.

    Skips cleanly when there is no such helper, so removing the last
    caller does not leave a test failing for a rule nothing breaks.
    """
    match = re.search(r"function q\(v\) \{ return ([^;]+); \}", source)
    if match is None:
        pytest.skip("no inline-handler string helper in the page")
    assert match.group(1).startswith("esc("), (
        "q() must wrap its JSON literal in esc(), or every handler built "
        f"with it is truncated at the first quote; found: {match.group(1)}"
    )
