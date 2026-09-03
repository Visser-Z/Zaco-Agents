"""Reading from and appending to the account-sales workbook.

The workbook is a long-lived document the operator keeps adding to, so we open
whatever they give us, append beneath the existing rows, and hand the file back
-- never rebuilding it from scratch and never touching the formula columns.

Real workbooks do not always match the template exactly: the data sheet may not
be the first sheet, columns get reordered or inserted, and cells hold messy
values. So nothing here trusts positions. The data sheet and every column are
located by their *header text*, and cell values are coerced leniently -- a
surprise string in a numeric column becomes None on read, never a crash.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import pivot

from .columns import (
    COMPLETED_DEFAULT,
    DATA_COLUMNS,
    FIRST_DATA_ROW,
    FORMULA_COLUMNS,
    HEADER_ROW,
    HEADERS,
    NUMBER_FORMATS,
    SHEET_NAME,
)
from .schemas import StatementRow


class WorkbookFormatError(ValueError):
    """The uploaded file has no sheet we can recognise as account-sales data."""


# ------------------------------------------------------------- layout

def _norm(text) -> str:
    """Normalise a header cell for matching: case, spacing and punctuation."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


_EXPECTED = {_norm(h): letter for letter, h in HEADERS.items()}

# Headers whose columns the append/date logic cannot work without.
_REQUIRED = {"A", "E"}  # DN, STM No
# How many of the 20 template headers a row must match to count as the header
# row. High enough that a lone "Date" or "Price" in some other sheet never wins.
_MIN_MATCHES = 6
_HEADER_SCAN_ROWS = 10


@dataclass
class Layout:
    """Where the data actually lives in this particular workbook."""

    ws: Worksheet
    header_row: int
    letters: dict[str, str]  # template letter (A..T) -> actual column letter

    @property
    def first_data_row(self) -> int:
        return self.header_row + 1

    def col(self, template_letter: str) -> str | None:
        return self.letters.get(template_letter)

    def cell(self, template_letter: str, row: int):
        actual = self.letters.get(template_letter)
        return self.ws[f"{actual}{row}"] if actual else None


def _match_row(ws: Worksheet, row: int) -> dict[str, str]:
    """Template letter -> actual letter for every recognised header in `row`."""
    found: dict[str, str] = {}
    for col_idx in range(1, min(ws.max_column, 60) + 1):
        template = _EXPECTED.get(_norm(ws.cell(row=row, column=col_idx).value))
        if template and template not in found:
            found[template] = get_column_letter(col_idx)
    return found


def detect_layout(wb: Workbook) -> Layout:
    """Find the sheet and header row that hold the account-sales table.

    Every sheet's first few rows are scored by how many template headers they
    contain; the best match wins. This is what lets a workbook with extra
    sheets ("multiple pages"), reordered columns, or a title row above the
    table still open correctly.
    """
    best: tuple[int, Worksheet, int, dict[str, str]] | None = None
    for ws in wb.worksheets:
        for row in range(1, min(ws.max_row, _HEADER_SCAN_ROWS) + 1):
            found = _match_row(ws, row)
            if len(found) >= _MIN_MATCHES and _REQUIRED <= found.keys():
                score = len(found)
                if best is None or score > best[0]:
                    best = (score, ws, row, found)
        # A perfect match cannot be beaten; stop scanning further sheets.
        if best and best[0] == len(HEADERS):
            break

    if best is not None:
        _, ws, row, found = best
        return Layout(ws=ws, header_row=row, letters=found)

    # No header row matched. Many real sheets have none -- the operator knows the
    # A-T columns by position (Book1 is exactly this: data from row 1, no
    # headers). Fall back to the standard A-T layout, but only when the data
    # actually looks like account-sales rows: DN (A) and STM No (E) must be
    # numbers. That keeps an unrelated spreadsheet from being read as data.
    if (positional := _positional_layout(wb)) is not None:
        return positional

    names = ", ".join(wb.sheetnames)
    raise WorkbookFormatError(
        "Could not find the account-sales table in this workbook. "
        f"Sheets checked: {names}. Expected either a header row containing "
        '"DN" and "STM No", or data laid out in the standard A-T columns with '
        "DN in column A and STM No in column E."
    )


_IDENTITY_LETTERS = {letter: letter for letter in HEADERS}


def _positional_layout(wb: Workbook) -> "Layout | None":
    """Standard A-T layout for a header-less sheet, if the data looks like it.

    Finds the first row (across sheets) where columns A and E both hold numbers
    and treats that as the first data row -- so a stray title line above the
    data is skipped, and a genuine header row (text in A) is never mistaken for
    data.
    """
    for ws in wb.worksheets:
        for row in range(1, min(ws.max_row, _HEADER_SCAN_ROWS) + 1):
            a = _to_int(ws[f"A{row}"].value)
            e = _to_int(ws[f"E{row}"].value)
            if a is not None and e is not None:
                return Layout(ws=ws, header_row=row - 1, letters=dict(_IDENTITY_LETTERS))
    return None


# ------------------------------------------------------------ coercion
# Cells in real workbooks hold whatever Excel let someone type. Reads must
# never crash on them; a value that does not fit its column becomes None.

def _to_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    try:
        return int(round(float(str(value).replace(",", ".").replace(" ", ""))))
    except (ValueError, TypeError):
        return None


def _to_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _to_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


_COERCE = {
    "dn": _to_int,
    "stm_no": _to_int,
    "qty_received": _to_int,
    "opening_stock": _to_int,
    "cartons_sold": _to_int,
    "price": _to_float,
    "nett_total": _to_float,
    "market_agent": _to_str,
    "description": _to_str,
    "completed": _to_str,
    "status": _to_str,
    "notes": _to_str,
    "date": _as_date,
}


# ------------------------------------------------------------- reading

def _is_blank(layout: Layout, row: int) -> bool:
    """A row counts as blank if none of the *data* columns hold a value.

    The template pre-fills formulas far below the last real row, so testing
    ``ws.max_row`` alone would append into empty formula rows.
    """
    for template_letter in DATA_COLUMNS:
        cell = layout.cell(template_letter, row)
        if cell is not None and cell.value not in (None, ""):
            return False
    return True


def first_free_row(layout: Layout) -> int:
    row = layout.first_data_row
    while row <= layout.ws.max_row and not _is_blank(layout, row):
        row += 1
    return row


def read_rows(wb: Workbook) -> list[StatementRow]:
    """Read back the rows already in a workbook, for display in the UI."""
    layout = detect_layout(wb)
    rows: list[StatementRow] = []
    for r in range(layout.first_data_row, first_free_row(layout)):
        values: dict[str, object] = {}
        for template_letter, field in DATA_COLUMNS.items():
            cell = layout.cell(template_letter, r)
            values[field] = _COERCE[field](cell.value) if cell else None
        values["completed"] = values.get("completed") or COMPLETED_DEFAULT
        rows.append(StatementRow(source_file="(already in workbook)", **values))
    return rows


# ------------------------------------------------------------ appending

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "data" / "template.xlsx"


def new_workbook() -> Workbook:
    """Create an empty workbook.

    Seeded from `data/template.xlsx` when it exists, so a new workbook carries
    the real template's header styling, column widths and pre-filled formula
    rows rather than bare text. Falls back to writing plain headers.

    Headers are written here and only here -- `append_rows` never touches the
    header row, so appending into an existing workbook can never duplicate it.
    """
    if TEMPLATE_PATH.is_file():
        return load_workbook(TEMPLATE_PATH)

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    for col, header in HEADERS.items():
        ws[f"{col}{HEADER_ROW}"] = header
    return wb


def reconcile_group_dates(layout: Layout, rows: list[StatementRow]) -> None:
    """Make the Date column agree across a DN group that spans several appends.

    Column D is the earliest DATE RECEIVED for a delivery. When statements for
    one DN arrive in separate rounds, the rows already in the sheet and the rows
    about to be written must settle on the same, earliest date -- so we take the
    minimum across both and rewrite the existing cells when the new batch turns
    out to be older.
    """
    if layout.col("D") is None:
        return
    last = first_free_row(layout)
    dns = {r.dn for r in rows if r.dn is not None}

    for dn in dns:
        existing_rows = [
            r
            for r in range(layout.first_data_row, last)
            if (c := layout.cell("A", r)) is not None and _to_int(c.value) == dn
        ]
        candidates: list[date] = []
        for r in existing_rows:
            if (d := _as_date(layout.cell("D", r).value)) is not None:
                candidates.append(d)
        for item in rows:
            if item.dn != dn:
                continue
            for d in (item.date, item.date_received):
                if d is not None:
                    candidates.append(d)
                    break

        if not candidates:
            continue
        earliest = min(candidates)

        for item in rows:
            if item.dn == dn:
                item.date = earliest
        for r in existing_rows:
            cell = layout.cell("D", r)
            if _as_date(cell.value) != earliest:
                cell.value = datetime(earliest.year, earliest.month, earliest.day)


def write_order(rows: list[StatementRow]) -> list[StatementRow]:
    """The order rows belong in on the sheet: DN ascending, groups kept whole.

    Rows arrive in the order the files were read, so a round of four weekly
    exports interleaves them: on a real multi-batch sample the DNs came out
    ``14828, 14815, 14815, ... 14799, 14776, 14828 ...`` and **6 of 9 DNs were
    split into separate blocks** further down the sheet. That is the grouping
    fault. It is not a broken group-by, it is the absence of a sort: nothing ever
    ordered them, so file order won.

    Sorted **numerically**, not as text, or 14 815 would follow 148. Within a DN
    the rows stay with their product and then run in settlement order, so a
    consignment's running stock reads down the page the way the balance moves:
    opening 120, sold 115, then opening 5.

    Rows with no DN cannot be grouped with anything, so they go last rather than
    being pooled under a shared blank.
    """
    def key(r: StatementRow) -> tuple:
        return (
            r.dn is None, r.dn or 0,                       # numeric, blanks last
            (r.description or r.product or "").upper(),    # one product together
            r.invoice_date or r.last_sale or date.max,     # settlement order
            r.stm_no if r.stm_no is not None else 1 << 62,
        )
    return sorted(rows, key=key)


def append_rows(wb: Workbook, rows: list[StatementRow], market_agent: str) -> int:
    """Append rows beneath the existing data. Returns the number written.

    Note this sorts the batch being appended. Rows already in the sheet are left
    exactly where they are: re-ordering them would mean rewriting the operator's
    existing data and its formulas, which is not something a save should do.
    """
    layout = detect_layout(wb)
    reconcile_group_dates(layout, rows)
    row_no = first_free_row(layout)
    ws = layout.ws

    for item in write_order(rows):
        item.market_agent = item.market_agent or market_agent
        for template_letter, field in DATA_COLUMNS.items():
            actual = layout.col(template_letter)
            if actual is None:
                continue  # this workbook simply lacks the column; skip it
            value = getattr(item, field, None)
            if field == "completed":
                value = value or COMPLETED_DEFAULT
            if isinstance(value, date) and not isinstance(value, datetime):
                value = datetime(value.year, value.month, value.day)
            ws[f"{actual}{row_no}"] = value

        # Re-write the template formulas for this row, re-anchored to wherever
        # the columns they reference actually sit in this workbook.
        for template_letter, template in FORMULA_COLUMNS.items():
            actual = layout.col(template_letter)
            if actual is None:
                continue
            refs = {tl: layout.col(tl) for tl in HEADERS if layout.col(tl)}
            try:
                ws[f"{actual}{row_no}"] = template.format(r=row_no, **refs)
            except KeyError:
                # A referenced column is missing from this workbook; leave the
                # cell empty rather than writing a formula pointing at nothing.
                continue

        for template_letter, fmt in NUMBER_FORMATS.items():
            actual = layout.col(template_letter)
            if actual:
                ws[f"{actual}{row_no}"].number_format = fmt

        row_no += 1

    return len(rows)


def write_netts(wb: Workbook, netts: dict[int, float]) -> int:
    """Write reconciled Netts into rows already in the workbook, matched by
    STM No (column E) -> Nett (column N). Returns how many rows were updated.

    This is how a Payment Details reconciliation reaches rows that were saved in
    an earlier round: the append path only adds new rows, so an in-place update
    by statement number is the way to fill their Nett after the fact.

    One account sale can span several rows, one per product. The payment is a
    single figure for the whole statement, so it is **split across those rows by
    their gross** (cartons sold x price, which is what column M computes) -- the
    same split the operator's own book uses. Writing the statement's whole Nett
    onto each row would multiply the money by the number of products on it.
    """
    layout = detect_layout(wb)
    if layout.col("E") is None or layout.col("N") is None:
        return 0
    fmt = NUMBER_FORMATS.get("N")

    # statement -> [(sheet row, its gross)]
    groups: dict[int, list[tuple[int, float]]] = {}
    for row in range(layout.first_data_row, first_free_row(layout)):
        cell_e = layout.cell("E", row)
        stm = _to_int(cell_e.value) if cell_e else None
        if stm is None or stm not in netts:
            continue
        sold = _to_float(c.value) if (c := layout.cell("J", row)) else None
        price = _to_float(c.value) if (c := layout.cell("L", row)) else None
        groups.setdefault(stm, []).append((row, (sold or 0.0) * (price or 0.0)))

    updated = 0
    for stm, members in groups.items():
        nett = netts[stm]
        total = sum(gross for _, gross in members)
        if len(members) == 1 or not total:
            # Nothing to split by, so an equal share is the only honest option.
            shares = [round(nett / len(members), 2)] * len(members)
        else:
            shares = [round(nett * (gross / total), 2) for _, gross in members]
        if (residual := round(nett - sum(shares), 2)):
            biggest = max(range(len(members)), key=lambda i: abs(members[i][1]))
            shares[biggest] = round(shares[biggest] + residual, 2)
        for (row, _), share in zip(members, shares):
            cell_n = layout.cell("N", row)
            cell_n.value = share
            if fmt:
                cell_n.number_format = fmt
            updated += 1
    return updated


def load(data: bytes) -> Workbook:
    return load_workbook(io.BytesIO(data))


def refresh_pivot(wb: Workbook) -> bool:
    """(Re)build the pivot sheet over whatever the data sheet now holds.

    Called after appending, so the pivot's source range covers the new rows.
    Best-effort: a pivot is a convenience, and failing to build one must never
    cost the operator their save.
    """
    try:
        layout = detect_layout(wb)
        return pivot.add_pivot(wb, layout.ws, layout.header_row, first_free_row(layout) - 1)
    except Exception:  # noqa: BLE001 -- the workbook matters, the pivot does not
        return False


def to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
