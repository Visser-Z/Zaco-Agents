"""Parser for the "Payment Details" report.

This is the payment-side counterpart to the Daily Sales Detail sales report. It
is organised by AccSale Number, one block per commodity payout under a Supplier
Ref, and carries the money we need: Nett Payments, Deductions, VAT and Gross
Payments -- plus, per commodity line, the Delivered/Sold quantities and the
Sales Total. It covers a whole date range (a week/fortnight), not one day.

Its role: reconciled against the accumulated Daily Sales Details (summing Sales
Total per Supplier Ref + Product across the report's date range), it supplies
the Nett that the sales reports never print. See ``reconcile``.

Layout of one account-sale block::

     FMS ID   Supplier Ref AccSale Number Date   Nett Total Deduction Gross Pay.
     203464   20026*14585 PRE*BT*392828 2026-08-05 R 2,304.40 R 361.35 R 54.25 R 2,720.00 EFT
     Line No              Commodity                Delivered Sold     Sales Total
          NECTARINES OTHER CLASS 1 LARGE MULTI LAYER TRAYER 5.00 kg
    203464                                           43      10        R 2,720.00
                                                                       R 2,720.00
"""

from __future__ import annotations

import re

from .nett_adjustments import _acc_to_stmno as acc_to_stmno, _ref_to_dn as ref_to_dn

# --- format detection -----------------------------------------------------

_MARKERS = (
    re.compile(r"Report:\s*Payment\s+Details", re.I),
    re.compile(r"AccSale\s+Number", re.I),
    re.compile(r"Gross\s+Pay", re.I),
    re.compile(r"\bCommodity\b", re.I),
)


# The closing line every Payment Details report carries, whether or not it has
# any payments on it.
_GRAND_TOTAL = re.compile(r"Grand\s+Total", re.I)

_DATE_RANGE = re.compile(
    r"Date\s+Range\s*:\s*(\d{4})/(\d{2})/(\d{2})\s*-\s*(\d{4})/(\d{2})/(\d{2})", re.I
)


def date_range(text: str) -> tuple[str | None, str | None]:
    """The report's "Date Range: YYYY/MM/DD - YYYY/MM/DD" as ISO strings."""
    m = _DATE_RANGE.search(text)
    if not m:
        return None, None
    return f"{m[1]}-{m[2]}-{m[3]}", f"{m[4]}-{m[5]}-{m[6]}"


def is_payment_details(text: str) -> bool:
    """True if the text looks like the Payment Details report.

    Requires the report title so it is never confused with the Nett Payment
    Adjustments report, which shares some column names but has no commodity
    lines.

    A report for a day on which nothing was paid prints its header, a Grand
    Total of zero, and no column headings at all -- six of eight real August
    exports look like that. Requiring the column headings rejected those as
    unreadable, which tells the operator their file is broken when the truth is
    that the day was quiet. The title plus a grand total is enough.
    """
    if not _MARKERS[0].search(text):
        return False
    if sum(bool(p.search(text)) for p in _MARKERS) >= 3:
        return True
    return bool(_GRAND_TOTAL.search(text))


# --- parsing --------------------------------------------------------------

# The account-sale header row. Column order is Nett, Total Deductions, Deduction
# VAT, Gross Payments (money we keep: nett = g5, gross = g8).
_HEADER = re.compile(
    r"(?P<fms>\d{5,})\s+"
    # Any producer code, not just Zaco's own 20026: produce delivered on behalf
    # of another producer carries theirs (e.g. 14013*30559). Hardcoding 20026
    # made those headers invisible, so the account sale was never recognised
    # and its commodity lines were absorbed into the record above it.
    # The ref is not always a delivery number, and not always a number at all.
    # Some account sales carry a date there ("20026*03/8/2026"), and a Durban
    # delivery carries a bare letter ("20026*N"). Admitting only digits, spaces,
    # ampersands and slashes meant those headers did not match: the whole
    # payment was dropped and its commodity lines were absorbed into the record
    # above, inflating that record's lines well past its own gross. One August
    # file lost R7 200 that way, and September lost R126 580 on DUR*13*202568.
    # So the ref is any ordinary token; it is audit text, never a join key.
    r"(?P<ref>\d{3,8}\*[\w &/.\-]+?)\s+"
    r"(?P<acc>[A-Z]{2,}\*[A-Z0-9]+\*[A-Z0-9/]+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"R\s*(?P<nett>-?[\d,]+\.\d{2})\s+"
    r"R\s*(?P<deduct>-?[\d,]+\.\d{2})\s+"
    r"R\s*(?P<vat>-?[\d,]+\.\d{2})\s+"
    r"R\s*(?P<gross>-?[\d,]+\.\d{2})",
    re.M,
)

# Commodity lines come in two layouts depending on the export, and a file uses
# one or the other throughout. Both are tried; whichever yields lines wins.
#
# Wrapped: the name on its own line, then the FMS id and the numbers beneath.
#     NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER
#   203456                            71      6      R 760.00
_LINE_WRAPPED = re.compile(
    r"^[ \t]+(?P<product>[A-Z][^\n]*?)[ \t]*\n"
    r"[ \t]*\d{5,}\s+(?P<delivered>\d+)\s+(?P<sold>-?\d+)\s+R\s*(?P<total>-?[\d,]+\.\d{2})",
    re.M,
)
# Inline: a line number, then everything on one line.
#   01    GRAPES THOMPSON SEEDLESS CLASS 1 NO SIZE PUNNET 5.00 kg 10 10 R 4000.00
# The product itself contains numbers ("5.00 kg", "500 gms"), so the name is
# lazy and the three numeric fields are anchored to the end of the line.
_LINE_INLINE = re.compile(
    r"^[ \t]*\d{1,3}[ \t]+(?P<product>[A-Z].*?)[ \t]+"
    r"(?P<delivered>\d+)[ \t]+(?P<sold>-?\d+)[ \t]+R[ \t]*(?P<total>-?[\d,]+\.\d{2})[ \t]*$",
    re.M,
)
# A PDF wraps a commodity onto one line or two depending on how long the name
# is, so a single block can contain both shapes -- and a name that fits stays
# inline with or without the FMS id in front of it. Whichever produced the
# layout, the numbers are the last three fields on the line.
_LINE_INLINE_ANY = re.compile(
    # The prefix is either a short line number ("01") or the FMS id
    # ("6385669"), and on some rows there is none at all.
    r"^[ \t]*(?:\d{1,8}[ \t]+)?(?P<product>[A-Z][A-Za-z0-9 ,.()/'-]*?)[ \t]+"
    r"(?P<delivered>\d+)[ \t]+(?P<sold>-?\d+)[ \t]+R[ \t]*(?P<total>-?[\d,]+\.\d{2})[ \t]*$",
    re.M,
)


def _as_line(m: re.Match) -> dict:
    return {
        "product": " ".join(m.group("product").split()),
        "delivered": int(m.group("delivered")),
        "sold": int(m.group("sold")),
        "sales_total": _num(m.group("total")),
    }


def _commodity_lines(block: str) -> list[dict]:
    """Every commodity line in a block, whatever shape each one takes.

    Layouts are mixed WITHIN a block, not just between files: a long commodity
    name wraps onto its own line while a short one stays inline. Matching one
    shape and stopping silently drops the others -- in the real July export
    that lost R8 800 from a single account sale, with nothing to show it had
    happened. So both shapes are collected and merged by position, the wrapped
    form winning any overlap because it is the more specific match.
    """
    found: list[tuple[int, int, dict]] = [
        (m.start(), m.end(), _as_line(m)) for m in _LINE_WRAPPED.finditer(block)
    ]
    taken = [(s, e) for s, e, _ in found]
    for m in _LINE_INLINE_ANY.finditer(block):
        if any(s < m.end() and m.start() < e for s, e in taken):
            continue                      # already captured by the wrapped form
        found.append((m.start(), m.end(), _as_line(m)))
    found.sort(key=lambda x: x[0])
    return [line for _, _, line in found]


def _num(token: str) -> float:
    return float(token.replace(",", "").replace(" ", ""))


# Where a payment came from, read off the AccSale Number's own prefix.
#
# The headings in the report cannot answer this. Most of them carry no market
# at all ("Growfresh Port Natal (Dbn)", "Subtropico (Jhb)"), and the agent is
# spelled differently from the sales report for the same agency ("Growfresh
# Port Natal" there, "Grow Port Natal" here), so matching header text is
# unfixable in principle. The AccSale prefix is system-generated and exact.
#
# Keyed on the first two segments, and the names are the sales report's
# spellings so that a market means the same thing on both sides of the book.
# Two markets share the agent Subtropico, which is why nothing may ever group
# on the agent name alone.
DESTINATIONS = {
    ("DUR", "13"): ("DUR", "GPN", "DURBAN MARKET", "Grow Port Natal"),
    ("JOH", "MAR"): ("JOH", "MAR", "JOBURG MKT - BR / MAR", "Grow Marco"),
    ("JOH", "SUB"): ("JOH", "SUB", "JOBURG MKT - TFRESH", "Subtropico"),
    ("SPR", "SUB"): ("SPR", "SUB", "SPRINGS MARKET", "Subtropico"),
    ("PRE", "BT"): ("PRE", "BT", "TSHWANE MARKET", "Farmers Trust"),
}


def destination(accsale: str) -> dict:
    """The market and agent an account sale belongs to, from its prefix.

    An unrecognised prefix is not guessed at and never falls back to the
    heading above the block: the record says plainly that it does not know,
    carries the prefix it saw, and is quarantined rather than filed somewhere
    wrong. A payment under the wrong agent is worse than one nobody has placed.
    """
    parts = str(accsale or "").split("*")
    prefix = tuple(parts[:2])
    known = DESTINATIONS.get(prefix)
    if known is None:
        return {"market_code": None, "agent_code": None, "market": None,
                "market_agent": None, "unknown_prefix": "*".join(prefix)}
    market_code, agent_code, market, agent = known
    return {"market_code": market_code, "agent_code": agent_code,
            "market": market, "market_agent": agent, "unknown_prefix": None}


def parse_payment_details(pages: list[str], filename: str) -> list[dict]:
    """One record per account-sale block, with its commodity lines.

    Each record::

        {market_code, agent_code, market, market_agent, unknown_prefix,
         supplier_ref, dn, accsale, stm_no, date,
         nett, gross, deductions, vat,
         lines: [{product, delivered, sold, sales_total}, ...]}
    """
    text = "\n".join(pages)

    heads = list(_HEADER.finditer(text))
    bounds = [m.start() for m in heads] + [len(text)]
    out: list[dict] = []
    for i, h in enumerate(heads):
        block = text[h.end() : bounds[i + 1]]
        lines = _commodity_lines(block)
        out.append(
            {
                **destination(h.group("acc")),
                # Kept for audit only. It is operator-entered free text: one ref
                # covers several deliveries, and it is sometimes a date or a
                # single letter, so nothing may join on it.
                "supplier_ref": h.group("ref").strip(),
                "dn": ref_to_dn(h.group("ref")),
                "accsale": h.group("acc"),
                "stm_no": acc_to_stmno(h.group("acc")),
                "date": h.group("date"),
                "nett": _num(h.group("nett")),
                "gross": _num(h.group("gross")),
                "deductions": _num(h.group("deduct")),
                "vat": _num(h.group("vat")),
                "lines": lines,
            }
        )
    return out
