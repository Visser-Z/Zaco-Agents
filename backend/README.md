# Zacon backend

Converts fixed-format account-sales statement PDFs into rows in an Excel
workbook. The workbook is a long-lived document the operator keeps adding to,
so every operation appends to the file they supply rather than rebuilding it.

## Running

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open **http://localhost:8000** — the backend serves the UI from
`../frontend`, so the app and API share an origin and no CORS setup is needed.
`http://localhost:8000/docs` gives the interactive API browser.

The UI checks `/api/health` on load. Connected, it processes real files and
shows a green **Connected** badge. If the API is not running it falls back to
built-in example data behind an amber **Demo data** badge, so the workflow stays
explorable without a backend.

```bash
pytest        # 12 tests covering extraction and workbook append
```

## Layout

| Path | Purpose |
|---|---|
| `app/columns.py` | The template's A–T column map, formula templates, number formats |
| `app/extraction.py` | Label-matching extraction from statement text |
| `app/workbook.py` | Open / read / append / save, preserving template formulas |
| `app/lookup.py` | Product string → short description code, persisted to `data/` |
| `app/schemas.py` | Shared request/response models |
| `app/main.py` | HTTP endpoints |

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness |
| `GET /api/lookup` | Current product→code map and the list of known codes |
| `POST /api/lookup` | Record a new product→code mapping |
| `POST /api/workbook/open` | Read an existing workbook's rows for display |
| `POST /api/extract` | Extract a round of statement PDFs into reviewable rows |
| `POST /api/workbook/append` | Append reviewed rows, return the updated `.xlsx` |

## Design notes

**Formula columns are never given values.** The template computes I, K, M, O,
P, Q, R and S. On append we re-write those formulas bound to the new row number.
Only A, B, C, D, E, F, G, H, J, L, N and T receive extracted values.

**Appending finds the first row with no data**, not `ws.max_row`. The template
pre-fills formulas well below the last real row, so trusting `max_row` would
drop new rows tens of rows down the sheet. Covered by
`test_append_skips_prefilled_formula_rows`.

**Headers are written once, by `new_workbook()` only.** `append_rows` never
touches row 1, so appending into a workbook the operator has been building
cannot stamp a duplicate header over their data. Locked in by
`test_headers_written_only_for_a_new_workbook`.

**New workbooks are seeded from `data/template.xlsx`**, a copy of the real
workbook with its data rows cleared. That keeps the header styling, column
widths and pre-filled formula rows, so a new file looks like the operator's own
rather than bare text. Falls back to plain headers if the template is missing.

**Column D is a group value.** Statements sharing a DN belong to one delivery
and all take that delivery's earliest `DATE RECEIVED`, even when the individual
statements arrived days apart. `apply_group_dates` settles this within a batch;
`reconcile_group_dates` extends it across separate appends, pulling rows already
in the sheet back to an earlier date when a later batch turns out to be older.

**Nothing is guessed.** Any label the extractor cannot resolve becomes a `Flag`
on the row instead of an exception or a silent default. Rows carrying an
error-severity flag are rejected by the append endpoint, so a bad extraction
cannot reach the workbook.

## Status

Validated end to end against the real `Project Tate.xlsx`: appending lands
directly beneath existing data, formulas rebind per row, and existing rows are
left byte-for-byte alone.

**The extraction regexes still need validating against a real PDF.** They were
written against `tests/fixtures/statement_387517.txt`, a transcription of the
documented layout. Column spacing in the price-breakdown table is the most
likely thing to differ once `pdfplumber` sees the genuine file. To validate:

```python
import pdfplumber
with pdfplumber.open("AS_387517.pdf") as pdf:
    print(pdf.pages[0].extract_text(layout=True))
```

Save that output over the fixture and re-run `pytest`. Failures there point at
exactly which label pattern needs adjusting.

## Pending

- A real statement PDF, to confirm the extraction patterns.
- The product description list (full PDF product string → short code) to seed
  `data/description_lookup.json`; it currently holds the single known mapping.
