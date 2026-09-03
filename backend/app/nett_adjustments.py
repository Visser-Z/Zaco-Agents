"""Parser for the "Nett Payment Adjustments" report.

This is a third report format, separate from the sales reports (``extraction``
and ``daily_sales``). It carries no products or quantities -- it is a purely
financial summary: one line per account sale, giving its Gross and (after
deductions) Nett payment, keyed by the AccSale Number.

Its job here is not to create workbook rows but to supply the **Nett** figure
the Daily Sales report leaves blank. The endpoint matches these records onto
sales rows by statement number (column E) and fills in the Nett.

Layout of a data line (two market styles appear)::

    20026*14847 PRE*BT*387517 2026-07-01 R 2 700.00 R 401.72 R 60.27 R 2 238.01 ...
    20026*14565 & 14980 JOH*SUB*5644102/12026-07-13 R 6 000.00 R 771.56 ...

  Supplier Ref | AccSale Number | Date | Gross | Total Deductions | Ded. VAT | Nett | ...

Two gotchas handled below:
  * Subtropico's AccSale carries an adjustment sequence ("/1") and is printed
    glued to the date ("5644102/12026-07-13"), with no separating space.
  * One account sale can appear on several adjustment lines (…/1, …/2, …), so
    its true Nett and Gross are the SUM across those lines.
"""

from __future__ import annotations

import re

# --- format detection -----------------------------------------------------

_MARKERS = (
    re.compile(r"Nett\s+Payment\s+Adjustments", re.I),
    re.compile(r"AccSale\s+Number", re.I),
    re.compile(r"Nett\s+Payments", re.I),
    re.compile(r"Supplier\s+Ref", re.I),
)


def is_nett_adjustments(text: str) -> bool:
    """True if the text looks like the Nett Payment Adjustments report.

    The title is required. Three of the four markers below are column headings
    the Payment Details report also carries, so matching on those alone made
    every payment report answer yes -- and because the sales import tests for
    an adjustments report FIRST, a Payment Details PDF dropped there was read
    as adjustments and quietly contributed Netts nobody asked for.
    """
    if not _MARKERS[0].search(text):
        return False
    return sum(bool(p.search(text)) for p in _MARKERS) >= 2


# --- line parsing ---------------------------------------------------------

# Supplier Ref (starts with a digit, may contain '*', '&', spaces), then the
# AccSale Number (starts UPPERCASE*…, lazy up to the date), then the date --
# which may be glued straight onto the AccSale -- then the four money columns
# we care about. Trailing columns (Calc Nett / Adj / Nett Adjustment) are left
# unmatched.
_ROW = re.compile(
    r"^\s*(?P<ref>\d[\S ]*?)\s+"
    r"(?P<acc>[A-Z]{2,}\*\S*?)"
    r"\s*(?P<date>\d{4}-\d{2}-\d{2})"
    r"\s+R\s*(?P<gross>-?[\d ]+\.\d{2})"
    r"\s+R\s*(?P<deduct>-?[\d ]+\.\d{2})"
    r"\s+R\s*(?P<vat>-?[\d ]+\.\d{2})"
    r"\s+R\s*(?P<nett>-?[\d ]+\.\d{2})",
    re.M,
)


def _num(token: str) -> float:
    return float(token.replace(" ", ""))


def _acc_to_stmno(acc: str) -> int | None:
    """AccSale Number -> statement number: the digits of the last '*' segment,
    dropping any '/n' adjustment sequence. "PRE*BT*387517" -> 387517;
    "JOH*SUB*5644102/1" -> 5644102."""
    tail = acc.split("*")[-1].split("/")[0]
    digits = re.sub(r"\D", "", tail)
    return int(digits) if digits else None


def _ref_to_dn(ref: str) -> int | None:
    """Supplier Ref -> DN: the first number after the '*'. Combined refs like
    "20026*14565 & 14980" keep the first delivery number."""
    m = re.search(r"\*\s*(\d+)", ref)
    return int(m.group(1)) if m else None


def _ref_producer(ref: str) -> int | None:
    """The producer code in front of the '*': 20026 in "20026*14847".

    Worth keeping, because a ref whose two halves are the same number
    ("20026*20026") is a placeholder rather than a delivery reference.
    """
    m = re.match(r"\s*(\d+)\s*\*", ref or "")
    return int(m.group(1)) if m else None


def parse_nett_adjustments(pages: list[str], filename: str) -> dict[int, dict]:
    """Aggregate the report into ``{statement_number: {nett, gross, ...}}``.

    Nett and Gross are summed across a statement's adjustment lines, so a
    statement split over several dates resolves to one final figure.
    """
    text = "\n".join(pages)
    out: dict[int, dict] = {}
    for m in _ROW.finditer(text):
        stm = _acc_to_stmno(m.group("acc"))
        if stm is None:
            continue
        rec = out.setdefault(
            stm,
            {"stm_no": stm, "nett": 0.0, "gross": 0.0, "lines": 0, "dn": _ref_to_dn(m.group("ref"))},
        )
        rec["nett"] += _num(m.group("nett"))
        rec["gross"] += _num(m.group("gross"))
        rec["lines"] += 1
    for rec in out.values():
        rec["nett"] = round(rec["nett"], 2)
        rec["gross"] = round(rec["gross"], 2)
    return out
