"""Column map for the account-sales workbook (Sheet1, columns A-U).

The sheet ships with formulas already written into I, K, M, O, P, Q, R and S.
When we append a row we must re-write those formulas for the new row number --
we never write a computed value into a formula column.

The formula chain is the operator's own, read back off their real book. It hangs
on one number, **I Frui Price/crt**, the price per carton that is not Zaco's::

    O Nett Price/crt         = N / J          what the carton actually returned
    I Frui Price/crt         = O x 70%        the default split (see below)
    S Frui Curr Sales Value  = I x J
    P Z R/crt                = O - I          Zaco's cut per carton
    Q Z Total                = N - S          Zaco's cut on the row
    R % Markup               = P / O          which is the commission rate
    K Baby Stock             = H - J          what was left on the floor

Writing I as the 70% default is what makes the sheet agree with a 30%
commission, and it is a *formula*, so a row on different terms is corrected by
typing the agreed per-carton price over it -- everything downstream follows.
That is exactly how the historical book is kept: 268 of 339 rows sit at exactly
30%, and the rest carry a hand-entered round number in I.

Note the difference from settlement. Here the default is a visible, editable
formula in a spreadsheet. ``consignment.py`` deliberately refuses to compute
what a supplier is owed at the default, because that would turn a missing form
field into a real debt. Both are right: a default in a cell is a suggestion, a
default in a payable is a fabrication.
"""

SHEET_NAME = "Sheet1"
HEADER_ROW = 1
FIRST_DATA_ROW = 2

# Column letter -> header text, exactly as it appears in the template.
HEADERS = {
    "A": "DN",
    "B": "Market Agent",
    "C": "Completed",
    "D": "Date",
    "E": "STM No",
    "F": "Description",
    "G": "Qty Received",
    "H": "Opening Stock",
    "I": "Frui Price/crt",
    "J": "Cartons Sold",
    "K": "Baby Stock",
    "L": "Price",
    "M": "Gross Total",
    "N": "Nett Total",
    "O": "Nett Price/crt",
    "P": "Z R/crt",
    "Q": "Z Total",
    "R": "% Markup",
    "S": "Frui Curr Sales Value",
    "T": "Status",
    "U": "NOTES",
}

# Columns we write real values into, mapped to the field name on StatementRow.
DATA_COLUMNS = {
    "A": "dn",
    "B": "market_agent",
    "C": "completed",
    "D": "date",
    "E": "stm_no",
    "F": "description",
    "G": "qty_received",
    "H": "opening_stock",
    "J": "cartons_sold",
    "L": "price",
    "N": "nett_total",
    "T": "status",
    "U": "notes",
}

# Columns that carry a template formula. `{r}` is the row number; the letter
# placeholders ({S}, {J}, ...) are the *template* letters of the columns the
# formula depends on. They are resolved against the workbook's actual layout at
# write time, so a workbook whose columns sit in different positions still gets
# formulas that point at the right cells.
COMMISSION_DEFAULT = 0.30

FORMULA_COLUMNS = {
    # The supplier's share per carton, at the default rate. Overwrite the cell
    # with the agreed price when a consignment is on different terms.
    "I": '=IFERROR({O}{r}*' + f"{1 - COMMISSION_DEFAULT:.0%}" + ',"-")',
    "K": "={H}{r}-{J}{r}",
    "M": "=SUM({J}{r}*{L}{r})",
    "O": '=IFERROR({N}{r}/{J}{r},"-")',
    "P": '=IFERROR({O}{r}-{I}{r},"-")',
    # Zaco's cut is what is left of the Nett after the supplier's share, so it
    # follows a hand-corrected I instead of being pinned to the default rate.
    "Q": "={N}{r}-{S}{r}",
    "R": '=IFERROR({P}{r}/{O}{r},"-")',
    "S": "={I}{r}*{J}{r}",
}

# Number formats applied to appended rows so they match the existing sheet.
NUMBER_FORMATS = {
    "D": "d-mmm",
    "G": "#,##0",
    "H": "#,##0",
    "I": "#,##0.00",
    "J": "#,##0",
    "K": "#,##0",
    "L": "#,##0.00000",
    "M": "#,##0.00",
    "N": "#,##0.00",
    "O": "#,##0.00",
    "P": "#,##0.00",
    "Q": "#,##0.00",
    "R": "0%",
    "S": "#,##0.00",
}

COMPLETED_DEFAULT = "Incomplete"
