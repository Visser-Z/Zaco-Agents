"""A native Excel pivot table on its own sheet, built from the statement rows.

Not a flat pivot-shaped summary: a real one, so the operator can drag fields
around, change what is measured and drill in, without the app having to
anticipate the question.

**The cache is deliberately left empty and marked stale.** A pivot table in a
file has two halves: the table definition, and a cached copy of the source data
that Excel shows before it recalculates. Writing that cache here would mean
shipping a second copy of every figure, which goes out of date the moment a row
is appended and is exactly the "stale pivot cache" to avoid. Instead the cache is
flagged ``invalid`` with ``refreshOnLoad``, so Excel rebuilds it from the sheet
every time the workbook is opened. The pivot is therefore always as current as
the data, and there is only ever one copy of the truth.

The pivot is rebuilt on every export rather than edited in place, so it always
covers the full data range including rows just appended.
"""

from __future__ import annotations

from openpyxl.pivot.cache import (
    CacheDefinition,
    CacheField,
    CacheSource,
    SharedItems,
    WorksheetSource,
)
from openpyxl.pivot.table import (
    DataField,
    Location,
    PivotField,
    RowColField,
    TableDefinition,
)
from openpyxl.worksheet.worksheet import Worksheet

SHEET_NAME = "Pivot"
PIVOT_NAME = "ZacoAgents"

# What wrote this pivot, in the versions Excel expects to see. openpyxl defaults
# these to 0, which is not a version Excel recognises: it reads the file as
# damaged and offers to repair it, which is worse than having no pivot at all
# because it makes the operator distrust the whole workbook. 8 is Excel 2016
# onward; 3 is the oldest version able to refresh this shape.
CREATED_VERSION = 8
REFRESHABLE_FROM = 3

# Which of the sheet's own headers the pivot arranges by, and which it measures.
# Named by header text, resolved against the workbook's real layout, so a sheet
# whose columns sit in different places still produces a valid pivot.
ROW_HEADERS = ("Market Agent", "Description")
COLUMN_HEADERS = ("Completed",)
VALUE_HEADERS = ("Cartons Sold", "Nett Total")


def _headers(ws: Worksheet, header_row: int) -> list[str]:
    out: list[str] = []
    for cell in ws[header_row]:
        value = cell.value
        out.append(str(value).strip() if value not in (None, "") else "")
    # A trailing run of blank headers is not part of the table; a pivot source
    # range including them is rejected by Excel as a missing field name.
    while out and not out[-1]:
        out.pop()
    return out


def _col_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index + 1)


def add_pivot(wb, source_ws: Worksheet, header_row: int, last_row: int) -> bool:
    """(Re)create the pivot sheet from ``source_ws``. False if there is no data.

    Any previous pivot sheet is dropped first: editing one in place risks leaving
    a table whose range stops short of the rows just appended.
    """
    headers = _headers(source_ws, header_row)
    if not headers or last_row <= header_row:
        return False        # headers but no rows: a pivot over nothing

    if SHEET_NAME in wb.sheetnames:
        del wb[SHEET_NAME]

    fields = {h: i for i, h in enumerate(headers) if h}
    rows = [h for h in ROW_HEADERS if h in fields]
    cols = [h for h in COLUMN_HEADERS if h in fields]
    values = [h for h in VALUE_HEADERS if h in fields]
    if not rows or not values:
        return False        # nothing to arrange by, or nothing to measure

    ref = f"A{header_row}:{_col_letter(len(headers) - 1)}{last_row}"

    # The cache: field names only. No records, and marked stale on purpose.
    cache = CacheDefinition(
        invalid=True,
        refreshOnLoad=True,
        enableRefresh=True,
        recordCount=0,
        createdVersion=CREATED_VERSION,
        refreshedVersion=CREATED_VERSION,
        minRefreshableVersion=REFRESHABLE_FROM,
        cacheSource=CacheSource(
            type="worksheet",
            worksheetSource=WorksheetSource(ref=ref, sheet=source_ws.title),
        ),
        cacheFields=[
            CacheField(name=h or f"Column{i + 1}", sharedItems=SharedItems())
            for i, h in enumerate(headers)
        ],
    )

    # One PivotField per source column, saying what role it plays.
    pivot_fields = []
    for h in headers:
        if h in rows:
            pivot_fields.append(PivotField(axis="axisRow", showAll=False, compact=False, outline=False))
        elif h in cols:
            pivot_fields.append(PivotField(axis="axisCol", showAll=False, compact=False, outline=False))
        elif h in values:
            pivot_fields.append(PivotField(dataField=True, showAll=False))
        else:
            pivot_fields.append(PivotField(showAll=False))

    ws = wb.create_sheet(SHEET_NAME)
    # Enough room that the table's own header band never lands on row 1, which
    # Excel treats as a malformed location.
    table = TableDefinition(
        name=PIVOT_NAME,
        cacheId=cache.id or 1,
        dataCaption="Values",
        createdVersion=CREATED_VERSION,
        updatedVersion=CREATED_VERSION,
        minRefreshableVersion=REFRESHABLE_FROM,
        location=Location(
            ref=f"A3:{_col_letter(len(rows) + len(values))}20",
            firstHeaderRow=1,
            firstDataRow=2,
            firstDataCol=len(rows),
        ),
        pivotFields=pivot_fields,
        rowFields=[RowColField(x=fields[h]) for h in rows],
        # With more than one measured value, Excel puts the value names on the
        # column axis; field -2 is how that axis is spelled.
        colFields=([RowColField(x=fields[h]) for h in cols]
                   + ([RowColField(x=-2)] if len(values) > 1 else [])) or None,
        dataFields=[
            DataField(fld=fields[h], name=f"Sum of {h}", baseField=-1, baseItem=0)
            for h in values
        ],
        # Excel decides the layout on refresh; these keep it readable meanwhile.
        compact=False,
        compactData=False,
        outline=False,
        outlineData=False,
        useAutoFormatting=True,
        indent=0,
        gridDropZones=True,
    )
    table.cache = cache
    ws.add_pivot(table)
    return True
