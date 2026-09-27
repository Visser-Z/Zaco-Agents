"""The second reading of a report, against what the parser made of it.

The model's part is stubbed here: what is being tested is the comparison, which
is where every judgement is made. None of it is left to the model.
"""

import pytest

from app import assistant, doc_check

PAID = [
    {"accsale": "DUR*13*202568", "gross": 126580.0, "nett": 108188.24},
    {"accsale": "DUR*13*203222", "gross": 51300.0, "nett": 43540.61},
]


def page(refs, amounts=None, totals=None, **extra):
    """What the checker says the document prints."""
    return {"kind": "payments", "references": refs,
            "amounts": amounts if amounts is not None else [],
            "printed_totals": totals or {}, **extra}


def test_a_block_the_parser_never_read_is_the_headline_finding():
    """The real failure: the payment parser matched one shape of supplier
    reference and silently dropped every 20026*N block with it."""
    seen = page(["DUR*13*202568", "DUR*13*203222", "DUR*13*203587"])
    out = doc_check.compare(seen, PAID, "payments")
    assert out["ok"] is False
    first = out["findings"][0]
    assert first["kind"] == "missing" and first["ref"] == "DUR*13*203587"
    assert "nothing was read from it" in first["message"]


def test_everything_agreeing_is_said_plainly():
    seen = page([r["accsale"] for r in PAID],
                [{"ref": r["accsale"], "gross": f"{r['gross']:.2f}", "nett": f"{r['nett']:.2f}"}
                 for r in PAID],
                {"gross": "177880.00", "nett": "151728.85"})
    out = doc_check.compare(seen, PAID, "payments")
    assert out["ok"] is True and out["findings"] == []
    assert out["checked"]["references_on_page"] == 2
    assert out["checked"]["amounts_checked"] == 2
    assert out["checked"]["totals"]["nett"] == {"document": 151728.85, "read": 151728.85}


def test_a_figure_read_wrong_is_named_with_both_readings():
    seen = page([r["accsale"] for r in PAID],
                [{"ref": "DUR*13*202568", "gross": "126 580,00", "nett": "108 188,24"},
                 {"ref": "DUR*13*203222", "gross": "51 300,00", "nett": "43 540,61"}])
    wrong = [dict(PAID[0]), dict(PAID[1], nett=43_540.00)]
    out = doc_check.compare(seen, wrong, "payments")
    bad = [f for f in out["findings"] if f["kind"] == "mismatch"]
    assert len(bad) == 1
    assert bad[0]["field"] == "nett"
    assert bad[0]["document"] == 43540.61 and bad[0]["read"] == 43540.0


def test_a_total_that_does_not_add_up_is_caught_even_when_every_line_agrees():
    """The document's own grand total against the sum of what was read: the
    check that finds a whole block missing without knowing which one."""
    seen = page([r["accsale"] for r in PAID], [],
                {"nett": "251 728,85"})
    out = doc_check.compare(seen, PAID, "payments")
    total = [f for f in out["findings"] if f["kind"] == "total"][0]
    assert total["document"] == 251728.85 and total["read"] == 151728.85
    assert "out by 100,000.00" in total["message"]


def test_something_read_that_the_document_does_not_show():
    seen = page(["DUR*13*202568"], [], {})
    out = doc_check.compare(seen, PAID, "payments")
    extra = [f for f in out["findings"] if f["kind"] == "extra"]
    assert [f["ref"] for f in extra] == ["DUR*13*203222"]


def test_a_truncated_reading_never_accuses_the_parser_of_inventing_rows():
    """Only part of a long document is read back. Anything the parser has that
    the checker did not see is the checker's blind spot, not a finding."""
    seen = page(["DUR*13*202568"], [], {}, truncated=True)
    out = doc_check.compare(seen, PAID, "payments")
    assert not [f for f in out["findings"] if f["kind"] == "extra"]
    assert out["checked"]["truncated"] is True


def test_a_sales_report_is_matched_on_its_delivery_identifiers():
    rows = [{"delivery_id": 1855491, "product": "GRAPES SUGRAONE", "sales_total": 4860.0},
            {"supplier_ref": 14631, "product": "GRAPES RALLI", "sales_total": 3150.0}]
    seen = {"kind": "sales", "references": ["1855491", "14631", "1856701"],
            "market": "DURBAN MARKET", "agent": "Grow Port Natal"}
    out = doc_check.compare(seen, rows, "sales")
    assert [f["ref"] for f in out["findings"]] == ["1856701"]
    assert out["market"] == "DURBAN MARKET"


def test_references_compare_on_what_carries_the_meaning():
    """A PDF's text layer and a person type the same reference differently."""
    seen = page([" dur*13*202568 ", "DUR*13*203222"])
    out = doc_check.compare(seen, PAID, "payments")
    assert out["ok"] is True


@pytest.mark.parametrize("printed,expected", [
    ("126 580,00", 126580.0),
    ("126,580.00", 126580.0),
    ("R 1 234,56", 1234.56),
    ("(105,12)", -105.12),
    ("-105.12", -105.12),
    ("126580", 126580.0),
    ("", None),
    (None, None),
    ("n/a", None),
])
def test_a_figure_is_read_however_the_export_printed_it(printed, expected):
    assert doc_check._num(printed) == expected


def test_the_answer_is_read_out_of_whatever_the_model_wrapped_it_in():
    assert doc_check._as_json('```json\n{"kind": "payments"}\n```')["kind"] == "payments"
    assert doc_check._as_json('Here it is: {"kind": "sales"} ')["kind"] == "sales"
    with pytest.raises(assistant.AssistantError):
        doc_check._as_json("I could not read that document.")


def test_an_unrecognised_document_is_not_reported_as_a_clean_check(monkeypatch):
    monkeypatch.setattr(doc_check, "read_document", lambda text: {"kind": "unknown"})
    out = doc_check.check("some other pdf", PAID, "payments")
    assert out["ok"] is None and out["findings"] == []
    assert "did not recognise" in out["note"]


def test_the_check_says_which_key_it_needs(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY_DOCS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert doc_check.configured() is False
    assert "ANTHROPIC_API_KEY_DOCS" in doc_check.NOT_SET_UP
    with pytest.raises(assistant.AssistantError):
        doc_check.read_document("anything")


def test_the_model_is_told_to_copy_and_never_to_calculate():
    """The whole design rests on this: it reads, Python compares."""
    assert "Do not add anything up" in doc_check.SYSTEM
    assert "You are NOT shown what the computer read" in doc_check.SYSTEM
    assert "JSON only" in doc_check.SYSTEM


# --- the whole path, over a real document ---------------------------------

def test_the_check_runs_over_what_the_parser_actually_produces():
    """The fixture that caused this feature: a September Payment Details
    report carrying both the 20026*NNNNNNN and the 20026*N supplier refs.

    Only the model call is stubbed. Everything else is the production path:
    the real parser reads the document, and the comparison is the real one.
    """
    from pathlib import Path

    from app import payment_details

    text = (Path(__file__).resolve().parent / "fixtures"
            / "payment_details_sept_n_ref.txt").read_text(encoding="utf-8")
    parsed = payment_details.parse_payment_details([text], "sept.pdf")
    assert parsed, "the fixture should parse into payments"

    # The document, read back honestly: every account sale the parser found.
    honest = {"kind": "payments",
              "references": [r["accsale"] for r in parsed],
              "amounts": [{"ref": r["accsale"], "gross": r["gross"], "nett": r["nett"]}
                          for r in parsed],
              "printed_totals": {}}
    assert doc_check.compare(honest, parsed, "payments")["ok"] is True

    # The same document with one block the parser never read: exactly the
    # failure that put R 126 580,00 of Durban payments out of the book.
    dropped = dict(honest, references=honest["references"] + ["DUR*13*209999"])
    out = doc_check.compare(dropped, parsed, "payments")
    assert out["ok"] is False
    assert out["findings"][0]["ref"] == "DUR*13*209999"
    assert out["findings"][0]["kind"] == "missing"


def test_the_endpoint_says_what_is_missing_rather_than_failing(monkeypatch):
    """With no key set, the check refuses in a way that names the variable."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("ANTHROPIC_API_KEY_DOCS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        r = client.post("/api/documents/check",
                        files={"files": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY_DOCS" in r.json()["detail"]


def test_one_unreadable_file_does_not_cost_the_check_on_the_others(monkeypatch):
    """A round is several files. One that cannot be opened is reported against
    itself, and the rest are still checked."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ANTHROPIC_API_KEY_DOCS", "sk-ant-test")
    monkeypatch.setattr(doc_check, "check",
                        lambda text, parsed, kind: {"kind": kind, "ok": True, "findings": [],
                                                    "checked": {}})
    monkeypatch.setattr("app.main._document_text",
                        lambda name, data: (_ for _ in ()).throw(ValueError("not a PDF"))
                        if name == "broken.pdf" else ("text", ["text"]))
    monkeypatch.setattr("app.main._parse_for_check",
                        lambda name, text, pages: ("payments", []))
    with TestClient(app) as client:
        r = client.post("/api/documents/check", files=[
            ("files", ("broken.pdf", b"not a pdf", "application/pdf")),
            ("files", ("good.pdf", b"%PDF-1.4", "application/pdf")),
        ])
    body = r.json()
    assert r.status_code == 200
    assert body["checked"] == 1
    assert body["files"][0]["error"].startswith("Could not check this file")
    assert body["files"][1]["ok"] is True
