"""Label-matching extraction for fixed-format account-sales statements.

These PDFs are text reports, not scans: every value sits next to a stable label
(``ACCOUNT SALES NO``, ``QUANTITY RECEIVED``, ...) even though the values and
their exact positions shift between statements. So we match on labels rather
than coordinates, and flag anything we cannot resolve instead of guessing.

NOTE: the regexes below were written against the documented layout (see
``tests/fixtures/statement_387517.txt``). They must be validated against a real
PDF's ``pdfplumber`` output before this is trusted in production -- whitespace
between columns is the main thing that can differ.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

import pdfplumber

from .schemas import Flag, StatementRow

# --- label patterns -------------------------------------------------------

# "ACCOUNT SALES NO : 387517" but NOT "PREVIOUS ACCOUNT SALES NO : 386759"
_ACCOUNT_SALES_NO = re.compile(
    r"(?P<prev>PREVIOUS\s+)?ACCOUNT\s+SALES\s+NO\s*:\s*(?P<val>\d+)", re.I
)
# "DATE : 01/07/2026" -- the colon must follow DATE directly, which is what
# keeps this from matching "DATE RECEIVED :".
_INVOICE_DATE = re.compile(r"\bDATE\s*:\s*(\d{2}/\d{2}/\d{4})", re.I)
_DATE_RECEIVED = re.compile(r"DATE\s+RECEIVED\s*:\s*(\d{2}/\d{2}/\d{4})", re.I)
_REFNO = re.compile(r"\bREFNO\s*:\s*(\d+)", re.I)
_QTY_RECEIVED = re.compile(r"QUANTITY\s+RECEIVED\s*:\s*([\d\s,]+?)(?=\s{2,}|\s*QUANTITY|$)", re.I)
_QTY_BF = re.compile(r"QUANTITY\s+B/F\s*:\s*([\d\s,]+?)(?=\s{2,}|$)", re.I | re.M)
# PRODUCT runs to the SMAN label if present, otherwise to end of line.
# The real reports separate PRODUCT from SMAN with a single space, so this
# anchors on the "SMAN :" label itself rather than on column whitespace.
_PRODUCT = re.compile(r"PRODUCT\s*:\s*(?P<val>.+?)(?:\s+SMAN\s*:|\s*$)", re.I | re.M)
_NETT_AMOUNT = re.compile(r"NETT\s+AMOUNT\s+([\d\s,]*\d[.,]\d{2})", re.I)
# "** TOTAL SOLD **  54" -- the current statement's sold count, distinct from
# the cumulative "TOTAL SOLD : 154" further down the page.
_TOTAL_SOLD = re.compile(r"\*\*\s*TOTAL\s+SOLD\s*\*\*\s*(\d+)", re.I)
# "GROSS AMOUNT 2700.00" -- the statement's own gross figure. The sheet's
# Gross Total column is a formula (= cartons x price), so this is not written
# anywhere; it is used purely to cross-check that the extracted cartons and
# price multiply out to what the statement itself says.
_GROSS_AMOUNT = re.compile(r"GROSS\s+AMOUNT\s+([\d\s,]*\d[.,]\d{2})", re.I)

_PRICE_TABLE_HEADER = re.compile(r"AVER\.?\s*PRICE", re.I)
_PRICE_TABLE_END = re.compile(r"QUANTITY\s+OUTSTANDING", re.I)

# One statement can hold several products, each introduced by its own
# "MARKET GRN :" line and closed off by the next one (or by the cost summary
# that follows the last product).
_SECTION_START = re.compile(r"MARKET\s+GRN\s*:", re.I)
_SECTIONS_END = re.compile(r"VAT\s*\(OUTPUT\)\s*COLLECTED", re.I)

# A named deduction line in the COSTS table: name + amount + VAT + total.
# Anchored near the line start so the right-hand summary column ("GROSS
# AMOUNT 12880.00" etc.) can never be mistaken for a deduction; subtotal
# lines carry numbers but no name, so they never match either.
_DEDUCTION_LINE = re.compile(
    r"^\s{0,12}([A-Z][A-Z /&'.]*?[A-Z])\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
    re.M,
)
# Words that say what kind of charge it is rather than which product it hits.
_GENERIC_DEDUCTION_WORDS = {
    "LEVY", "LEVIES", "FEE", "FEES", "CHARGE", "CHARGES",
    "MARKET", "AGENT", "COMMISSION", "BANK", "VAT",
}
_DETAIL_LINE = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s")
_NUMBER = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _to_float(token: str) -> float:
    """Parse a report number. Thousands may be separated by spaces or commas."""
    return float(token.replace(" ", "").replace(",", ""))


def _to_int(token: str) -> int:
    return int(round(_to_float(token)))


def _parse_date(token: str) -> date:
    return datetime.strptime(token.strip(), "%d/%m/%Y").date()


def pdf_to_page_texts(pdf_bytes: bytes) -> list[str]:
    """Text of each page separately, preserving layout where we can."""
    out: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            # layout=True keeps column spacing, which the price table relies on.
            out.append(page.extract_text(layout=True) or "")
    return out


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Concatenate the text of every page."""
    return "\n".join(pdf_to_page_texts(pdf_bytes))


def _page_stm_no(page_text: str) -> int | None:
    """The page's own ACCOUNT SALES NO, ignoring the PREVIOUS variant."""
    for m in _ACCOUNT_SALES_NO.finditer(page_text):
        if not m.group("prev"):
            return int(m.group("val"))
    return None


def split_statements(pages: list[str]) -> list[str]:
    """Split a document's pages into one text block per statement.

    A single PDF can carry several account-sales statements back to back, and
    one statement can also run over more than one page ("Page 1/2"). Each page
    is claimed by the ACCOUNT SALES NO it prints; consecutive pages with the
    same number -- or with none, which marks a continuation page -- belong to
    the same statement. A document with no statement numbers at all is treated
    as one statement, so single-statement extraction keeps working even if the
    header were ever unreadable.
    """
    groups: list[tuple[int | None, list[str]]] = []
    for page in pages:
        if not page.strip():
            continue
        stm = _page_stm_no(page)
        if groups and (stm is None or stm == groups[-1][0]):
            groups[-1][1].append(page)
        elif groups and groups[-1][0] is None:
            # Pages before the first numbered statement (a cover sheet, say)
            # belong with the first statement that follows them.
            groups[-1] = (stm, groups[-1][1] + [page])
        else:
            groups.append((stm, [page]))
    return ["\n".join(pages) for _, pages in groups] or [""]


def _parse_price_table(
    text: str, flags: list[Flag]
) -> tuple[int | None, float | None, float | None]:
    """Return (cartons_sold, aver_price, value_total) from the price table.

    The table looks like::

        DATE       PRICES        AVER.PRICE  MARKET AVG   QUANTITY   VALUE
        29/06/2026 50.00 - 50.00   50.00       50.00         54      2700.00
                                   50.00                     54      2700.00

    Detail lines start with a date; the unlabelled summary line beneath carries
    exactly three numbers (aver price, quantity, value). We sum the detail rows
    and cross-check them against that summary.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _PRICE_TABLE_HEADER.search(ln)), None)
    if start is None:
        flags.append(Flag(field="cartons_sold", message="Price breakdown table not found."))
        return None, None, None

    end = next(
        (i for i in range(start, len(lines)) if _PRICE_TABLE_END.search(lines[i])),
        len(lines),
    )
    block = lines[start + 1 : end]

    qty_sum = 0
    value_sum = 0.0
    detail_rows = 0
    summary: list[float] | None = None

    for line in block:
        nums = _NUMBER.findall(line)
        if _DETAIL_LINE.match(line):
            # date + ... + QUANTITY + VALUE -- the last two numbers are what we need.
            if len(nums) >= 2:
                qty_sum += _to_int(nums[-2])
                value_sum += _to_float(nums[-1])
                detail_rows += 1
        elif len(nums) == 3 and not line.strip().startswith("-"):
            summary = [_to_float(n) for n in nums]

    if detail_rows == 0 and summary is None:
        flags.append(Flag(field="cartons_sold", message="No price rows found in the breakdown table."))
        return None, None, None

    if summary is not None:
        aver_price, summary_qty, summary_value = summary[0], int(summary[1]), summary[2]
        if detail_rows and summary_qty != qty_sum:
            flags.append(
                Flag(
                    field="cartons_sold",
                    severity="warning",
                    message=f"Price rows sum to {qty_sum} cartons but the table totals {summary_qty}.",
                )
            )
        if detail_rows and abs(summary_value - value_sum) > 0.01:
            flags.append(
                Flag(
                    field="price",
                    severity="warning",
                    message=f"Price rows sum to {value_sum:.2f} but the table totals {summary_value:.2f}.",
                )
            )
        return summary_qty, aver_price, summary_value

    # No summary line: fall back to the detail rows.
    aver = value_sum / qty_sum if qty_sum else None
    flags.append(
        Flag(
            field="price",
            severity="warning",
            message="No summary line in the price table; average price computed from the detail rows.",
        )
    )
    return qty_sum, aver, round(value_sum, 2)


def extract_statements(pdf_bytes: bytes, filename: str) -> list[StatementRow]:
    """Pull every row a PDF should produce, in document order.

    Two report formats are supported and auto-detected: the older account-sales
    statement (handled here) and the newer Daily Sales Detail consignment report
    (delegated to `daily_sales`). Detection is by the labels each format prints.

    For account-sales PDFs, two levels of splitting happen: a file may bundle
    several statements (each with its own ACCOUNT SALES NO), and each statement
    may list several products (each with its own MARKET GRN section). Every
    product becomes one row.
    """
    return statements_from_pages(pdf_to_page_texts(pdf_bytes), filename)


def statements_from_pages(pages: list[str], filename: str) -> list[StatementRow]:
    """Sales rows from already-extracted page texts.

    Split from `extract_statements` so a caller that has already read the PDF
    (e.g. to sniff its format) does not have to open it a second time.
    """
    # Newer consignment report -> dedicated parser.
    from . import daily_sales

    if daily_sales.is_daily_sales("\n".join(pages)):
        return daily_sales.parse_daily_sales(pages, filename)

    blocks = split_statements(pages)
    rows: list[StatementRow] = []
    for i, block in enumerate(blocks, start=1):
        name = filename if len(blocks) == 1 else f"{filename} (statement {i} of {len(blocks)})"
        rows.extend(extract_from_text(block, name))
    return rows


def extract_statement(pdf_bytes: bytes, filename: str) -> StatementRow:
    """Single-row convenience used by tests; the first row of the document."""
    return extract_from_text(pdf_to_text(pdf_bytes), filename)[0]


def _split_product_sections(text: str) -> list[str]:
    """One text block per MARKET GRN product section.

    The blocks run from each "MARKET GRN :" to the next, so a section whose
    price table continues over a page break stays intact -- the continuation
    lines sit before the next section starts. Statements with a single product
    (or an unexpected layout) come back as one block covering everything.
    """
    starts = [m.start() for m in _SECTION_START.finditer(text)]
    if not starts:
        return [text]
    end_m = _SECTIONS_END.search(text, starts[-1])
    end = end_m.start() if end_m else len(text)
    bounds = starts + [end]
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _parse_deductions(text: str) -> list[tuple[str, float]]:
    """The COSTS table's named lines as (name, total including VAT)."""
    return [(m.group(1).strip(), _to_float(m.group(4))) for m in _DEDUCTION_LINE.finditer(text)]


def _deduction_rows(name: str, rows: list[StatementRow]) -> list[int]:
    """Which rows a deduction applies to.

    Product levies name their fruit ("PLUMS LEVY", "NECTARIEN LEVY") in the
    market's own spelling, so the match is a conservative word-stem comparison
    against each row's product string. A deduction with no product word -- or
    one naming a product we cannot find -- spreads across every row.
    """
    tokens = [t for t in re.split(r"[^A-Z]+", name.upper()) if t and t not in _GENERIC_DEDUCTION_WORDS]
    if not tokens:
        return list(range(len(rows)))
    matched = []
    for i, r in enumerate(rows):
        words = (r.product or "").upper().split()
        for t in tokens:
            stem = t.rstrip("S")
            if len(stem) >= 4 and any(w[:5] == stem[:5] for w in words):
                matched.append(i)
                break
    return matched or list(range(len(rows)))


def _apportion_nett(
    rows: list[StatementRow],
    values: list[float],
    deductions: list[tuple[str, float]],
    statement_nett: float,
) -> bool:
    """Fill each row's nett by splitting the statement's own printed costs.

    No rates are re-derived: each printed deduction total is redistributed over
    the rows it applies to, proportional to their sales value -- so a plum levy
    lands only on the plum rows. Rounding is absorbed by the largest row so the
    shares always sum to the statement's printed NETT AMOUNT exactly. Returns
    False (writing nothing) when the numbers don't reconcile closely enough to
    trust.
    """
    total_value = sum(values)
    if not total_value or not deductions:
        return False

    shares = [0.0] * len(rows)
    for name, total in deductions:
        idx = _deduction_rows(name, rows)
        base = sum(values[i] for i in idx)
        if not base:
            idx, base = list(range(len(rows))), total_value
        for i in idx:
            shares[i] += total * values[i] / base

    netts = [round(v - s, 2) for v, s in zip(values, shares)]
    residual = round(statement_nett - sum(netts), 2)
    if abs(residual) > 1.00:
        # The printed deductions do not explain the printed nett; a levy or
        # charge we failed to parse would silently distort every row.
        return False
    biggest = max(range(len(netts)), key=lambda i: values[i])
    netts[biggest] = round(netts[biggest] + residual, 2)
    for r, n in zip(rows, netts):
        r.nett_total = n
    return True


def extract_from_text(text: str, filename: str) -> list[StatementRow]:
    """Parse one statement's text into one row per product section.

    Split out from `extract_statement` so the label matching can be tested
    against captured text without needing a PDF in the loop.

    Anything that cannot be matched becomes a Flag rather than an exception, so
    the review screen can show the operator exactly what needs attention.
    """
    statement_flags: list[Flag] = []

    def require(pattern: re.Pattern[str], field: str, label: str, src: str, flags: list[Flag], group=1):
        m = pattern.search(src)
        if not m:
            flags.append(Flag(field=field, message=f"Could not find “{label}” on the statement."))
            return None
        return m.group(group)

    # ---- statement-level fields, shared by every product row ----
    stm_no = next(
        (int(m.group("val")) for m in _ACCOUNT_SALES_NO.finditer(text) if not m.group("prev")),
        None,
    )
    if stm_no is None:
        statement_flags.append(Flag(field="stm_no", message="Could not find “ACCOUNT SALES NO”."))

    dn = _to_int(v) if (v := require(_REFNO, "dn", "REFNO", text, statement_flags)) else None
    nett = _to_float(v) if (v := require(_NETT_AMOUNT, "nett_total", "NETT AMOUNT", text, statement_flags)) else None
    date_received = _parse_date(v) if (v := require(_DATE_RECEIVED, "date_received", "DATE RECEIVED", text, statement_flags)) else None
    invoice_date = _parse_date(v) if (v := require(_INVOICE_DATE, "invoice_date", "DATE", text, statement_flags)) else None
    status = invoice_date.strftime("%d.%m") if invoice_date else None

    # ---- one row per product section ----
    sections = _split_product_sections(text)
    rows: list[StatementRow] = []
    section_values: list[float | None] = []
    for idx, section in enumerate(sections, start=1):
        flags = [f.model_copy() for f in statement_flags]
        row = StatementRow(
            source_file=filename if len(sections) == 1 else f"{filename} · product {idx} of {len(sections)}",
            stm_no=stm_no,
            dn=dn,
            date_received=date_received,
            invoice_date=invoice_date,
            status=status,
        )
        if (v := require(_QTY_RECEIVED, "qty_received", "QUANTITY RECEIVED", section, flags)) is not None:
            row.qty_received = _to_int(v)
        if (v := require(_QTY_BF, "opening_stock", "QUANTITY B/F", section, flags)) is not None:
            row.opening_stock = _to_int(v)
        if (v := require(_PRODUCT, "product", "PRODUCT", section, flags, group="val")) is not None:
            row.product = " ".join(v.split())
        cartons, price, value = _parse_price_table(section, flags)
        row.cartons_sold = cartons
        row.price = price
        section_values.append(value)
        row.flags = flags
        rows.append(row)

    # ---- NETT AMOUNT: one figure printed for the whole statement ----
    # Single product: it is that row's nett outright. Several products: the PDF
    # prints no per-product split, so we recreate it from the statement's own
    # COSTS table -- redistributing each printed deduction over the rows it
    # applies to (a plum levy onto plum rows only), proportional to sales value.
    # If that reconciles to the printed nett we use it; otherwise every row goes
    # in at 0 with a warning so the operator fills the shares in by hand.
    if len(sections) == 1:
        rows[0].nett_total = nett
    elif nett is not None:
        deductions = _parse_deductions(text)
        computed = (
            None not in section_values
            and _apportion_nett(rows, section_values, deductions, nett)
        )
        for r in rows:
            if computed:
                r.flags.append(
                    Flag(
                        field="nett_total",
                        severity="warning",
                        message=(
                            f"Estimated share of the statement's NETT AMOUNT "
                            f"({nett:,.2f}), split from its printed costs by sales "
                            "value. Check it against your own figures."
                        ),
                    )
                )
            else:
                r.nett_total = 0.0
                r.flags.append(
                    Flag(
                        field="nett_total",
                        severity="warning",
                        message=(
                            f"The statement's NETT AMOUNT ({nett:,.2f}) covers all "
                            f"{len(sections)} products; this row is set to 0. "
                            "Enter its share here or in the sheet later."
                        ),
                    )
                )
    else:
        for r in rows:
            r.nett_total = 0.0

    # ---- statement-level cross-checks, reported once on the first row ----
    first = rows[0]
    all_cartons = [r.cartons_sold for r in rows]
    if (m := _TOTAL_SOLD.search(text)) and all(c is not None for c in all_cartons):
        total_sold = int(m.group(1))
        if total_sold != sum(all_cartons):
            first.flags.append(
                Flag(
                    field="cartons_sold",
                    severity="warning",
                    message=f"Product tables total {sum(all_cartons)} cartons but TOTAL SOLD shows {total_sold}.",
                )
            )

    # The sheet's Gross Total column stays a formula (cartons x price); this
    # validates its inputs against the gross the statement itself printed.
    # Averaged prices are rounded to 2dp, so tolerance scales with volume.
    if (m := _GROSS_AMOUNT.search(text)) and all(
        r.cartons_sold is not None and r.price is not None for r in rows
    ):
        stated = _to_float(m.group(1))
        computed = sum(r.cartons_sold * r.price for r in rows)
        tolerance = 0.05 + 0.01 * sum(all_cartons)
        if abs(computed - stated) > tolerance:
            first.flags.append(
                Flag(
                    field="price",
                    severity="warning",
                    message=(
                        f"Cartons x price across the statement = {computed:,.2f} but its "
                        f"GROSS AMOUNT is {stated:,.2f}. Something was misread."
                    ),
                )
            )

    return rows


def _group_date(row: StatementRow) -> date | None:
    """The date column D should carry for a row.

    The day the load was sent where that is known, else when the market received
    it. The two differ by a day on the CSV exports -- the operator's book dates
    the load going out, the market dates booking it in -- and the book is what
    this column has to agree with. The account-sales PDF prints its own DATE
    RECEIVED, which is already that date, and is used unshifted.
    """
    return row.date_sent or row.date_received


def apply_group_dates(rows: list[StatementRow]) -> None:
    """Set column D to the earliest such date within each DN group.

    Statements sharing a DN belong to one delivery, so they all carry that
    delivery's first date -- even when individual statements arrived days apart.
    """
    earliest: dict[int, date] = {}
    for r in rows:
        own = _group_date(r)
        if r.dn is None or own is None:
            continue
        current = earliest.get(r.dn)
        if current is None or own < current:
            earliest[r.dn] = own
    for r in rows:
        if r.dn is not None:
            r.date = earliest.get(r.dn, _group_date(r))
        else:
            r.date = _group_date(r)
