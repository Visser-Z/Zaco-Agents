"""The client-supplied DN hint. Untrusted input on a path that must not break."""

import app.main as main


def test_a_normal_map_is_read():
    assert main._book_dns('{"383735": 14815, "384423": 14776}') == {383735: 14815, 384423: 14776}


def test_nothing_supplied_is_not_an_error():
    assert main._book_dns(None) == {} and main._book_dns("") == {}


def test_rubbish_is_dropped_rather_than_raised():
    """It arrives from the browser. A malformed hint must never cost the operator
    the whole import, and it is only ever a hint: the flags still catch what it
    fails to answer."""
    assert main._book_dns("not json") == {}
    assert main._book_dns('["a", "list"]') == {}
    assert main._book_dns('{"x": "y"}') == {}
    assert main._book_dns('{"383735": null}') == {}
    # Zero is not an identity, here as everywhere else.
    assert main._book_dns('{"0": 14815, "383735": 0}') == {}
    # A good pair survives beside a bad one.
    assert main._book_dns('{"bad": "x", "383735": 14815}') == {383735: 14815}
