"""Product description lookup: full PDF product string -> short sheet code.

e.g. "NEOT 1L MA50 36 T2 NECTARINE OTHER" -> "IMP Nect"

This mapping cannot be derived from the statements themselves, so it is built
up by the operator: whenever an unseen product appears, the review screen asks
for its code and the answer is stored here for next time.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "description_lookup.json"
_lock = threading.Lock()


def normalise(product: str | None) -> str:
    """Match on collapsed whitespace and case so trivial spacing differences hit."""
    return " ".join((product or "").split()).upper()


def _read() -> dict[str, str]:
    if not _PATH.exists():
        return {}
    with _PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _write(data: dict[str, str]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)


def all_entries() -> dict[str, str]:
    return _read()


def codes() -> list[str]:
    """Distinct short codes, for the picker on the review screen."""
    return sorted(set(_read().values()))


# Keyword rules that pick a short code straight from the fruit named in the
# product string, so common products self-select without an exact lookup entry.
# Order matters -- the first rule whose keyword(s) all appear wins. These are
# starting defaults using the operator's own code style (from Book1); anything
# they correct on the review screen is saved to the lookup and wins next time.
_CLASSIFY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("GRAPE", "WHITE"), "Imp White Grapes"),
    (("GRAPE", "SUGRA"), "Imp White Grapes"),
    (("GRAPE", "PRIME"), "Imp White Grapes"),
    (("GRAPE", "THOMPSON"), "Imp White Grapes"),
    (("GRAPE", "CRIMSON"), "Imp Pink Grapes"),
    (("GRAPE", "RED"), "Imp Pink Grapes"),
    (("GRAPE", "FLAME"), "Imp Pink Grapes"),
    (("GRAPE", "PINK"), "Imp Pink Grapes"),
    (("NECTARINE",), "IMP Nect"),
    (("CHERR",), "Imp Cherries 5kg"),
    (("PLUM",), "Imp Plums"),
    (("APPLE",), "Imp Apples"),
    (("PEAR",), "Imp Pears"),
    (("PEACH",), "Imp Peaches"),
    (("ORANGE",), "Imp Oranges"),
    # Read off the operator's own book by joining its rows to the export that
    # names the product: strawberries are always "Strawberries" there, and there
    # is exactly one grapefruit code. Their other uncoded lines are deliberately
    # left out -- the book gives exotic citrus and granadillas two codes each,
    # and a grape whose name carries no colour cannot be told apart at all, so
    # those still go to the review screen rather than being guessed.
    (("STRAWBERR",), "Strawberries"),
    (("GRAPEFRUIT",), "Grapefruit 15kg"),
]


def classify(product: str | None) -> str | None:
    """A short code guessed from the fruit in the product string, or None."""
    if not product:
        return None
    text = normalise(product)
    for keywords, code in _CLASSIFY_RULES:
        if all(k in text for k in keywords):
            return code
    return None


def resolve(product: str | None) -> str | None:
    """Exact lookup first, then a keyword guess by fruit type."""
    if not product:
        return None
    return _read().get(normalise(product)) or classify(product)


def remember(product: str, code: str) -> None:
    with _lock:
        data = _read()
        data[normalise(product)] = code
        _write(data)
