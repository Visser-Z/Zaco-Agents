"""A second pair of eyes on the reports, before they become the book.

Everything in this app is read out of PDFs by regular expressions. That is the
right way to do it -- it is exact, it is testable, and it costs nothing -- but
it fails in one particular way: silently. A header whose format the pattern
does not cover is not an error, it is a block that never appears. That is not
hypothetical. The payment parser matched one shape of supplier reference and
quietly dropped every ``20026*N`` block with it, and nobody found out from the
app: the money simply was not there, and the app looked right.

So the document is read a second time, by a model, and the two readings are put
side by side. The model is asked for one thing only: the identifiers and the
totals THE DOCUMENT PRINTS. It never adds anything up, never corrects anything,
never sees what the parser found -- if it did, it would agree with it, which is
the one answer that is no use. Every comparison below is done in Python, on
figures, where it can be tested.

What it catches, in the order it matters: a block on the page that never became
a row, a row that came from nowhere, and a total that does not agree with the
lines under it. What it cannot catch is a document that is wrong about itself.
"""
from __future__ import annotations

import json
import re

from . import assistant

# The check reads identifiers and totals, not every line: a sales report runs to
# hundreds of rows, and transcribing them would cost more than it finds. The
# failures worth catching are whole blocks going missing and totals not adding
# up, and both of those show in the identifiers.
MAX_CHARS = 80_000
MAX_TOKENS = 4000

SYSTEM = """You are checking that a computer read a South African fresh-produce market report correctly. You are shown the text of the report. You are NOT shown what the computer read, and you must not guess at it.

Report ONLY what the document itself prints. Copy every figure and every reference exactly as it appears, character for character, including its punctuation. Do not add anything up. Do not convert, round, tidy or correct anything. Do not infer a value that is not printed. If something is not on the page, use null.

Two kinds of report:

PAYMENT DETAILS lists account sales that have been paid. For each one the document prints an account sale reference (like "DUR*13*202568" or "PRE*BT*395300"), a Gross and a Nett. Report every account sale reference on the page, with its Gross and Nett as printed.

DAILY SALES DETAIL lists what sold off each delivery. Each block carries a delivery or consignment identifier and the market and agent it belongs to. Report every delivery identifier on the page, and the market and agent names as printed.

Answer with JSON only, no other words, in exactly this shape:

{"kind": "payments" or "sales",
 "market": "as printed or null",
 "agent": "as printed or null",
 "references": ["every account sale reference, or every delivery identifier, in the order printed"],
 "amounts": [{"ref": "the reference", "gross": "as printed or null", "nett": "as printed or null"}],
 "printed_totals": {"gross": "the document's own grand total or null", "nett": "or null"},
 "pages_read": how many pages of text you were given}

If the text is not a market report at all, answer {"kind": "unknown"} and nothing else."""


def _num(value) -> float | None:
    """A figure as printed, however it was printed.

    The exports are inconsistent about it and the model copies what it sees, so
    "126 580,00", "126,580.00" and "126580" all have to come back as the same
    number. A minus sign, or brackets, means a credit either way.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    text = re.sub(r"[^\d,.]", "", text)
    if not text:
        return None
    # Whichever separator comes last is the decimal one; the other groups.
    comma, dot = text.rfind(","), text.rfind(".")
    if comma > dot:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        out = float(text)
    except ValueError:
        return None
    return -out if negative and out > 0 else out


def normalise_ref(ref) -> str:
    """One spelling of a reference, so the two readings can be compared.

    Case and inner spaces vary between a PDF's text layer and what anyone
    types; the characters that carry the meaning do not.
    """
    return re.sub(r"\s+", "", str(ref or "")).upper()


def read_document(text: str) -> dict:
    """What the document says, according to a second reader.

    One short call on the document's own text. The model is given no part of
    what the parser found, so agreement between the two means something.
    """
    import anthropic

    key = assistant.docs_api_key()
    if not key:
        raise assistant.AssistantError(NOT_SET_UP)
    body = text[:MAX_CHARS]
    client = anthropic.Anthropic(api_key=key)
    model_id = assistant.model()
    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": body}],
        )
    except anthropic.AuthenticationError as exc:
        raise assistant.AssistantError(key_rejected()) from exc
    except anthropic.NotFoundError as exc:
        raise assistant.AssistantError(
            f"The model {model_id!r} is not available to this API key.") from exc
    except anthropic.RateLimitError as exc:
        raise assistant.AssistantError(
            "The document checker is busy right now. Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise assistant.AssistantError("Could not reach the document checker.") from exc
    except anthropic.APIError as exc:  # noqa: BLE001 -- surfaced, never swallowed
        raise assistant.AssistantError(f"The document checker failed: {exc}") from exc

    answer = "\n".join(b.text for b in response.content if b.type == "text").strip()
    seen = _as_json(answer)
    seen["truncated"] = len(text) > MAX_CHARS
    return seen


def _as_json(answer: str) -> dict:
    """The model's answer as data, whatever it wrapped it in."""
    text = answer.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise assistant.AssistantError(
                "The document checker did not answer with a reading of the document.")
        try:
            out = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise assistant.AssistantError(
                "The document checker's reading could not be understood.") from exc
    return out if isinstance(out, dict) else {}


NOT_SET_UP = (f"The document check is not set up yet: add {assistant.DOCS_KEY} "
              f"(or {assistant.SHARED_KEY}) in Vercel under Settings, Environment Variables.")


def key_rejected() -> str:
    """The same diagnosis the assistant gives, named for this key."""
    import os

    key = assistant.docs_api_key() or ""
    where = assistant.DOCS_KEY if os.getenv(assistant.DOCS_KEY) else assistant.SHARED_KEY
    if not key.startswith(assistant.KEY_PREFIX):
        return (f"Anthropic refused the key. The value in {where} does not look like an API "
                f"key: it should start with \"{assistant.KEY_PREFIX}\" and this one does not.")
    return (f"Anthropic refused the key in {where} (it starts with "
            f"\"{assistant.KEY_PREFIX}\" and is {len(key)} characters). It may have been "
            f"revoked, pasted incompletely, or belong to an organisation this app is not "
            f"billed through.")


def compare(seen: dict, parsed: list[dict], kind: str) -> dict:
    """The document's reading against the parser's, figure by figure.

    Every judgement here is Python's. The model contributed what the page says;
    what that means is decided on numbers that a test can pin down.
    """
    findings: list[dict] = []
    doc_refs = [normalise_ref(r) for r in (seen.get("references") or []) if normalise_ref(r)]
    if kind == "payments":
        ours = {normalise_ref(r.get("accsale")): r for r in parsed if r.get("accsale")}
        label = "account sale"
    else:
        ours = {}
        for row in parsed:
            for field in ("delivery_id", "supplier_ref", "dn", "stm_no"):
                if (value := row.get(field)) not in (None, ""):
                    ours.setdefault(normalise_ref(value), row)
        label = "delivery"

    on_page = set(doc_refs)
    in_parse = set(ours)

    for ref in sorted(on_page - in_parse):
        findings.append({
            "kind": "missing",
            "ref": ref,
            "message": f"The document shows {label} {ref}, and nothing was read from it.",
        })
    # Only reported for payments: a sales report's identifiers are read off
    # blocks the checker may not have been shown if the text was truncated.
    if kind == "payments" and not seen.get("truncated"):
        for ref in sorted(in_parse - on_page):
            findings.append({
                "kind": "extra",
                "ref": ref,
                "message": f"{label.capitalize()} {ref} was read, and the document does "
                           f"not show it.",
            })

    amounts = {normalise_ref(a.get("ref")): a for a in (seen.get("amounts") or [])
               if isinstance(a, dict)}
    for ref, printed in amounts.items():
        row = ours.get(ref)
        if row is None:
            continue
        for field in ("gross", "nett"):
            page = _num(printed.get(field))
            mine = _num(row.get(field))
            if page is None or mine is None:
                continue
            if abs(page - mine) > 0.01:
                findings.append({
                    "kind": "mismatch",
                    "ref": ref,
                    "field": field,
                    "document": page,
                    "read": mine,
                    "message": f"{label.capitalize()} {ref}: the document prints "
                               f"{field} {page:,.2f}, and {mine:,.2f} was read.",
                })

    totals = seen.get("printed_totals") or {}
    checked_totals = {}
    for field in ("gross", "nett"):
        page = _num(totals.get(field))
        if page is None:
            continue
        mine = round(sum(_num(r.get(field)) or 0.0 for r in parsed), 2)
        checked_totals[field] = {"document": page, "read": mine}
        if abs(page - mine) > 0.05:
            findings.append({
                "kind": "total",
                "field": field,
                "document": page,
                "read": mine,
                "message": f"The document's own {field} total is {page:,.2f}; what was "
                           f"read adds up to {mine:,.2f}, out by {page - mine:,.2f}.",
            })

    order = {"missing": 0, "extra": 1, "mismatch": 2, "total": 3}
    findings.sort(key=lambda f: (order.get(f["kind"], 9), f.get("ref") or ""))
    return {
        "kind": kind,
        "ok": not findings,
        "findings": findings,
        "checked": {
            "references_on_page": len(on_page),
            "references_read": len(in_parse),
            "amounts_checked": len([a for a in amounts if a in ours]),
            "totals": checked_totals,
            "truncated": bool(seen.get("truncated")),
        },
        "market": seen.get("market"),
        "agent": seen.get("agent"),
    }


def check(text: str, parsed: list[dict], kind: str) -> dict:
    """Read the document again, and say where the two readings differ."""
    seen = read_document(text)
    if seen.get("kind") == "unknown":
        return {"kind": kind, "ok": None, "findings": [], "checked": {},
                "note": "The checker did not recognise this as a market report, so nothing "
                        "was compared."}
    return compare(seen, parsed, kind)


def configured() -> bool:
    return assistant.docs_configured()
