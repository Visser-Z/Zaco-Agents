"""Zacon API.

Endpoints mirror the three things the frontend does: open a workbook, extract a
round of statements, and append those rows back into the workbook.
"""

from __future__ import annotations

import hmac
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query, Request,
                     UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import (
    doc_check,
    order_sheet,
    procurement,
    scorecard,
    analytics,
    assistant,
    config,
    csv_reports,
    forecast,
    lookup,
    nett_adjustments,
    payment_details,
    delivery,
    integrity,
    reconcile,
    reports,
    stock,
    technofresh,
    tracking,
)
from .supabase_auth import (User, current_profile, db_delete, db_get, db_patch,
                            db_post, require_admin, require_user)
from .extraction import apply_group_dates, pdf_to_page_texts, statements_from_pages
from . import dispatch_sheet, market_summary, overlap, supply
from .schemas import ExtractResponse, Flag, LookupEntry, NettMatch, StatementRow

app = FastAPI(title="ZacoAgents", version="0.1.0")

# The frontend is served separately during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000", "http://localhost:8731"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def restrict_to_allowed_networks(request: Request, call_next):
    """Reject callers outside ZACON_ALLOWED_NETWORKS.

    Secondary to the network itself: the app should not be publicly routable in
    the first place. This is here so a misconfigured firewall or an accidental
    bind to 0.0.0.0 does not immediately expose the data.

    `request.client.host` is the real peer address. It is deliberately not read
    from X-Forwarded-For, which a caller can set freely; if this is ever put
    behind a reverse proxy, that header must be handled by the proxy layer.
    """
    if not config.is_allowed(request.client.host if request.client else None):
        return JSONResponse({"detail": "Not available from this network."}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, object]:
    """Open endpoint. The login screen must be able to reach it before sign-in."""
    return {
        "status": "ok",
        "auth_required": config.AUTH_REQUIRED and config.auth_configured(),
        "supabase_url": config.SUPABASE_URL,
        "supabase_anon_key": config.SUPABASE_ANON_KEY,
        # Whether the Claude assistant has a key -- never the key itself. This
        # endpoint is open, so anything added here is public by definition.
        "assistant": assistant.configured(),
        # The second key, for reading the reports back to check the parser.
        "doc_check": assistant.docs_configured(),
        # The commit actually serving, so a stale deploy can be spotted from
        # outside instead of being taken on trust.
        "build": config.BUILD_SHA or "unknown",
    }


@app.get("/api/me")
async def me(profile: dict = Depends(current_profile)) -> dict[str, object]:
    """The signed-in user's profile, including their role.

    Read through PostgREST with the caller's own token, so RLS decides what is
    visible rather than the backend deciding on its behalf.
    """
    return {
        "id": profile.get("id"),
        "email": profile.get("email"),
        "full_name": profile.get("full_name"),
        "role": profile.get("role"),
    }


# --- lookup ---------------------------------------------------------------
# Backed by the Supabase `product_codes` table rather than a JSON file: the
# mapping is shared between users, and serverless hosts have a read-only
# filesystem, so writing it to disk would fail at exactly the moment an
# operator maps a new product.
#
# The JSON file remains the fallback for local development with no Supabase
# configured, so the app still runs offline.

async def codes_map(user: User | None) -> dict[str, str]:
    if user is None:
        return lookup.all_entries()
    rows = await db_get(user, "product_codes", {"select": "product,code"})
    return {r["product"]: r["code"] for r in rows}


async def remember_code(user: User | None, product: str, code: str) -> None:
    if user is None:
        lookup.remember(product, code)
        return
    await db_post(
        user,
        "product_codes",
        {"product": lookup.normalise(product), "code": code},
        upsert=True,
    )


@app.get("/api/lookup")
async def get_lookup(user: User | None = Depends(require_user)) -> dict[str, object]:
    entries = await codes_map(user)
    # Offer the keyword-rule codes in the picker too, so an auto-selected code
    # (and its siblings) can always be chosen even before it's in the table.
    codes = set(entries.values()) | {code for _, code in lookup._CLASSIFY_RULES}
    return {"entries": entries, "codes": sorted(codes)}


@app.post("/api/lookup")
async def add_lookup(
    entry: LookupEntry, user: User | None = Depends(require_user)
) -> dict[str, str]:
    await remember_code(user, entry.product, entry.code)
    return {"status": "saved"}


# --- extraction -----------------------------------------------------------

async def flag_duplicates(user: User | None, rows: list[StatementRow]) -> None:
    """Mark rows whose day is already on the book. See ``overlap``.

    Matched on the consignment and the day it sold, whatever statement number
    the report files it under, and compared with what was saved so a report
    that disagrees with the book says so. The review screen leaves every
    marked row out: what is on the book stays as it was saved. Skipped when no
    user is signed in (local dev has no history to compare against).
    """
    if user is None:
        return
    cons = sorted({int(r.consignment_id) for r in rows if r.consignment_id})
    stms = sorted({int(r.stm_no) for r in rows if r.stm_no is not None and not r.consignment_id})
    parts = []
    if cons:
        parts.append(f"consignment_id.in.({','.join(map(str, cons))})")
    if stms:
        parts.append(f"stm_no.in.({','.join(map(str, stms))})")
    if not parts:
        return
    columns = "stm_no,consignment_id,market_agent,created_at,sale_day,sales_total,cartons_sold"
    try:
        existing = await db_get(user, "statements",
                                {"select": columns, "or": f"({','.join(parts)})", "limit": "10000"})
    except Exception:  # noqa: BLE001 -- a duplicate warning is not worth failing an import
        return
    overlap.mark_saved(rows, existing)


def _merge_nett(nett_map: dict[int, dict], more: dict[int, dict]) -> None:
    """Fold one report's records into the running map, summing a statement that
    appears across more than one report."""
    for stm, rec in more.items():
        if stm in nett_map:
            cur = nett_map[stm]
            cur["nett"] = round(cur["nett"] + rec["nett"], 2)
            cur["gross"] = round(cur["gross"] + rec["gross"], 2)
            cur["lines"] += rec["lines"]
        else:
            nett_map[stm] = dict(rec)


def fill_netts(rows: list[StatementRow], nett_map: dict[int, dict]) -> NettMatch:
    """Fill each sales row's Nett from the adjustments map, keyed on statement
    number (column E). The report is the authoritative final figure, so it wins
    over any blank or provisional Nett, and clears the "enter Nett by hand"
    warning. Returns a summary of what matched.

    The report gives ONE figure per statement, and a statement now covers several
    rows -- the products on it. So it is **split between them by gross**, the same
    way the operator's own book splits it. Assigning the statement's full Nett to
    each row would multiply the money by the number of products on it.
    """
    used: set[int] = set()
    matched = 0

    per_statement: dict[int, list[StatementRow]] = {}
    for row in rows:
        if row.stm_no is not None and row.stm_no in nett_map:
            per_statement.setdefault(row.stm_no, []).append(row)

    for stm, group in per_statement.items():
        nett = nett_map[stm]["nett"]
        grosses = [(r.cartons_sold or 0) * (r.price or 0) for r in group]
        total = sum(grosses)
        if len(group) == 1 or not total:
            # No ratio to split by, so an equal share is the only honest option.
            shares = [round(nett / len(group), 2)] * len(group)
        else:
            shares = [round(nett * (g / total), 2) for g in grosses]
        if (residual := round(nett - sum(shares), 2)):
            biggest = max(range(len(group)), key=lambda i: abs(grosses[i]))
            shares[biggest] = round(shares[biggest] + residual, 2)
        for row, share in zip(group, shares):
            row.nett_total = share
            row.flags = [f for f in row.flags if f.field != "nett_total"]
            used.add(stm)
            matched += 1
    return NettMatch(
        reports=0,  # set by the caller, which knows how many PDFs were reports
        report_statements=len(nett_map),
        matched=matched,
        unmatched_rows=len(rows) - matched,
        unused=sum(1 for stm in nett_map if stm not in used),
    )


async def _delivery_notes(user: User | None) -> dict[int, int]:
    """Delivery ID -> Zaco's DN, as captured so far.

    Best-effort: without it column A falls back to the market's Supplier Ref,
    which is what it did before this existed.
    """
    if user is None:
        return {}
    try:
        rows = await db_get(user, "delivery_notes", {"select": "delivery_id,dn"})
    except Exception:  # noqa: BLE001 -- before migration 0011 there is no table
        return {}
    return {r["delivery_id"]: r["dn"] for r in rows if r.get("delivery_id") and r.get("dn")}


def _book_dns(raw: str | None) -> dict[int, int]:
    """``{account sale: DN}`` from the workbook the operator has open.

    Sent by the client, so nothing here trusts its shape: anything that is not a
    pair of numbers is dropped rather than allowed to raise, because a malformed
    hint must not cost the operator their import.
    """
    if not raw:
        return {}
    import json

    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[int, int] = {}
    for stm, dn in parsed.items():
        try:
            stm_no, number = int(stm), int(dn)
        except (TypeError, ValueError):
            continue
        if stm_no and number:
            out[stm_no] = number
    return out


async def _save_delivery_notes(user: User | None, pairs: dict[int, int]) -> None:
    """Store Delivery ID -> DN pairs. Best-effort by design."""
    if user is None or not pairs:
        return
    try:
        await db_post(
            user,
            "delivery_notes",
            [{"delivery_id": d, "dn": n} for d, n in pairs.items()],
            upsert=True,
            on_conflict="delivery_id",
        )
    except Exception:  # noqa: BLE001 -- a lookup write must never fail the request
        pass


async def remember_delivery_notes(user: User | None, rows: list[StatementRow]) -> None:
    """Record any DN the operator corrected, so the next round starts right."""
    if user is None:
        return
    await _save_delivery_notes(user, delivery.learned(rows, await _delivery_notes(user)))


async def _sold_before(user: User | None, rows: list[StatementRow]) -> dict[int, int]:
    """Cartons each consignment had already sold before this round.

    Opening Stock is a running balance, so a consignment whose earlier account
    sales are already saved must not start again at the full delivery. Account
    sales present in this round are excluded, so re-processing the same export
    does not subtract its own sales twice.

    Best-effort: without this the first row of a straddling consignment opens too
    high, which is worth a wrong figure on the review screen but not a failed
    import, so any database trouble is swallowed.
    """
    if user is None:
        return {}
    ids = sorted({r.consignment_id for r in rows if r.consignment_id is not None})
    if not ids:
        return {}
    here = {r.stm_no for r in rows if r.stm_no is not None}
    try:
        saved = await db_get(
            user,
            "statements",
            {
                "select": "consignment_id,stm_no,cartons_sold",
                "consignment_id": f"in.({','.join(str(i) for i in ids)})",
            },
        )
    except Exception:  # noqa: BLE001 -- a stock balance is not worth failing an import
        return {}
    out: dict[int, int] = {}
    for rec in saved:
        if rec.get("stm_no") in here:
            continue
        cid = rec.get("consignment_id")
        if cid is None:
            continue
        out[cid] = out.get(cid, 0) + (rec.get("cartons_sold") or 0)
    return out


# A column or table a migration adds, and the file that adds it. When the
# database is behind the code, PostgREST answers with the raw Postgres text
# ("column statements.consignment_id does not exist") or a bare 404, which reads
# like a broken app rather than one pending setup step.
_MIGRATIONS = {
    "sale_day": "0020_unique_per_sale_day.sql",
    "dismissals": "0016_dismissals.sql",
    "market_summaries": "0024_market_summaries.sql",
    "stock_carryovers": "0025_stock_carryovers.sql",
    "period_month": "0017_period_blocks.sql",
    "payments": "0015_payments.sql",
    "statements_unique_per_agent": "0014_statements_per_day.sql",
    "market_avg": "0013_statements_market_avg.sql",
    "cartons_returned": "0012_statements_returns.sql",
    "returns_total": "0012_statements_returns.sql",
    "consignment_id": "0010_statements_consignment_id.sql",
    "purchase_costs": "0008_purchase_costs.sql",
    "last_sale": "0006_statements_last_sale.sql",
    "payment_refs": "0007_statements_payment_refs.sql",
    "market": "0002_statements_market.sql",
    "fms_id": "0021_fms_and_stock_columns.sql",
    "qty_amended": "0021_fms_and_stock_columns.sql",
    "qty_avail": "0021_fms_and_stock_columns.sql",
}


def _pending_migration(exc: Exception) -> str | None:
    """The migration file that would fix this error, if that is what it is."""
    detail = str(getattr(exc, "detail", None) or exc)
    if "does not exist" not in detail and "404" not in detail:
        return None
    return next((f for name, f in _MIGRATIONS.items() if name in detail), None)


async def schema_gaps(user: User | None) -> list[str]:
    """Migrations this database is missing, by asking it for each new column.

    Cheap (a one-row select each) and worth it: without this the first sign is a
    failed save, after the operator has already done the work of a round.
    """
    if user is None:
        return []
    gaps: list[str] = []
    probes = (
        ("statements", "market_avg", "0013_statements_market_avg.sql"),
        ("statements", "cartons_returned", "0012_statements_returns.sql"),
        ("statements", "consignment_id", "0010_statements_consignment_id.sql"),
    )
    for table, column, migration in probes:
        try:
            await db_get(user, table, {"select": column, "limit": "1"})
        except Exception:  # noqa: BLE001 -- absence is the answer we are after
            gaps.append(migration)
    return gaps


async def _known_agents(user: User | None) -> set[str]:
    """Market agents that already appear in the saved history."""
    if user is None:
        return set()
    try:
        rows = await db_get(user, "statements", {"select": "market_agent"})
    except Exception:  # noqa: BLE001 -- a warning is not worth failing an import
        return set()
    return {r["market_agent"] for r in rows if r.get("market_agent")}


def _filter_warning(name: str, text: str) -> str | None:
    """The export states its own filter. Read it back rather than trusting that
    whoever ran it left the filters on ALL -- they usually believe they did."""
    f = csv_reports.export_filter(text)
    if not f["filtered"]:
        return None
    limits = " and ".join(x for x in (f["market"], f["agent"]) if x)
    return (
        f"“{name}” was exported for {limits} only, not for everything. "
        "Any other market or agent that traded in this period is missing from "
        "it. Re-export with Market and Agent both set to ALL."
    )


def _missing_agent_warning(name: str, seen: set[str], known: set[str]) -> str | None:
    """An agent in the history but not in this file. Not proof of a bad export
    -- an agent can simply have had no business -- so it is worded as a check,
    not an accusation."""
    absent = sorted(known - seen)
    if not absent or not seen:
        return None
    return (
        f"“{name}” contains {', '.join(sorted(seen))} but nothing for "
        f"{', '.join(absent)}, which you have traded through before. If they "
        "did trade in this period, the export was filtered."
    )


@app.post("/api/extract", response_model=ExtractResponse)
async def extract(
    files: list[UploadFile] = File(...),
    known_dns: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> ExtractResponse:
    """Extract a round of PDFs into rows awaiting review.

    A round may mix two kinds of PDF: sales reports (which become rows) and Nett
    Payment Adjustment reports (which carry no rows, but supply the Nett the
    Daily Sales format leaves blank). Adjustment reports are matched onto the
    sales rows by statement number and fill in their Nett.

    ``known_dns`` is ``{account sale: DN}`` read out of the workbook the operator
    has open. Column A is Zaco's own DN, which the market's export does not
    carry, but the workbook and the export share the account-sale number, so
    joining them recovers the DN for deliveries the workbook already covers
    instead of asking for them again. See ``delivery.seed_from_history``.
    """
    uploads = [(f.filename or "statement.pdf", await f.read()) for f in files]
    return await read_sales_files(user, uploads, known_dns)


async def read_sales_files(user: User | None, files: list[tuple[str, bytes]],
                           known_dns: str | None = None) -> ExtractResponse:
    """Everything /api/extract does, for files already in memory. The 8am
    TechnoFresh pull reads its CSVs through here too, so a pulled day is
    placed exactly as an uploaded one."""
    known = await codes_map(user)
    rows: list[StatementRow] = []
    nett_map: dict[int, dict] = {}
    nett_reports = 0
    warnings: list[str] = []

    for name, data in files:

        # CSV exports of the same reports. Preferred where available: the values
        # are read directly instead of being recovered from a PDF's layout, and
        # they carry the delivery date and the payment reference besides.
        if csv_reports.looks_like_csv(name, data):
            text = csv_reports.decode(data)
            if csv_reports.is_payment_details_csv(text):
                raise HTTPException(
                    400,
                    f"“{name}” is a Payment Details export. Load it under Payment "
                    "matching, not here — it carries the Nett, not the sales.",
                )
            if not csv_reports.is_daily_sales_csv(text):
                raise HTTPException(
                    400, f"Could not recognise “{name}” as a sales export."
                )
            extracted = csv_reports.parse_daily_sales_csv(text, name)
            # Read the export's own filter back, so a partial file is caught
            # here rather than showing up later as a market that vanished.
            if (w := _filter_warning(name, text)):
                warnings.append(w)
            seen_agents = {r.market_agent for r in extracted if r.market_agent}
            if (w := _missing_agent_warning(name, seen_agents, await _known_agents(user))):
                warnings.append(w)
        else:
            pages = pdf_to_page_texts(data)
            # A Nett Adjustments report contributes Nett figures, not rows.
            if nett_adjustments.is_nett_adjustments("\n".join(pages)):
                nett_reports += 1
                _merge_nett(nett_map, nett_adjustments.parse_nett_adjustments(pages, name))
                continue
            # One PDF may bundle several statements; each becomes its own row.
            extracted = statements_from_pages(pages, name)

        for row in extracted:
            # Exact lookup first, then a keyword guess by fruit type so common
            # products self-select. Anything wrong is editable on review.
            row.description = (
                known.get(lookup.normalise(row.product)) or lookup.classify(row.product)
                if row.product
                else None
            )
            if row.product and not row.description:
                row.flags.append(
                    Flag(
                        field="description",
                        message=f"No short code on file for “{row.product}”.",
                    )
                )
            rows.append(row)

    # Column A is Zaco's own DN, which no export carries. Fill it from what has
    # been captured before, and say so on the rows where the market's Supplier
    # Ref demonstrably is not one, so the operator answers once per delivery
    # rather than finding out months later against their own book.
    stored = await _delivery_notes(user)
    # Recover what the open workbook already knows about deliveries nobody has
    # recorded yet. A correction made on the review screen is more deliberate
    # than a row read out of a sheet, so a stored note wins over a recovered one.
    recovered = delivery.seed_from_history(rows, _book_dns(known_dns))
    fresh = {d: n for d, n in recovered.items() if d not in stored}
    notes = {**recovered, **stored}
    delivery.apply_notes(rows, notes)
    delivery.flag_unproven(rows, notes)
    # Keep the new ones, so the next round does not depend on that workbook
    # being open, or on the operator opening the same one.
    await _save_delivery_notes(user, fresh)

    # A day read twice in this drop -- a day's report and the week's over it --
    # is one day. The spare copy is set aside before anything is counted, so
    # the stock carried forward does not sell the same cartons twice.
    rows, repeats = overlap.collapse_batch(rows)

    # Column D depends on the whole group, so it is resolved across the batch.
    apply_group_dates(rows)

    # Opening Stock is a running balance per consignment, so it is settled once
    # across the whole round rather than per file: a consignment straddling two
    # weekly exports would otherwise restart at the full delivery in the second.
    stock.carry_forward(rows, await _sold_before(user, rows))
    stock.flag_impossible_stock(rows)

    # Days already on the book stay as they were saved; say where they differ.
    await flag_duplicates(user, rows)
    rows += repeats

    # A database behind the code cannot record anything, and the operator should
    # hear that before reviewing 70 rows, not after pressing Save.
    for migration in await schema_gaps(user):
        warnings.append(
            f"This database is missing the {migration} migration. Your workbook will "
            f"still save, but nothing will be recorded to history until it is run in "
            f"the Supabase SQL editor, so Insights and settlements stay empty."
        )

    # Fill Nett from any adjustment reports dropped in the same round.
    nett_match = None
    if nett_reports:
        nett_match = fill_netts(rows, nett_map)
        nett_match.reports = nett_reports

    unknown = sorted({r.product for r in rows if r.product and not r.description})
    netts = {str(stm): rec["nett"] for stm, rec in nett_map.items()}
    return ExtractResponse(
        rows=rows, unknown_products=unknown, nett_match=nett_match, netts=netts,
        warnings=warnings,
    )


# --- history persistence --------------------------------------------------
# Every appended row is also recorded in the Supabase `statements` table. The
# Excel workbook stays the operator's working document; this is the durable
# history the analytics endpoint reads. It only runs when a real user is
# present (production / auth on) -- local development has no Supabase to write
# to, so persistence is simply skipped there.

def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _statement_record(row: StatementRow, user: User) -> dict | None:
    """Map a reviewed row onto a `statements` insert, or None if it can't be
    keyed. The table requires market_agent and stm_no (its unique key); a row
    missing either cannot be recorded without corrupting the history."""
    if not row.market_agent or row.stm_no is None:
        return None
    return {
        "market_agent": row.market_agent,
        "market": row.market,
        "supplier_ref": row.supplier_ref,
        "sales_total": row.sales_total,
        "last_sale": _iso(row.last_sale),
        "payment_refs": row.payment_refs,
        "stm_no": row.stm_no,
        # 0 stands for "no consignment id" so the unique key still bites: in
        # Postgres a NULL is distinct from every other NULL, which would let the
        # same row be recorded again and again.
        "consignment_id": row.consignment_id or 0,
        "delivery_id": row.delivery_id,
        "dn": row.dn,
        "product": row.product,
        "description": row.description,
        "qty_received": row.qty_received,
        "qty_amended": row.qty_amended,
        "qty_avail": row.qty_avail,
        "opening_stock": row.opening_stock,
        "cartons_sold": row.cartons_sold,
        # Net of returns, as cartons_sold is, so the two figures folded into it
        # can be told apart again. See ``StatementRow.cartons_returned``.
        "cartons_returned": row.cartons_returned,
        "returns_total": row.returns_total,
        # What the market itself was paying for this commodity that day.
        "market_avg": row.market_avg,
        "price": row.price,
        "nett_total": row.nett_total,
        "date_received": _iso(row.date_received),
        "invoice_date": _iso(row.invoice_date),
        "group_date": _iso(row.date),
        "status": row.status,
        "source_file": row.source_file,
        "created_by": user.id,
    }


async def persist_statements(user: User | None, rows: list[StatementRow]) -> str | None:
    """Upsert appended rows into the history table, keyed on (market_agent,
    stm_no, consignment_id).

    Best-effort: the Excel workbook is the operator's deliverable and must be
    returned even if the history write fails (e.g. migration 0002 not yet
    applied, or a transient database error). On failure this returns a short
    warning message for the caller to surface, rather than raising -- a failed
    analytics write must never cost the operator their save.

    Skipped entirely without a signed-in user. Reprocessing the same statement
    updates the existing record rather than duplicating it.
    """
    if user is None:
        return None
    records = _one_per_sale_day(
        [rec for r in rows if (rec := _statement_record(r, user)) is not None])
    if not records:
        return None
    try:
        # A row already on the book is refreshed, not skipped. Dropping a report
        # again -- or a week's report over the days already loaded from their
        # own -- then brings every row up to what the report says, including
        # fields the first save could not read yet. Migration 0019 opened
        # UPDATE to signed-in staff, so this no longer trips row-level security.
        # One account sale settles several consignments, each its own row,
        # and a consignment sells over several days, each its own row too.
        # The day here must be the day it SOLD. group_date is the
        # consignment's date, and apply_group_dates collapses it to one
        # value per consignment across a batch, so keyed on that a week of
        # reports dropped together kept only the first day of each
        # consignment and the insert discarded the rest in silence.
        await _post_with_late_columns(
            user, "statements", records,
            on_conflict="market_agent,stm_no,consignment_id,sale_day",
            late=("qty_amended", "qty_avail"))
    except Exception as exc:  # noqa: BLE001 -- save must succeed regardless
        if (migration := _pending_migration(exc)):
            # Deliberately NOT retried without the new column: the older unique
            # key is (market_agent, stm_no), and an account sale now covers
            # several rows, so the ignore-duplicates upsert would keep the first
            # product and drop the rest without a word. Better to record nothing
            # and say why than to record a quarter of the round in silence.
            return (
                f"Your Excel file saved normally. History was not recorded: the database "
                f"is missing the {migration} migration, so nothing can be written until it "
                f"is run in the Supabase SQL editor. Insights and settlements stay empty "
                f"until then, and no data has been lost — re-save this round afterwards."
            )
        detail = getattr(exc, "detail", None) or str(exc)
        return f"Saved to Excel, but recording history for Insights failed: {detail}"
    return None


def _one_per_sale_day(records: list[dict]) -> list[dict]:
    """One record per (agent, statement, consignment, day sold).

    Overlapping reports put the same day in one batch twice -- a day's report
    and the week's that covers it. Refreshing a row the database already has is
    an update, and an update may not touch the same row twice in one write, so
    the whole save would be refused. The later copy is kept: it is the later
    word on that day, and carries the later stock figure.
    """
    out: dict[tuple, dict] = {}
    for rec in records:
        day = rec.get("last_sale") or rec.get("group_date") or rec.get("invoice_date") \
            or rec.get("date_received")
        out[(rec["market_agent"], rec["stm_no"], rec["consignment_id"], day)] = rec
    return list(out.values())


async def _post_with_late_columns(user: User, table: str, records: list[dict],
                                  on_conflict: str, late: tuple[str, ...]) -> None:
    """Save, refreshing rows already present; before a migration, without it.

    Columns added by a migration that has not been run yet fail the whole
    write. Those columns only add detail, so the write is retried without them
    rather than losing the round: the rows are saved, and dropping the report
    again after the migration fills the detail in.
    """
    try:
        await db_post(user, table, records, upsert=True, on_conflict=on_conflict,
                      resolution="merge-duplicates")
    except Exception as exc:  # noqa: BLE001
        detail = str(getattr(exc, "detail", None) or exc)
        if not any(col in detail for col in late):
            raise
        trimmed = [{k: v for k, v in r.items() if k not in late} for r in records]
        await db_post(user, table, trimmed, upsert=True, on_conflict=on_conflict,
                      resolution="merge-duplicates")


# --- append + save --------------------------------------------------------

@app.post("/api/sales/save")
async def save_rows(
    rows_json: str = Form(..., alias="rows"),
    market_agent: str = Form(...),
    user: User | None = Depends(require_user),
) -> dict:
    """Record reviewed rows. The app's own store is the book now.

    This replaces the old append-to-workbook path. Nothing is written to a file
    and nothing is returned to download: the rows go to the durable history and
    the UI reads them back from there.
    """
    import json

    try:
        payload = [StatementRow.model_validate(r) for r in json.loads(rows_json)]
    except Exception as exc:
        raise HTTPException(400, f"Malformed rows payload: {exc}") from exc

    blocking = [r for r in payload if r.blocking]
    if blocking:
        raise HTTPException(
            400,
            f"{len(blocking)} row(s) still have unresolved issues and cannot be saved.",
        )

    # The workbook writer used to be what finalised the agent onto each row.
    # Nothing else did it, so it moves here rather than disappearing with it.
    for row in payload:
        row.market_agent = row.market_agent or market_agent

    warning = await persist_statements(user, payload)
    # A DN the operator corrected on review is worth keeping: no export carries
    # it, so the alternative is re-entering it for the same delivery every round.
    await remember_delivery_notes(user, payload)

    return {"saved": len(payload), "warning": warning}


# --- analytics ------------------------------------------------------------

_ANALYTICS_COLUMNS = (
    "market_agent,market,description,product,cartons_sold,price,nett_total,"
    "group_date,invoice_date,date_received,status,created_at,"
    # What the row actually sold for. ``price`` is an average the extractor
    # divides out and rounds, so cartons x price does not come back to the
    # money: 13 cartons for R740,00 read back as R739,96. Both extractors
    # record the exact figure and the save writes it; leaving it out of the
    # read meant every view recomputed the rounded one. From migration 0003,
    # so it is older than everything in the fallback chain below.
    "sales_total,"
    # supplier_ref and dn are how a row finds its recorded purchase cost.
    "qty_received,last_sale,payment_refs,supplier_ref,dn,"
    # Which report a row came from, so a day can say where it was read.
    "source_file,"
    # Rows are account sales; this is how several of them are recognised as one
    # delivery, so what was SENT is not counted once per account sale.
    "consignment_id,"
    # What came back, so Insights can report sold and returned rather than only
    # the net of the two.
    "cartons_returned,returns_total,"
    # What the market itself averaged, so the price can be checked against it.
    "market_avg,"
    # What the market booked and what it still holds, for stock on hand.
    "qty_amended,qty_avail"
)

# Columns that arrived with a later migration, newest group first. Asking a
# database for a column it does not have yet fails the WHOLE request, which
# would take Insights, the buy list and settlements down together over one
# column none of them strictly needs -- so each group is dropped in turn and the
# read retried, rather than the page going dark.
_LATE_COLUMNS = (
    ("qty_amended", "qty_avail"),            # 0021
    ("market_avg",),                         # 0013
    ("cartons_returned", "returns_total"),   # 0012
    ("consignment_id",),                     # 0010
)


async def _history_rows(user: User | None) -> list[dict]:
    """Every recorded statement, oldest first, read as the caller so RLS applies.

    Shared by the dashboard and the assistant so both answer from exactly the
    same history. Empty in local mode, where there is no Supabase to read.
    """
    if user is None:
        return []

    # Every column, then progressively fewer as each late migration is assumed
    # missing. A row that comes back without consignment_id is right for history
    # recorded before the split anyway (each was already one row per
    # consignment); one without the returns columns simply reports nothing
    # returned, which is what that history knew.
    selects = [_ANALYTICS_COLUMNS]
    for late in _LATE_COLUMNS:
        trimmed = selects[-1]
        for name in late:
            trimmed = trimmed.replace(f",{name}", "")
        selects.append(trimmed)

    failure: Exception | None = None
    for select in selects:
        query = {"select": select, "order": "group_date.asc", "limit": "10000"}
        try:
            return await db_get(user, "statements", query)
        except Exception as exc:  # noqa: BLE001
            failure = exc
    raise failure


# --- payments (the durable record behind the tracker) ---------------------

def _payment_record(rec: dict, user: User) -> dict | None:
    """Map a parsed payment onto a `payments` insert, or None if unkeyable.

    The AccSale number is the key. A record without one cannot be recorded
    without colliding with every other unkeyed payment, so it is left out (its
    money still shows in the reconcile panel's unattributed line).
    """
    if not rec.get("accsale"):
        return None
    return {
        "accsale": rec["accsale"],
        "stm_no": rec.get("stm_no"),
        "market_agent": rec.get("market_agent"),
        "supplier_ref": rec.get("supplier_ref"),
        "dn": rec.get("dn"),
        "paid_on": rec.get("date"),
        "nett": rec.get("nett"),
        "gross": rec.get("gross"),
        "deductions": rec.get("deductions"),
        "vat": rec.get("vat"),
        "lines": rec.get("lines") or [],
        # One per delivery and stable across its account sales: what ties a
        # payment to the sales it paid for. See migration 0021.
        "fms_id": rec.get("fms_id"),
        "created_by": user.id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def persist_payments(user: User | None, records: list[dict]) -> str | None:
    """Record reconciled payments, so outstanding money survives a refresh.

    Best-effort, exactly like ``persist_statements``: reconciliation must still
    return even if the write fails (the table's migration not run yet, say).

    The same payment arriving again -- the file dropped twice, or a day's
    report and then the week's -- is saved once. It refreshes the record
    already held rather than being skipped, so a payment first read wrongly is
    put right by dropping its report again: the two Durban account sales whose
    lines were absorbed by the old parser mend themselves that way.
    """
    if user is None:
        return None
    rows = [r for rec in payment_details.dedupe(records)
            if (r := _payment_record(rec, user)) is not None]
    if not rows:
        return None
    try:
        await _post_with_late_columns(user, "payments", rows,
                                      on_conflict="accsale", late=("fms_id",))
    except Exception as exc:  # noqa: BLE001 -- reconciliation must succeed regardless
        if (migration := _pending_migration(exc)):
            return (
                f"Payments were matched but not recorded: the database is missing the "
                f"{migration} migration. Run it in the Supabase SQL editor and re-drop "
                f"the report; nothing is lost."
            )
        return None
    return None


async def persist_market_summaries(user: User | None, records: list[dict]) -> dict:
    """Keep the market's summaries, never letting an older run replace a newer.

    Returns what was kept, for the payments screen to say so.
    """
    out = {"deliveries": len({r["delivery_id"] for r in records}),
           "lines": len(records),
           "run_at": max((r["run_at"] for r in records if r.get("run_at")), default=None),
           "unpaid": round(sum(r["unpaid"] for r in records), 2), "kept": 0, "older": 0}
    if user is None or not records:
        return out
    ids = sorted({r["delivery_id"] for r in records})
    try:
        held = await db_get(user, "market_summaries", {
            "select": "delivery_id,product,run_at",
            "delivery_id": f"in.({','.join(map(str, ids))})", "limit": "10000"})
    except Exception as exc:  # noqa: BLE001
        migration = _pending_migration(exc) or "0024_market_summaries.sql"
        out["warning"] = (f"The Summary of Deliveries was read but not kept: the database "
                          f"is missing the {migration} migration.")
        return out
    newest = {(int(h["delivery_id"]), h["product"]): (h.get("run_at") or "") for h in held}
    fresh = [r for r in records
             if (r.get("run_at") or "") >= newest.get((r["delivery_id"], r["product"]), "")]
    out["older"] = len(records) - len(fresh)
    rows = [{**r, "created_by": user.id} for r in fresh]
    if rows:
        try:
            await db_post(user, "market_summaries", rows, upsert=True,
                          on_conflict="delivery_id,product", resolution="merge-duplicates")
        except Exception as exc:  # noqa: BLE001
            out["warning"] = f"The Summary of Deliveries was read but could not be kept: {exc}"
            return out
    out["kept"] = len(rows)
    return out


async def _market_summaries(user: User | None) -> list[dict] | None:
    """The market's summaries on file, or None where there is no table yet."""
    if user is None:
        return None
    try:
        return await db_get(user, "market_summaries", {
            "select": "delivery_id,product,product_name,agent,supplier_ref,date_sent,"
                      "sold,gross,paid,unpaid,run_at", "limit": "20000"})
    except Exception:  # noqa: BLE001 -- before migration 0024
        return None


async def _saved_payments(user: User | None) -> list[dict]:
    """Every recorded payment, read as the caller so RLS applies."""
    if user is None:
        return []
    columns = "accsale,stm_no,market_agent,supplier_ref,dn,paid_on,nett,gross,lines"
    rows = None
    # With the FMS id where migration 0021 has run, without it before. The
    # market's deductions and the VAT on them were stored all along but never
    # read, so Tracking could show what the market took only as one sum.
    for select in (columns + ",deductions,vat,fms_id", columns + ",fms_id", columns):
        try:
            rows = await db_get(user, "payments", {"select": select, "limit": "10000"})
            break
        except Exception:  # noqa: BLE001 -- before migration 0015 there are none
            continue
    if rows is None:
        return []
    # reconcile expects a payment's date under "date" and its breakdown under
    # "lines"; the table stores the date as paid_on. Bridge the two shapes here.
    for r in rows:
        r["date"] = r.pop("paid_on", None)
        r["lines"] = r.get("lines") or []
    _apply_checks(rows, await _payment_checks(user))
    return rows


async def _payment_checks(user: User | None) -> list[dict]:
    """What people decided about payments the matcher was unsure of."""
    if user is None:
        return []
    try:
        return await db_get(user, "payment_checks", {
            "select": "accsale,product,decision,consignment_id,note,created_at",
            "limit": "5000"})
    except Exception:  # noqa: BLE001 -- before migration 0023 nothing is decided
        return []


def _apply_checks(payments: list[dict], checks: list[dict]) -> None:
    """Put each decision onto the payment line it is about.

    Done here, as the payments are read, rather than passed to the matcher:
    every screen that settles payments reads them through this function, so a
    link made on Tracking is the link the chat, the reports and Procurement
    see as well, without any of them having to be told.
    """
    if not checks:
        return
    by_key = {tracking.link_key(c.get("accsale"), c.get("product")): c for c in checks}
    unlisted = tracking.link_key("", tracking.NOT_LISTED)[1]
    for rec in payments:
        # Money the market paid without itemising it has no line to carry a
        # link, so a link for it becomes the line, for exactly that money.
        c = by_key.get(tracking.link_key(rec.get("accsale"), tracking.NOT_LISTED))
        listed = sum(float(l.get("sales_total") or 0) for l in rec.get("lines") or [])
        gap = round(float(rec.get("gross") or 0) - listed, 2)
        itemised = any(tracking.link_key("", l.get("product"))[1] == unlisted
                       for l in rec.get("lines") or [])
        if (c and c.get("decision") == "link" and c.get("consignment_id") is not None
                and gap > 0.01 and not itemised):
            rec["lines"] = [*(rec.get("lines") or []),
                            {"product": tracking.NOT_LISTED, "sales_total": gap}]
        for line in rec.get("lines") or []:
            c = by_key.get(tracking.link_key(rec.get("accsale"), line.get("product")))
            if not c:
                continue
            if c.get("decision") == "link" and c.get("consignment_id") is not None:
                line["linked_consignment"] = int(c["consignment_id"])
            elif c.get("decision") == "keep":
                line["check"] = "keep"


async def _stock_carryovers(user: User | None) -> list[dict]:
    """Stock carried from one month into the next, read as the caller."""
    if user is None:
        return []
    try:
        return await db_get(user, "stock_carryovers", {
            "select": "month,ref,consignment_id,cartons,arrived", "limit": "20000"})
    except Exception:  # noqa: BLE001 -- before migration 0025 nothing is carried
        return []


@app.get("/api/tracking/carryover")
async def carryover_status(user: User | None = Depends(require_user)) -> dict:
    """Whether this month still has last month's unsold stock to carry in, for
    the notice the app shows on any page at the start of a month."""
    if user is None:
        return {"pending": False}
    status = tracking.carryover_status(await _history_rows(user), None,
                                       await _closed_refs(user), await _stock_carryovers(user))
    status.pop("_left", None)
    return status


@app.post("/api/tracking/carryover")
async def carry_stock_over(
    month: str = Form(..., pattern=r"^\d{4}-\d{2}$"),
    user: User | None = Depends(require_user),
) -> dict:
    """Carry everything still on the floor from before `month` into it.

    Only where it is shown changes: a carton sold in the new month is that
    month's sale and its money lands in that month, as it always did.
    """
    if user is None:
        raise HTTPException(401, "Sign in to carry stock over.")
    sales = await _history_rows(user)
    closed = await _closed_refs(user)
    left = tracking.last_months_stock(sales, month, None, closed,
                                      await _stock_carryovers(user))
    rows = [{"month": month, "ref": r["ref"], "consignment_id": r.get("consignment_id"),
             "product": r.get("product"), "market": r.get("market"),
             "cartons": r["cartons_left"], "arrived": r["arrived"], "created_by": user.id}
            for r in left]
    if rows:
        try:
            await db_post(user, "stock_carryovers", rows, upsert=True,
                          on_conflict="month,ref", resolution="merge-duplicates")
        except Exception as exc:  # noqa: BLE001
            if (migration := _pending_migration(exc)):
                raise HTTPException(400, f"Carrying stock over needs the {migration} migration.") from exc
            raise HTTPException(400, f"Could not carry the stock over: {exc}") from exc
    return {"month": month, "lines": len(rows), "cartons": sum(r["cartons"] for r in rows)}


@app.post("/api/tracking/carryover/undo")
async def undo_carry_over(
    month: str = Form(..., pattern=r"^\d{4}-\d{2}$"),
    user: User | None = Depends(require_user),
) -> dict:
    """Take a month's carried stock back out of it."""
    if user is None:
        raise HTTPException(401, "Sign in to undo a carry-over.")
    await db_delete(user, "stock_carryovers", {"month": f"eq.{month}"})
    return {"month": month, "undone": True}


async def _closed_refs(user: User | None) -> set[str]:
    """Tracking lines the team has closed off, read as the caller."""
    if user is None:
        return set()
    try:
        rows = await db_get(user, "dismissals", {"select": "kind,ref", "limit": "5000"})
    except Exception:  # noqa: BLE001 -- before migration 0016 nothing is closed
        return set()
    return {f"{r['kind']}:{r['ref']}" for r in rows if r.get("ref")}


@app.post("/api/tracking/close")
async def close_tracking_item(
    kind: str = Form(...),
    ref: str = Form(...),
    note: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> dict:
    """Mark one Tracking line as dealt with.

    Advisory only: nothing in the sales or payment history changes, and the
    line keeps its value on the closed list so closing can never quietly
    shrink what is owed.
    """
    if user is None:
        raise HTTPException(401, "Sign in to close an item.")
    if kind not in ("owed", "slow"):
        raise HTTPException(400, f"Unknown kind “{kind}”.")
    try:
        await db_post(user, "dismissals",
                      [{"kind": kind, "ref": ref, "note": note, "created_by": user.id}],
                      upsert=True, on_conflict="kind,ref",
                      resolution="ignore-duplicates")
    except Exception as exc:  # noqa: BLE001
        if (migration := _pending_migration(exc)):
            raise HTTPException(
                400, f"Closing needs the {migration} migration, which has not been "
                     f"run in the Supabase SQL editor yet.") from exc
        raise HTTPException(400, f"Could not close that item: {exc}") from exc
    return {"closed": f"{kind}:{ref}"}


@app.post("/api/tracking/reopen")
async def reopen_tracking_item(
    kind: str = Form(...),
    ref: str = Form(...),
    user: User | None = Depends(require_user),
) -> dict:
    """Put a closed line back on the live list."""
    if user is None:
        raise HTTPException(401, "Sign in to reopen an item.")
    await db_delete(user, "dismissals", {"kind": f"eq.{kind}", "ref": f"eq.{ref}"})
    return {"reopened": f"{kind}:{ref}"}


@app.get("/api/tracking")
async def get_tracking(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    month: str | None = Query(None),
    week: str | None = Query(None),
    user: User | None = Depends(require_user),
) -> dict:
    """The Tracking tab: paid vs outstanding, sales per day, slow stock.

    Answered from saved data -- the statement history and the recorded payments
    -- so it holds for the whole period rather than only the moment a file is
    dropped. Empty but valid in local mode, where there is nothing saved.

    `from` and `to` ("YYYY-MM-DD") narrow the per-day sales list only. What is
    owed is a running position, not a period figure, so date-filtering it would
    understate the exposure.
    """
    sales = await _history_rows(user)
    payments = await _saved_payments(user)
    closed = await _closed_refs(user)
    carried = await _stock_carryovers(user)
    out = tracking.compute(sales, payments, start=date_from, end=date_to,
                           closed=closed, month=month, week=week, carried=carried)
    # Whether this month still has last month's unsold stock to carry in.
    status = tracking.carryover_status(sales, None, closed, carried)
    status.pop("_left", None)
    out["carryover"] = status
    # What has been decided, so a decision can be seen and undone.
    out["flags"]["decisions"] = await _payment_checks(user)
    # Every outstanding line held against the market's own summaries.
    summaries = await _market_summaries(user)
    if summaries is not None:
        out["market_check"] = market_summary.check(
            sales, payments, summaries, out["payments"]["outstanding"])
    return out


@app.get("/api/sales-days")
async def get_sales_days(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    month: str | None = Query(None),
    week: str | None = Query(None),
    user: User | None = Depends(require_user),
) -> dict:
    """Sales per day for Insights, with what each day has been paid and owes."""
    sales = await _history_rows(user)
    payments = await _saved_payments(user)
    return tracking.daily(sales, payments, start=date_from, end=date_to,
                          month=month, week=week)


@app.post("/api/tracking/flags/decide")
async def decide_payment_flag(
    accsale: str = Form(...),
    product: str = Form(...),
    decision: str = Form(...),
    consignment_id: int | None = Form(None),
    note: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> dict:
    """Settle a flagged payment by hand: keep it where it is, or link it.

    Nothing in the sales or payment history is changed. The decision is kept
    beside them and applied every time payments are read, so it can always be
    seen, and undone, and the original match comes back when it is.
    """
    if user is None:
        raise HTTPException(401, "Sign in to check a payment.")
    if decision not in ("keep", "link"):
        raise HTTPException(400, f"Unknown decision \u201c{decision}\u201d.")
    if decision == "link" and consignment_id is None:
        raise HTTPException(400, "Say which sale the payment belongs to.")
    try:
        await db_post(user, "payment_checks", [{
            "accsale": accsale.strip(), "product": product.strip(), "decision": decision,
            "consignment_id": consignment_id if decision == "link" else None,
            "note": note, "created_by": user.id}],
            upsert=True, on_conflict="accsale,product")
    except Exception as exc:  # noqa: BLE001
        if (migration := _pending_migration(exc)):
            raise HTTPException(
                400, f"Checking payments needs the {migration} migration, which has not "
                     f"been run in the Supabase SQL editor yet.") from exc
        raise HTTPException(400, f"Could not record that: {exc}") from exc
    return {"decided": decision, "accsale": accsale, "product": product}


@app.post("/api/tracking/flags/undo")
async def undo_payment_flag(
    accsale: str = Form(...),
    product: str = Form(...),
    user: User | None = Depends(require_user),
) -> dict:
    """Take a decision back; the payment is matched by the rules again."""
    if user is None:
        raise HTTPException(401, "Sign in to undo a check.")
    await db_delete(user, "payment_checks",
                    {"accsale": f"eq.{accsale.strip()}", "product": f"eq.{product.strip()}"})
    return {"undone": True}


@app.get("/api/reports/periods")
async def get_report_periods(user: User | None = Depends(require_user)) -> dict:
    """The months the history holds, newest first.

    Read from the period_index view where migration 0017 has been run, which
    answers from the month column rather than by reading every statement.
    Falls back to counting the history in the app, so the picker still works
    before the migration.
    """
    if user is None:
        return {"periods": [], "source": "local"}
    try:
        rows = await db_get(user, "period_index",
                            {"select": "*", "order": "period_month.desc", "limit": "500"})
        return {"periods": rows, "source": "period_index"}
    except Exception:  # noqa: BLE001 -- before migration 0017 the view is absent
        periods: dict[str, dict] = {}
        for row in await _history_rows(user):
            day = str(row.get("group_date") or "")[:10]
            if len(day) < 7:
                continue
            p = periods.setdefault(day[:7], {
                "period_month": day[:7], "first_sale": day, "last_sale": day,
                "statement_count": 0, "cartons_sold": 0.0, "sales_value": 0.0})
            p["first_sale"] = min(p["first_sale"], day)
            p["last_sale"] = max(p["last_sale"], day)
            p["statement_count"] += 1
            p["cartons_sold"] += analytics.row_cartons(row)
            p["sales_value"] += analytics.row_value(row)
        for p in periods.values():
            p["cartons_sold"] = round(p["cartons_sold"], 2)
            p["sales_value"] = round(p["sales_value"], 2)
        return {"periods": sorted(periods.values(), key=lambda p: p["period_month"],
                                  reverse=True), "source": "history"}


@app.get("/api/reports/period")
async def get_period_report(
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    user: User | None = Depends(require_user),
) -> dict:
    """A report for one date range: what sold, what came in, what it is worth.

    Sales are taken on the day they sold, payments on the day they were
    received. Both ends are inclusive, so a single day is from == to.
    """
    for label, value in (("from", date_from), ("to", date_to)):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"“{value}” is not a date (expected YYYY-MM-DD in “{label}”).")
    if date_from > date_to:
        raise HTTPException(400, "The start of the range falls after its end.")

    sales = await _history_rows(user)
    payments = await _saved_payments(user)
    return reports.build(sales, payments, date_from, date_to)


@app.get("/api/analytics")
async def get_analytics(
    month: str | None = None,
    week: str | None = None,
    user: User | None = Depends(require_user),
) -> dict:
    """Sales insights over the recorded statement history.

    Optional `month` ("YYYY-MM") and `week` ("YYYY-Www") scope the whole
    dashboard to a period; omit both for the all-time view. The trend's
    granularity follows the scope (week->daily, month->weekly, all->monthly).

    Reads the durable `statements` table (not the working Excel file) as the
    caller, so RLS applies. With auth off locally there is no history to read,
    so it returns an empty-but-valid payload and the UI shows its empty state.
    """
    rows = await _history_rows(user)

    # Pickers list every period that has data, so they stay populated even when
    # the current filter narrows the view to one month or week.
    available = analytics.available_periods(rows)
    scoped = analytics.filter_rows(rows, month, week)
    period = analytics.trend_granularity(month, week)

    result = analytics.compute(scoped, period)
    result["available"] = available
    result["filter"] = {"month": month, "week": week}
    # How the agent has treated this money. Computed over the SAME scoped rows
    # the rest of the dashboard shows, and measured against this business's own
    # going rate rather than a figure of ours.
    result["integrity"] = integrity.summary(scoped)
    return result


@app.post("/api/history/delete")
async def delete_history(
    month: str | None = Form(None),
    week: str | None = Form(None),
    scope: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> dict:
    """Delete a period of the book, or all of it.

    A period is not just the sales. Tracking answers from the sales, the
    payments recorded against them and the lines closed on them, so removing
    one and leaving the others is what left a deleted month still on screen.
    All three go together here.

    `scope="all"` clears the whole book. It is a separate word rather than an
    empty month so that it can only ever be asked for deliberately -- a missing
    filter must stay an error, not a way to empty the table by accident.
    """
    if user is None:
        raise HTTPException(400, "History deletion is not available in local mode.")

    everything = (scope or "").lower() == "all"
    lo, hi = analytics.period_bounds(month, week)
    if not everything and not lo:
        raise HTTPException(400, "Choose a month or week to delete.")

    if everything:
        return await _delete_everything(user)
    # Two passes, because the period is not one column.
    #
    # First the dated rows, by a filter the database applies itself. This must
    # not be done by reading the ids and deleting those: a read is capped by the
    # server's row limit and comes back in no particular order, so on a history
    # larger than that cap the rows to delete may simply not be in the page that
    # comes back, and the delete quietly does nothing.
    # A row's period is decided by the first date it actually has: the day it
    # sold, then the consignment's date, then the invoice, then when it was
    # received. The delete has to walk the same chain or it removes a different
    # set than the page just counted -- a sale on 1 August off a load sent on
    # 31 July is August everywhere except in a filter written on group_date.
    windows = [
        {"and": f"(last_sale.gte.{lo},last_sale.lte.{hi})"},
        {"last_sale": "is.null", "and": f"(group_date.gte.{lo},group_date.lte.{hi})"},
        {"last_sale": "is.null", "group_date": "is.null",
         "and": f"(invoice_date.gte.{lo},invoice_date.lte.{hi})"},
        {"last_sale": "is.null", "group_date": "is.null", "invoice_date": "is.null",
         "and": f"(date_received.gte.{lo},date_received.lte.{hi})"},
    ]
    deleted: list[dict] = []
    for where in windows:
        deleted += await db_delete(user, "statements", where)

    window = f"(group_date.gte.{lo},group_date.lte.{hi})"

    # Then the rows carrying no group_date at all. They are still dated, because
    # the pages fall back to the invoice date, then the received date, then when
    # the row was recorded -- so a statement shown under August with no
    # group_date survived a filter written against group_date and brought the
    # period back on the next refresh. Only null-dated rows are read here, which
    # is a small set, and they are removed by id.
    undated = await db_get(user, "statements", {
        "select": "id,group_date,invoice_date,date_received,created_at",
        "group_date": "is.null",
        "limit": "20000",
    })
    strays = [r["id"] for r in analytics.filter_rows(undated, month, week) if r.get("id")]
    deleted += await _delete_by_id(user, "statements", strays)

    # Tracking is not built from the statements alone: what is owed comes from
    # the recorded payments, and the closed lines from the dismissals. Deleting
    # only the sales left Tracking still reporting the period -- outstanding
    # money against sales that no longer existed. A period is one thing to the
    # operator, so all three go together.
    pay_window = f"(paid_on.gte.{lo},paid_on.lte.{hi})"
    payments = await db_delete(user, "payments", {"and": pay_window})
    # What the period still holds. A refused delete answers 200 with an empty
    # list, so "removed nothing" and "there was nothing to remove" look
    # identical from here unless we go back and count.
    payments_left = len(await db_get(
        user, "payments", {"select": "accsale", "and": pay_window, "limit": "20000"}))
    # Payments dated outside the period stay, even where their sales have just
    # gone. Each one is the market's record of money received, and it matches
    # its sales again the moment they are loaded back. Clearing them with
    # their sales lost PRE*BT*397227, R 7 200,00 paid on 1 September for 31
    # August: August was deleted and reloaded, September's report was not,
    # and the payment was simply gone. Until the sales return it is listed
    # as a payment with no matching sale, never counted as paid.
    closed = await _prune_dismissals(user)

    # Say plainly what is still there. A delete that removes nothing because the
    # rows belong to someone else returns 200 and an empty list, which read as
    # success while the period stayed on screen.
    left = []
    for where in windows:
        left += await db_get(user, "statements", {**where, "select": "id", "limit": "20000"})
    undated = await db_get(user, "statements", {
        "select": "id,last_sale,group_date,invoice_date,date_received,created_at",
        "last_sale": "is.null", "group_date": "is.null",
        "invoice_date": "is.null", "date_received": "is.null",
        "limit": "20000",
    })
    remaining = len(left) + len(analytics.filter_rows(undated, month, week))

    return {
        "deleted": len(deleted),
        "payments_deleted": len(payments),
        "payments_remaining": payments_left,
        "closed_cleared": closed,
        "remaining": remaining,
        "from": lo,
        "to": hi,
    }


async def _delete_everything(user: User) -> dict:
    """Clear the whole book: every statement, every payment, every closed line.

    Deliberately not reachable by leaving the period blank. db_delete refuses an
    unfiltered request precisely so a missing filter cannot empty a table, so
    each of these carries a filter that is true of every row and is written out
    rather than defaulted into.
    """
    statements = await db_delete(user, "statements", {"id": "gt.0"})
    payments = await db_delete(user, "payments", {"accsale": "not.is.null"})
    closed = await db_delete(user, "dismissals", {"ref": "not.is.null"})

    left = await db_get(user, "statements", {"select": "id", "limit": "20000"})
    pay_left = await db_get(user, "payments", {"select": "accsale", "limit": "20000"})
    return {
        "deleted": len(statements),
        "payments_deleted": len(payments),
        "closed_cleared": len(closed),
        "remaining": len(left),
        "payments_remaining": len(pay_left),
        "from": None,
        "to": None,
        "scope": "all",
    }


async def _delete_by_id(user: User, table: str, ids: list) -> list[dict]:
    """Delete rows by primary key, in batches a URL can carry.

    PostgREST takes the filter in the query string, so a few thousand ids in one
    `in.()` would be refused by the server long before the database saw it.
    """
    out: list[dict] = []
    for i in range(0, len(ids), 200):
        batch = ids[i : i + 200]
        if not batch:
            continue
        out += await db_delete(
            user, table, {"id": f"in.({','.join(str(x) for x in batch)})"}
        )
    return out


async def _prune_dismissals(user: User) -> int:
    """Drop closed-item records whose line no longer exists anywhere.

    A dismissal is named by what the line is (kind + ref), not by a row id, so
    deleting the history behind one leaves the record orphaned. Harmless while
    it sits there, but it would silently re-close the line if that delivery and
    commodity were ever imported again, so it is cleared with its period.

    Refs still backed by remaining history are kept, whichever period they fall
    in: a dismissal is not scoped to the period being deleted.
    """
    try:
        rows = await db_get(user, "dismissals", {"select": "kind,ref", "limit": "10000"})
    except Exception:  # noqa: BLE001 -- before migration 0016 there are none
        return 0
    if not rows:
        return 0

    live: set[str] = set()
    for r in await _history_rows(user):
        # Every name a line has gone by: its consignment now, and the delivery
        # note and commodity it was closed under before. The note was read off
        # supplier_ref here while the lists name it by dn, which is the Delivery
        # ID wherever the ref is blank, so those closures were pruned as orphans.
        live.add(tracking.item_ref({"dn": r.get("supplier_ref"), "product": r.get("product")}))
        live.add(tracking.item_ref({"dn": r.get("dn"), "product": r.get("product")}))
        if r.get("consignment_id"):
            live.add(f"c:{r['consignment_id']}")
    for p in await _saved_payments(user):
        if p.get("accsale"):
            live.add(f"ref:{p['accsale']}")

    gone = 0
    for row in rows:
        if row.get("ref") in live:
            continue
        await db_delete(user, "dismissals",
                        {"kind": f"eq.{row.get('kind')}", "ref": f"eq.{row.get('ref')}"})
        gone += 1
    return gone


# --- assistant ------------------------------------------------------------
# Plain-language questions over the recorded sales history, for buying
# decisions. Read-only: it can answer, never write. See `assistant` for why the
# money is computed deterministically rather than by the model.


@app.get("/api/forecast")
async def get_forecast(user: User | None = Depends(require_user)) -> dict:
    """What the book expects next month, and how fast things move.

    Computed, not generated: every figure here comes from ``forecast`` over the
    recorded rows, so this endpoint answers with or without an assistant key.
    The key only buys the written brief on top of it.
    """
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    return forecast.build(rows, payments)


@app.get("/api/procurement")
async def get_procurement(
    months: int = Query(scorecard.DEFAULT_MONTHS, ge=0, le=24),
    days: int = Query(procurement.DEFAULT_DAYS, ge=1, le=365),
    user: User | None = Depends(require_user),
) -> dict:
    """The buy plan: what to take on, how much, and where to send it.

    Computed from the saved history, so it answers with or without a Claude
    key; the key only buys the written plan on top. `months` sets the window
    the destinations are compared over, 0 for the whole book. `days` sets how
    long the order is for: the two are different questions.
    """
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    return procurement.build(rows, payments, months, days=days)


@app.get("/api/procurement/sheet.pdf")
async def get_order_sheet(
    months: int = Query(scorecard.DEFAULT_MONTHS, ge=0, le=24),
    days: int = Query(procurement.DEFAULT_DAYS, ge=1, le=365),
    prepared_for: str | None = Query(None, max_length=80),
    user: User | None = Depends(require_user),
) -> Response:
    """The same plan as a PDF, for whoever does the buying.

    Drawn on the server rather than left to the browser's print dialog: the
    desktop shell offers no print preview, so "save it as a PDF" there means
    hunting for the destination every time. This comes back as a file with a
    name on it, ready to send.
    """
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    plan = procurement.build(rows, payments, months, days=days)
    pdf = await run_in_threadpool(order_sheet.build, plan, None, prepared_for)
    return Response(
        pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{order_sheet.filename(plan)}"',
                 "Cache-Control": "no-store"})


@app.get("/api/procurement/dispatch.pdf")
async def get_dispatch_sheet(
    months: int = Query(scorecard.DEFAULT_MONTHS, ge=0, le=24),
    days: int = Query(procurement.DEFAULT_DAYS, ge=1, le=365),
    prepared_for: str | None = Query(None, max_length=80),
    user: User | None = Depends(require_user),
) -> Response:
    """Where the plan's cartons go, market by market, as a PDF for whoever
    loads the trucks. Drawn from the same plan as the screen."""
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    plan = procurement.build(rows, payments, months, days=days)
    pdf = await run_in_threadpool(dispatch_sheet.build, plan, None, prepared_for)
    return Response(
        pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{dispatch_sheet.filename(plan)}"',
                 "Cache-Control": "no-store"})


# --- the supplier's stock ----------------------------------------------------
# The supplier has a fixed amount each week. These read the list as pasted,
# split exactly that stock across the markets, have the agent write it up, and
# draw it as a dispatch sheet. The split is computed; the agent only words it.

def _supply_items(items: str) -> list[dict]:
    import json

    try:
        parsed = json.loads(items or "[]")
    except ValueError as exc:
        raise HTTPException(400, f"Could not read the stock list: {exc}") from exc
    if not isinstance(parsed, list):
        raise HTTPException(400, "The stock list should be a list of products and cartons.")
    return parsed


async def _supply_plan(user: User | None, items: str, months: int, days: int) -> dict:
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    full = procurement.build(rows, payments, months, days=days)
    return supply.plan(full, _supply_items(items), rows, payments, months)


@app.post("/api/procurement/supply/read")
async def read_supply(
    text: str = Form(..., max_length=20000),
    months: int = Form(scorecard.DEFAULT_MONTHS),
    user: User | None = Depends(require_user),
) -> dict:
    """The supplier's list as pasted, each line matched to the products it
    could be. Nothing is decided here: a line two products fit is left for a
    person to pick."""
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    full = procurement.build(rows, payments, months)
    return {"items": supply.read(text, full.get("lines") or []),
            "products": sorted(l["product"] for l in full.get("lines") or [])}


@app.post("/api/procurement/supply/plan")
async def plan_supply(
    items: str = Form(...),
    months: int = Form(scorecard.DEFAULT_MONTHS),
    days: int = Form(7),
    user: User | None = Depends(require_user),
) -> dict:
    """Exactly the supplier's stock, split across the markets."""
    plan = await _supply_plan(user, items, months, days)
    return {"dispatch": plan["dispatch"], "supply": plan["supply"],
            "horizon": plan.get("horizon"),
            "lines": [{"product": l["product"], "cartons": l["take_on"],
                       "planned": l.get("planned"), "send": l.get("send") or []}
                      for l in plan["lines"]]}


@app.post("/api/procurement/supply/brief")
async def brief_supply(
    items: str = Form(...),
    months: int = Form(scorecard.DEFAULT_MONTHS),
    days: int = Form(7),
    user: User | None = Depends(require_user),
) -> dict:
    """The agent's loading instructions for this week's stock."""
    if not assistant.configured():
        raise HTTPException(503, assistant.NOT_SET_UP)
    plan = await _supply_plan(user, items, months, days)
    try:
        text = await run_in_threadpool(assistant.supply_brief, plan)
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"text": text, "model": assistant.model()}


@app.post("/api/procurement/supply/dispatch.pdf")
async def supply_dispatch_sheet(
    items: str = Form(...),
    months: int = Form(scorecard.DEFAULT_MONTHS),
    days: int = Form(7),
    prepared_for: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> Response:
    """This week's stock as a dispatch sheet, by market."""
    plan = await _supply_plan(user, items, months, days)
    pdf = await run_in_threadpool(dispatch_sheet.build, plan, None, prepared_for)
    name = dispatch_sheet.filename(plan).replace("zaco-dispatch", "zaco-supply-dispatch")
    return Response(
        pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "no-store"})


@app.post("/api/assistant/plan")
async def write_procurement_plan(
    months: int = Form(scorecard.DEFAULT_MONTHS),
    days: int = Form(procurement.DEFAULT_DAYS),
    user: User | None = Depends(require_user),
) -> dict:
    """The written buy plan over those figures, from Claude Haiku."""
    if not assistant.configured():
        raise HTTPException(503, assistant.NOT_SET_UP)
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    try:
        text = await run_in_threadpool(assistant.plan_brief, rows, payments, months)
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"text": text, "months": months, "model": assistant.model()}


@app.get("/api/assistant")
async def assistant_status(user: User | None = Depends(require_user)) -> dict:
    """Whether the assistant is usable, plus example questions for the UI."""
    return {"configured": assistant.configured(), "suggestions": assistant.SUGGESTIONS}


# --- conversations --------------------------------------------------------
# A conversation is kept so it can be picked up later, on another day or
# another device. It is the caller's own: every read and write goes through
# PostgREST as them, and the policies in 0022 only show a thread to whoever
# started it. Nothing here is needed to ask a question -- if the tables are not
# there yet, the answer still comes back and only the keeping of it fails.

THREAD_TITLE_CHARS = 70


def _thread_title(question: str) -> str:
    """Name a conversation after the question that started it."""
    title = " ".join(question.split())
    if len(title) > THREAD_TITLE_CHARS:
        title = title[:THREAD_TITLE_CHARS - 1].rstrip() + "\u2026"
    return title or "New conversation"


async def _thread_messages(user: User, thread_id: str) -> list[dict]:
    """One conversation's turns, oldest first."""
    return await db_get(user, "chat_messages", {
        "select": "id,role,body,findings,failed,created_at",
        "thread_id": f"eq.{thread_id}", "order": "id.asc", "limit": "400"})


async def _remember(user: User | None, thread_id: str | None, question: str,
                    turns: list[dict]) -> str | None:
    """Keep a question and what came back, in a thread, and return its id.

    Best effort on purpose: a conversation that cannot be filed is worth less
    than an answer that never arrives, so every failure here is swallowed and
    the caller still gets its answer.
    """
    if user is None:
        return None
    try:
        if not thread_id:
            made = await db_post(user, "chat_threads",
                                 [{"title": _thread_title(question), "created_by": user.id}])
            thread_id = made[0]["id"] if made else None
            if not thread_id:
                return None
        else:
            await db_patch(user, "chat_threads", {"id": f"eq.{thread_id}"},
                           {"updated_at": datetime.now(timezone.utc).isoformat()})
        await db_post(user, "chat_messages",
                      [{"thread_id": thread_id, "role": t["role"], "body": t["body"],
                        "findings": t.get("findings") or [],
                        "failed": bool(t.get("failed")), "created_by": user.id}
                       for t in turns])
        return thread_id
    except Exception:  # noqa: BLE001 -- see the docstring
        return None


@app.get("/api/assistant/threads")
async def list_threads(user: User | None = Depends(require_user)) -> dict:
    """The caller's conversations, most recently used first."""
    if user is None:
        return {"threads": []}
    try:
        rows = await db_get(user, "chat_threads", {
            "select": "id,title,created_at,updated_at",
            "order": "updated_at.desc", "limit": "60"})
    except Exception as exc:  # noqa: BLE001 -- before 0022 there is nothing to list
        return {"threads": [], "unavailable": _pending_migration(exc) or "0022_chat_threads"}
    return {"threads": rows}


@app.get("/api/assistant/threads/{thread_id}")
async def read_thread(thread_id: str, user: User | None = Depends(require_user)) -> dict:
    """One conversation, turn by turn."""
    if user is None:
        raise HTTPException(401, "Sign in to open a conversation.")
    return {"thread_id": thread_id, "messages": await _thread_messages(user, thread_id)}


@app.delete("/api/assistant/threads/{thread_id}")
async def delete_thread(thread_id: str, user: User | None = Depends(require_user)) -> dict:
    """Forget one conversation. The messages go with it (0022 cascades)."""
    if user is None:
        raise HTTPException(401, "Sign in to delete a conversation.")
    await db_delete(user, "chat_threads", {"id": f"eq.{thread_id}"})
    return {"deleted": thread_id}


@app.post("/api/assistant")
async def ask_assistant(
    question: str = Form(...),
    thread_id: str | None = Form(None),
    user: User | None = Depends(require_user),
) -> dict:
    """Answer a question about the sales history, in the thread it belongs to."""
    question = question.strip()
    if not question:
        raise HTTPException(400, "Ask a question first.")
    if len(question) > 2000:
        raise HTTPException(400, "That question is too long.")
    if not assistant.configured():
        raise HTTPException(
            503,
            assistant.NOT_SET_UP,
        )

    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    history: list[dict] = []
    if user is not None and thread_id:
        try:
            history = await _thread_messages(user, thread_id)
        except Exception:  # noqa: BLE001 -- a lost history is not a lost answer
            history = []
    try:
        # The lines closed off on Tracking, so "still owed" in the chat is the
        # figure on the screen behind it, not one that still counts them.
        closed = await _closed_refs(user)
        answer = await run_in_threadpool(assistant.ask, question, rows, payments, history,
                                         closed, await _market_summaries(user))
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc

    kept = await _remember(user, thread_id, question,
                           [{"role": "q", "body": question},
                            {"role": "a", "body": answer}])
    return {"question": question, "answer": answer, "rows_considered": len(rows),
            "thread_id": kept or thread_id, "remembered": bool(kept),
            "turns_recalled": len(history)}


@app.post("/api/assistant/analyse")
async def run_analysis(thread_id: str | None = Form(None),
                       user: User | None = Depends(require_user)) -> dict:
    """Run the analyst panel: several specialists, then a buying recommendation.

    Each specialist examines the same complete history from a different angle
    (how stock moved, what prices held, where it sold best, what next month
    looks like, when the money lands, what is changing),
    and a final pass weighs their findings against each other.
    """
    if not assistant.configured():
        raise HTTPException(
            503,
            assistant.NOT_SET_UP,
        )
    rows = await _history_rows(user)
    payments = await _saved_payments(user)
    try:
        out = await assistant.analyse(rows, payments, await _closed_refs(user))
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc
    question = "What does next month look like, and what should I buy?"
    kept = await _remember(user, thread_id, question,
                           [{"role": "q", "body": question},
                            {"role": "a", "body": out.get("recommendation") or "",
                             "findings": out.get("findings") or []}])
    return out | {"thread_id": kept or thread_id, "remembered": bool(kept)}


# --- reconciliation -------------------------------------------------------
# Payment Details reports carry the Nett the sales reports never print. Dropped
# here, each is reconciled against the accumulated Daily Sales history (the
# `statements` table) by Supplier Ref + Product across its date range: where the
# summed daily Sales Total equals the payment Gross, the Nett is trustworthy and
# is returned to fill onto those rows. Nothing is written back to the database
# (that is an admin-gated update); the caller applies the Netts to its workbook.

_RECON_COLUMNS = (
    "stm_no,supplier_ref,product,sales_total,group_date,market_agent,payment_refs"
)

# How far before a payment run to look for the sales it settles. Observed lag
# in real data runs to a few weeks; this is deliberately generous.
LOOKBACK_DAYS = 120


async def _accumulated_daily(user: User, dns: set[int], lo: str | None, hi: str | None) -> list[dict]:
    """Daily rows in the history for the payment's suppliers and date window."""
    if not dns:
        return []
    params = {
        "select": _RECON_COLUMNS,
        "supplier_ref": f"in.({','.join(str(d) for d in sorted(dns))})",
    }
    if lo and hi:
        # The window comes from the payment dates, but the sales it pays for
        # happened earlier -- sometimes weeks earlier. Reaching back well before
        # `lo` is what stops a June sale paid in July looking like it never
        # happened. The supplier filter keeps the query bounded regardless.
        from datetime import date as _date, timedelta

        try:
            start = (_date.fromisoformat(lo) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        except ValueError:
            start = lo
        params["and"] = f"(group_date.gte.{start},group_date.lte.{hi})"
    rows = await db_get(user, "statements", params)
    return [
        {
            "dn": r.get("supplier_ref"),
            "product": r.get("product"),
            "sales_total": r.get("sales_total"),
            "stm_no": r.get("stm_no"),
            # Present on CSV-sourced rows; enables the exact reference match.
            "payment_refs": r.get("payment_refs"),
        }
        for r in rows
    ]


# --- the document check ---------------------------------------------------
# The parsers are exact but they fail quietly: a header in a shape the pattern
# does not cover is not an error, it is a block that never appears. So the same
# file is read again by a model that is shown none of what was parsed, and the
# two readings are compared in `doc_check`. It runs on its own key
# (ANTHROPIC_API_KEY_DOCS) and on its own request, after the rows are already
# on screen: a check is worth waiting for, but not worth waiting for before
# seeing the work.

def _document_text(name: str, data: bytes) -> tuple[str, list[str]]:
    """The file as text, and as pages where it has them."""
    if csv_reports.looks_like_csv(name, data):
        text = csv_reports.decode(data)
        return text, [text]
    pages = pdf_to_page_texts(data)
    return "\n".join(pages), pages


def _parse_for_check(name: str, text: str, pages: list[str]) -> tuple[str, list[dict]]:
    """What this app reads out of that file, and which kind of file it is.

    Deliberately the same parsers the real endpoints use, called the same way:
    a check against a second, kinder reading of the document would prove
    nothing about what is actually saved.
    """
    if csv_reports.looks_like_csv(name, text.encode("utf-8", "ignore")) or "," in text[:200]:
        if csv_reports.is_payment_details_csv(text):
            return "payments", csv_reports.parse_payment_details_csv(text, name)
        if csv_reports.is_daily_sales_csv(text):
            return "sales", [r.model_dump() for r in
                             csv_reports.parse_daily_sales_csv(text, name)]
    if payment_details.is_payment_details(text):
        return "payments", payment_details.parse_payment_details(pages, name)
    if nett_adjustments.is_nett_adjustments(text):
        return "nett", []
    return "sales", [r.model_dump() for r in statements_from_pages(pages, name)]


@app.post("/api/documents/check")
async def check_documents(
    files: list[UploadFile] = File(...),
    user: User | None = Depends(require_user),
) -> dict:
    """Read the dropped reports again and say where the two readings differ.

    Answers per file, so one unreadable document never costs the check on the
    others. Nothing here writes anything: it reports.
    """
    if not doc_check.configured():
        raise HTTPException(503, doc_check.NOT_SET_UP)

    results: list[dict] = []
    for f in files:
        name = f.filename or "document.pdf"
        data = await f.read()
        try:
            text, pages = _document_text(name, data)
            kind, parsed = _parse_for_check(name, text, pages)
            if kind == "nett":
                results.append({"file": name, "kind": kind, "ok": None, "findings": [],
                                "note": "A Nett Adjustments report carries no rows to check."})
                continue
            out = await run_in_threadpool(doc_check.check, text, parsed, kind)
            results.append({"file": name, **out})
        except assistant.AssistantError as exc:
            results.append({"file": name, "ok": None, "findings": [], "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- one bad file, not a failed check
            results.append({"file": name, "ok": None, "findings": [],
                            "error": f"Could not check this file: {exc}"})

    findings = sum(len(r.get("findings") or []) for r in results)
    checked = [r for r in results if r.get("ok") is not None]
    return {
        "files": results,
        "checked": len(checked),
        "findings": findings,
        # True only when something was actually compared and all of it agreed.
        "ok": bool(checked) and all(r.get("ok") for r in checked),
        "model": assistant.model(),
    }


@app.post("/api/reconcile")
async def reconcile_payments(
    files: list[UploadFile] = File(...), user: User | None = Depends(require_user)
) -> dict:
    """Reconcile Payment Details report(s) against the accumulated daily history.

    A Summary of Deliveries dropped in the same place is the market's own
    statement of what it has paid; it is kept for Tracking to check every
    outstanding line against, and not matched as a payment.
    """
    records: list[dict] = []
    summaries: list[dict] = []
    warnings: list[str] = []
    empty: list[str] = []
    lo: str | None = None
    hi: str | None = None
    for f in files:
        name = f.filename or "payment.pdf"
        data = await f.read()
        if csv_reports.looks_like_csv(name, data):
            text = csv_reports.decode(data)
            if not csv_reports.is_payment_details_csv(text):
                raise HTTPException(400, f"“{name}” is not a Payment Details export.")
            records.extend(csv_reports.parse_payment_details_csv(text, name))
            if (w := _filter_warning(name, text)):
                warnings.append(w)
            rlo, rhi = csv_reports.date_range(text)
        else:
            pages = pdf_to_page_texts(data)
            text = "\n".join(pages)
            if market_summary.is_delivery_summary(text):
                summaries.extend(market_summary.parse(text, name))
                continue
            if not payment_details.is_payment_details(text):
                raise HTTPException(400, f"“{name}” is not a Payment Details report "
                                         f"or a Summary of Deliveries.")
            found = payment_details.parse_payment_details(pages, name)
            records.extend(found)
            rlo, rhi = payment_details.date_range(text)
            # An export with a Grand Total of nil is a valid report of "nothing
            # was paid that day". Silently contributing nothing looked instead
            # like the app had failed to read it -- on a real week six of eight
            # files were empty and nothing said so.
            if not found:
                span = f" for {rlo}" if rlo and rlo == rhi else (
                    f" for {rlo} to {rhi}" if rlo else "")
                empty.append(f"“{name}” records no payments{span}.")
        lo = rlo if lo is None else min(lo, rlo or lo)
        hi = rhi if hi is None else max(hi, rhi or hi)

    summary_note = await persist_market_summaries(user, summaries) if summaries else None
    if summaries and not records:
        # Only summaries in this drop: nothing to match, so say what was kept.
        return {"warnings": warnings + ([summary_note["warning"]]
                                        if summary_note.get("warning") else []),
                "reconciliation": [], "netts": {}, "payment_count": 0,
                "date_range": {"from": None, "to": None}, "daily_rows": 0,
                "market_summary": summary_note}

    if empty:
        head = (f"{len(empty)} of the {len(files)} files carry no payments"
                if len(empty) > 1 else "One file carries no payments")
        warnings.append(f"{head}: " + " ".join(empty)
                        + " Re-export those days if you expected figures on them.")

    # Record the payments before matching, so the Tracking tab knows what is
    # outstanding across the whole period rather than only while this file is
    # open. Best-effort: a failed write must not cost the operator their match.
    # The same payment in two files -- a day's report and the week's, or one
    # file dropped twice -- is one payment. Counted twice it doubled the money
    # matched against the sales on this very screen.
    records = payment_details.dedupe(records)
    payment_warning = await persist_payments(user, records)
    if payment_warning:
        warnings.append(payment_warning)

    dns = {r["dn"] for r in records if r["dn"] is not None}
    daily = await _accumulated_daily(user, dns, lo, hi) if user is not None else []

    # Row by row: a sale that names the payment it belongs to (the CSV export
    # does) is matched on that reference, which is exact; a sale that does not
    # (every PDF row) falls back to supplier ref + product name. Deciding this
    # once for the whole run meant one CSV row in the window sent every PDF row
    # down the reference path, where it has nothing to match on.
    summary = reconcile.reconcile_any(daily, records)
    reconcile.fill_netts_any(daily, records)
    # Per statement, SUMMED across the rows under it -- not one entry per row.
    # A dict comprehension keyed on the statement number silently kept whichever
    # row came last, so a statement covering three consignments reached the
    # workbook carrying one consignment's share of the money and the sheet
    # showed R1.71 against a R4 402 sale. The workbook then splits this total
    # back over those rows by their gross, which is the same ratio it was
    # apportioned by, so each row lands on its own share again.
    netts: dict[str, float] = {}
    for r in daily:
        if r.get("nett_total") is None or r.get("stm_no") is None:
            continue
        key = str(r["stm_no"])
        netts[key] = round(netts.get(key, 0.0) + float(r["nett_total"]), 2)

    # What each account sale kept, straight off the payment report -- no sales
    # side needed, so it works even where nothing reconciled. Measured against
    # the median of this same file rather than a rate of ours.
    rates = sorted(
        (rec["gross"] - rec["nett"]) / rec["gross"]
        for rec in records if rec.get("gross")
    )
    baseline = rates[len(rates) // 2] if len(rates) >= integrity.MIN_FOR_MEDIAN else None
    kept = []
    for rec in records:
        if not rec.get("gross"):
            continue
        rate = (rec["gross"] - rec["nett"]) / rec["gross"]
        base = baseline if baseline is not None else integrity.TYPICAL_RATE
        if rec["nett"] <= 0 or rate >= integrity.SEVERE_RATE:
            level = "severe"
        elif rate > base + integrity.OVER_MEDIAN_PP:
            level = "watch"
        else:
            continue
        kept.append({
            "accsale": rec.get("accsale"), "stm_no": rec.get("stm_no"),
            "dn": rec.get("dn"), "market_agent": rec.get("market_agent"),
            "date": rec.get("date"), "gross": round(rec["gross"], 2),
            "nett": round(rec["nett"], 2), "rate": round(rate, 4),
            "baseline": round(base, 4), "severity": level,
        })
    kept.sort(key=lambda d: (d["severity"] != "severe", -(d["gross"] - d["nett"])))

    return {
        # Both strategies can run in one pass now, so say which were used
        # rather than which one was chosen.
        "matched_on": " and ".join(
            [w for w, on in (("payment reference", any("reference" in r for r in summary)),
                             ("supplier ref and product", any("product" in r for r in summary)))
             if on]) or "nothing to match",
        "warnings": warnings,
        "reconciliation": summary,
        "deductions": {
            "going_rate": round(baseline, 4) if baseline is not None else None,
            "of": len(rates),
            "concerns": kept,
        },
        "netts": netts,
        "payment_count": len(records),
        "date_range": {"from": lo, "to": hi},
        "daily_rows": len(daily),
        # Money the report itself gives no product breakdown for, so it can
        # never match. Reported so the gap is explained rather than missing.
        "unattributed": reconcile.unattributed(records),
        "market_summary": summary_note,
    }


# --- TechnoFresh auto-pull ------------------------------------------------
# Every morning the app signs in to the TechnoFresh portal, pulls yesterday's
# Payment Details and Daily Sales one day at a time, and puts them through the
# same reading, placing and saving as an upload. See ``technofresh``.

_TF_MIGRATION = "0026_technofresh_autopull.sql"
# South Africa keeps one offset all year, so no time zone database is needed.
_SAST = timezone(timedelta(hours=2))


def _sa_today() -> date:
    return datetime.now(_SAST).date()


async def _tf_settings(user: User) -> dict | None:
    rows = await db_get(user, "technofresh_settings", {
        "select": "username,password_enc,schedule,enabled,updated_at", "id": "eq.1"})
    return rows[0] if rows else None


async def _tf_record(user: User, report: str, day: date, **fields) -> None:
    await db_post(user, "technofresh_days", [{
        "report": report, "day": day.isoformat(), "pulled_by": user.id,
        "pulled_at": datetime.now(timezone.utc).isoformat(), "csv": None,
        "found": 0, "saved": 0, "held": 0, "unpaid": 0, "message": None, **fields,
    }], upsert=True, on_conflict="report,day", resolution="merge-duplicates")


async def _tf_payments_day(user: User, day: date, text: str) -> dict:
    name = f"technofresh_payments_{day}.csv"
    records = payment_details.dedupe(csv_reports.parse_payment_details_csv(text, name))
    warning = await persist_payments(user, records) if records else None
    return {"status": "failed" if warning else "done", "found": len(records),
            "saved": 0 if warning else len(records),
            "message": warning or (None if records else "No payments that day.")}


_OVERLAP_CODES = {overlap.ALREADY, overlap.DIFFERS, overlap.REPEAT}


async def _tf_sales_day(user: User, day: date, text: str) -> dict:
    """Read a day like an upload, then save what needs nobody to look at it.

    A row already on the book is left as it was, as the review screen does.
    A row with no account sale number yet cannot be recorded at all, so the day
    stays "waiting" and is pulled again tomorrow. A row that needs a person (no
    short code, say) is held, and the day's file is kept for the review screen.
    """
    name = f"technofresh_sales_{day}.csv"
    result = await read_sales_files(user, [(name, text.encode("utf-8"))])
    rows = result.rows
    on_book = [r for r in rows if any(f.code in _OVERLAP_CODES for f in r.flags)]
    fresh = [r for r in rows if not any(f.code in _OVERLAP_CODES for f in r.flags)]
    unpaid = [r for r in fresh if r.stm_no is None]
    held = [r for r in fresh if r.stm_no is not None and (r.blocking or not r.market_agent)]
    ready = [r for r in fresh if r.stm_no is not None and not r.blocking and r.market_agent]
    warning = await persist_statements(user, ready) if ready else None
    if not warning and ready:
        await remember_delivery_notes(user, ready)
    notes = [warning] if warning else []
    if on_book:
        notes.append(f"{len(on_book)} already on the book.")
    if unpaid:
        notes.append(f"{len(unpaid)} not paid yet, so they have no account sale number; "
                     f"this day is pulled again until they do.")
    if held:
        notes.append(f"{len(held)} need a short code or a check before they can be saved.")
    notes += result.warnings
    status = ("failed" if warning else "waiting" if unpaid
              else "review" if held else "done")
    return {"status": status, "found": len(rows), "saved": 0 if warning else len(ready),
            "held": len(held), "unpaid": len(unpaid),
            "message": " ".join(notes) or (None if rows else "No sales that day.")}


async def run_technofresh_pull(user: User, *, scheduled: bool) -> dict:
    """Pull every day that is due, oldest first, within the time the server allows."""
    try:
        settings = await _tf_settings(user)
    except Exception as exc:  # noqa: BLE001
        if _pending_migration(exc) or "technofresh" in str(getattr(exc, "detail", exc)):
            return {"ran": False, "message": f"Run the {_TF_MIGRATION} migration first."}
        raise
    if not settings or not settings.get("username") or not settings.get("password_enc"):
        return {"ran": False, "message": "Enter the TechnoFresh login in Settings first."}
    today = _sa_today()
    if scheduled and not settings.get("enabled", True):
        return {"ran": False, "message": "Automatic pulls are switched off."}
    if scheduled and not technofresh.due_today(settings.get("schedule", "daily"), today):
        return {"ran": False, "message": "Weekly pulls run on Mondays."}

    since = (today - timedelta(days=technofresh.CATCH_UP_LIMIT + 14)).isoformat()
    held = await db_get(user, "technofresh_days", {
        "select": "report,day,status", "day": f"gte.{since}", "limit": "1000"})
    order = {"payments": 0, "sales": 1}
    plan = sorted(
        [(technofresh.PAYMENTS, d) for d in technofresh.days_to_pull("payments", held, today)]
        + [(technofresh.SALES, d) for d in technofresh.days_to_pull("sales", held, today)],
        key=lambda p: (p[1], order[p[0].key]))
    if not plan:
        return {"ran": True, "pulled": [], "left": 0, "message": "Everything is up to date."}

    started = time.monotonic()
    pulled: list[dict] = []
    try:
        password = technofresh.decrypt(settings["password_enc"])
        portal = await run_in_threadpool(technofresh.Portal, settings["username"], password)
    except technofresh.PortalError as exc:
        # Recorded against the days it was meant to pull, so they are tried
        # again next time and the screen says why they are missing.
        for report, day in plan:
            await _tf_record(user, report.key, day, status="failed", message=str(exc))
        return {"ran": True, "pulled": [], "left": len(plan), "message": str(exc)}

    left = 0
    with portal:
        for report, day in plan:
            if time.monotonic() - started > technofresh.TIME_BUDGET:
                left += 1
                continue
            try:
                text = await run_in_threadpool(portal.fetch, report, day)
                handle = _tf_payments_day if report is technofresh.PAYMENTS else _tf_sales_day
                outcome = await handle(user, day, text)
                outcome["csv"] = text
            except technofresh.PortalError as exc:
                outcome = {"status": "failed", "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 -- one bad day must not stop the rest
                outcome = {"status": "failed",
                           "message": f"Could not place this day: {getattr(exc, 'detail', exc)}"}
            await _tf_record(user, report.key, day, **outcome)
            pulled.append({"report": report.key, "day": day.isoformat(),
                           **{k: v for k, v in outcome.items() if k != "csv"}})
    message = (f"{left} more day(s) still to pull; press Pull now to carry on."
               if left else None)
    return {"ran": True, "pulled": pulled, "left": left, "message": message}


@app.get("/api/technofresh/status")
async def technofresh_status(user: User | None = Depends(require_user)) -> dict:
    """The login (never the password), the schedule, and recent days."""
    ready = {
        "key": bool(os.getenv("TECHNOFRESH_KEY")),
        "robot": bool(os.getenv("ZACON_ROBOT_EMAIL") and os.getenv("ZACON_ROBOT_PASSWORD")),
        "cron": bool(os.getenv("CRON_SECRET")),
    }
    if user is None:
        return {"server": ready, "settings": None, "days": []}
    try:
        settings = await _tf_settings(user)
        since = (_sa_today() - timedelta(days=21)).isoformat()
        days = await db_get(user, "technofresh_days", {
            "select": "report,day,status,found,saved,held,unpaid,message,pulled_at",
            "day": f"gte.{since}", "order": "day.desc,report.asc", "limit": "200"})
    except Exception:  # noqa: BLE001 -- before the migration
        return {"server": ready, "settings": None, "days": [], "migration": _TF_MIGRATION}
    shown = None
    if settings:
        shown = {"username": settings.get("username"),
                 "has_password": bool(settings.get("password_enc")),
                 "schedule": settings.get("schedule"), "enabled": settings.get("enabled"),
                 "updated_at": settings.get("updated_at")}
    return {"server": ready, "settings": shown, "days": days}


@app.post("/api/technofresh/settings")
async def save_technofresh_settings(
    username: str = Form(...),
    password: str = Form(""),
    schedule: str = Form("daily"),
    enabled: bool = Form(True),
    profile: dict = Depends(require_admin),
) -> dict:
    """Admins only. A new password is tried on the portal before it is kept."""
    user = User(id=profile["id"], email=profile.get("email"), token=profile["token"])
    if schedule not in ("daily", "weekly"):
        raise HTTPException(400, "Schedule must be daily or weekly.")
    record = {"id": 1, "username": username.strip(), "schedule": schedule,
              "enabled": enabled, "updated_by": user.id,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        if password:
            with await run_in_threadpool(technofresh.Portal, record["username"], password):
                pass
            record["password_enc"] = technofresh.encrypt(password)
        elif not ((await _tf_settings(user)) or {}).get("password_enc"):
            raise HTTPException(400, "Enter the TechnoFresh password.")
    except technofresh.PortalError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db_post(user, "technofresh_settings", [record], upsert=True,
                  on_conflict="id", resolution="merge-duplicates")
    return {"saved": True, "checked": bool(password)}


@app.post("/api/technofresh/pull")
async def pull_technofresh_now(user: User | None = Depends(require_user)) -> dict:
    """Pull now, as whoever pressed the button, whatever the schedule says."""
    if user is None:
        raise HTTPException(400, "Pulling from TechnoFresh needs a signed-in account.")
    return await run_technofresh_pull(user, scheduled=False)


@app.get("/api/technofresh/cron")
async def technofresh_cron(authorization: str = Header(default="")) -> dict:
    """Vercel Cron calls this at 08:00 SAST with the CRON_SECRET it was given."""
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise HTTPException(401, "Not the scheduler.")
    try:
        robot = await technofresh.robot_user()
    except technofresh.PortalError as exc:
        return {"ran": False, "message": str(exc)}
    return await run_technofresh_pull(robot, scheduled=True)


@app.get("/api/technofresh/file")
async def technofresh_file(report: str = Query(...), day: date = Query(...),
                           user: User | None = Depends(require_user)) -> Response:
    """A pulled day's CSV, so its held rows can be opened in the review screen."""
    if user is None:
        raise HTTPException(400, "Needs a signed-in account.")
    rows = await db_get(user, "technofresh_days", {
        "select": "csv", "report": f"eq.{report}", "day": f"eq.{day.isoformat()}"})
    if not rows or not rows[0].get("csv"):
        raise HTTPException(404, "That day has not been pulled.")
    return Response(rows[0]["csv"], media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="technofresh_{report}_{day}.csv"'})


# --- frontend -------------------------------------------------------------
# Mounted last so it never shadows an /api route. Serving the UI from the same
# origin as the API keeps the browser out of CORS and lets fetch() use paths.
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
