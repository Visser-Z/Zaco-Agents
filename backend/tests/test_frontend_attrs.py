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


def test_switching_pages_clears_the_error_with_the_data(source: str) -> None:
    """Each page auto-loads on `data === null && !loading && error === null`.

    Clearing only `data` on a page switch left a page that had failed once
    unable to ever load again: Insights sat on "Could not load analytics"
    while Tracking, which had not failed, kept showing the same history.
    """
    body = re.search(r"function setModule\(m\) \{(.*?)\n\}", source, re.S)
    assert body, "setModule is gone or was reshaped"
    body = body.group(1)
    assert ".data = null" in body, "a page switch must still force a re-fetch"
    assert ".error = null" in body, (
        "setModule must clear the error alongside the data, or a page that "
        "failed once can never auto-load again"
    )


def test_tracking_delete_is_scoped_and_disarms_on_a_period_change(source: str) -> None:
    """Tracking can delete the period it is showing.

    Two things must hold. There is no delete-everything path, so the action
    refuses when nothing is scoped. And an open confirmation names the period
    it was opened for, so changing month or week has to close it rather than
    leave it armed against the new one.
    """
    body = re.search(r"async function confirmDeleteTracking\(\) \{(.*?)\n\}", source, re.S)
    assert body, "confirmDeleteTracking is gone or was reshaped"
    assert 'body.set("scope", "all")' in body.group(1), (
        "clearing the whole book must ask for it by name; a blank period is an "
        "error on the server, never a silent delete-all")

    for setter in ("setTrackingMonth", "setTrackingWeek"):
        fn = re.search(rf"function {setter}\(\w\) \{{(.*?)\n\}}", source, re.S)
        assert fn, f"{setter} is gone or was reshaped"
        assert "confirmDelete = false" in fn.group(1), (
            f"{setter} must close an open confirmation, or it stays armed "
            f"against the period the operator just switched away from")
