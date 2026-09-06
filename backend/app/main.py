"""Zacon API.

Endpoints mirror the three things the frontend does: open a workbook, extract a
round of statements, and append those rows back into the workbook.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import (
    analytics,
    assistant,
    config,
    csv_reports,
    lookup,
    nett_adjustments,
    payment_details,
    delivery,
    integrity,
    reconcile,
    stock,
    tracking,
)
from .supabase_auth import User, current_profile, db_delete, db_get, db_post, require_user
from .extraction import apply_group_dates, pdf_to_page_texts, statements_from_pages
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
    """Warn on rows whose statement is already in the saved history.

    Guards against appending the same PDF to the workbook twice. The check is on
    the statement number (Consignment ID / Account Sales No) -- the finest-grain
    identity, and already the workbook's unique key -- NOT the Delivery ID: one
    delivery legitimately spans several save rounds, so matching on it would
    false-warn on the normal multi-round workflow.

    Non-blocking (a warning): re-processing a corrected statement is legitimate
    and the save upserts rather than duplicating in the database. Skipped when no
    user is signed in (local dev has no history to compare against).
    """
    if user is None:
        return
    stm_nos = sorted({r.stm_no for r in rows if r.stm_no is not None})
    if not stm_nos:
        return
    where = {"stm_no": f"in.({','.join(str(n) for n in stm_nos)})"}
    try:
        existing = await db_get(
            user, "statements", {"select": "stm_no,consignment_id,market_agent,created_at,group_date", **where}
        )
    except Exception:  # noqa: BLE001 -- before migration 0010 there is no such column
        try:
            existing = await db_get(
                user, "statements",
                {"select": "stm_no,market_agent,created_at,group_date", **where}
            )
        except Exception:  # noqa: BLE001 -- a duplicate warning is not worth failing an import
            return
    # An account sale covers several consignments, each its own row, so identity
    # is the pair. Keyed on the statement alone, a consignment would be called a
    # duplicate because a *different* product on the same account sale was saved.
    # The trading day is part of identity now: the same consignment selling on
    # Monday and on Wednesday is two rows, not one recorded twice.
    seen: dict[tuple, dict] = {}
    for rec in existing:
        day = (rec.get("group_date") or "")[:10] or None
        seen[(rec["stm_no"], rec.get("consignment_id") or 0, day)] = rec
        seen.setdefault((rec["stm_no"], None, day), rec)
    for row in rows:
        day = row.date.isoformat() if row.date else None
        rec = seen.get((row.stm_no, row.consignment_id or 0, day))
        if rec is None and row.consignment_id is None:
            rec = seen.get((row.stm_no, None, day))
        if rec is None:
            continue
        when = (rec.get("created_at") or "")[:10]
        agent = rec.get("market_agent") or "another agent"
        on = f" on {when}" if when else ""
        row.flags.append(
            Flag(
                field="stm_no",
                severity="warning",
                code="duplicate",
                message=f"Already saved{on} ({agent}) — adding it again will duplicate it in the workbook.",
            )
        )


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
    known = await codes_map(user)
    rows: list[StatementRow] = []
    nett_map: dict[int, dict] = {}
    nett_reports = 0
    warnings: list[str] = []

    for f in files:
        name = f.filename or "statement.pdf"
        data = await f.read()

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

    # Column D depends on the whole group, so it is resolved across the batch.
    apply_group_dates(rows)

    # Opening Stock is a running balance per consignment, so it is settled once
    # across the whole round rather than per file: a consignment straddling two
    # weekly exports would otherwise restart at the full delivery in the second.
    stock.carry_forward(rows, await _sold_before(user, rows))
    stock.flag_impossible_stock(rows)

    # Warn on any statement already in the saved history (same PDF twice).
    await flag_duplicates(user, rows)

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
    records = [rec for r in rows if (rec := _statement_record(r, user)) is not None]
    if not records:
        return None
    try:
        # Insert new statements; silently skip any already recorded. Deliberately
        # NOT merge-duplicates: the statements table restricts UPDATE to admins
        # (a processed statement is a financial record staff shouldn't overwrite),
        # so an upsert that updates would be blocked by RLS. Re-saving a statement
        # therefore keeps the first recorded copy rather than erroring.
        await db_post(
            user,
            "statements",
            records,
            upsert=True,
            # One account sale settles several consignments, each of which is its
            # own row, so the statement number alone is no longer unique.
            on_conflict="market_agent,stm_no,consignment_id,group_date",
            resolution="ignore-duplicates",
        )
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
    # supplier_ref and dn are how a row finds its recorded purchase cost.
    "qty_received,last_sale,payment_refs,supplier_ref,dn,"
    # Rows are account sales; this is how several of them are recognised as one
    # delivery, so what was SENT is not counted once per account sale.
    "consignment_id,"
    # What came back, so Insights can report sold and returned rather than only
    # the net of the two.
    "cartons_returned,returns_total,"
    # What the market itself averaged, so the price can be checked against it.
    "market_avg"
)

# Columns that arrived with a later migration, newest group first. Asking a
# database for a column it does not have yet fails the WHOLE request, which
# would take Insights, the buy list and settlements down together over one
# column none of them strictly needs -- so each group is dropped in turn and the
# read retried, rather than the page going dark.
_LATE_COLUMNS = (
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
        "created_by": user.id,
    }


async def persist_payments(user: User | None, records: list[dict]) -> str | None:
    """Record reconciled payments, so outstanding money survives a refresh.

    Best-effort, exactly like ``persist_statements``: reconciliation must still
    return even if the write fails (the table's migration not run yet, say).
    Re-processing a report keeps the first recorded copy -- a payment is a
    financial record staff do not overwrite -- so the insert ignores duplicates.
    """
    if user is None:
        return None
    rows = [r for rec in records if (r := _payment_record(rec, user)) is not None]
    if not rows:
        return None
    try:
        await db_post(user, "payments", rows, upsert=True,
                      on_conflict="accsale", resolution="ignore-duplicates")
    except Exception as exc:  # noqa: BLE001 -- reconciliation must succeed regardless
        if (migration := _pending_migration(exc)):
            return (
                f"Payments were matched but not recorded: the database is missing the "
                f"{migration} migration. Run it in the Supabase SQL editor and re-drop "
                f"the report; nothing is lost."
            )
        return None
    return None


async def _saved_payments(user: User | None) -> list[dict]:
    """Every recorded payment, read as the caller so RLS applies."""
    if user is None:
        return []
    query = {
        "select": "accsale,stm_no,market_agent,supplier_ref,dn,paid_on,nett,gross,lines",
        "limit": "10000",
    }
    try:
        rows = await db_get(user, "payments", query)
    except Exception:  # noqa: BLE001 -- before migration 0015 there are none
        return []
    # reconcile expects a payment's date under "date" and its breakdown under
    # "lines"; the table stores the date as paid_on. Bridge the two shapes here.
    for r in rows:
        r["date"] = r.pop("paid_on", None)
        r["lines"] = r.get("lines") or []
    return rows


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
    return tracking.compute(sales, payments, start=date_from, end=date_to,
                            closed=closed, month=month, week=week)


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
    user: User | None = Depends(require_user),
) -> dict:
    """Delete the recorded statements for one month or week.

    Scoped to a period on purpose -- there is no "delete everything" path. This
    removes the sales history behind Insights and reconciliation for that
    period, so it should only be run once the period's payments are reconciled.
    Governed by RLS (a user may delete their own statements; admins any)."""
    if user is None:
        raise HTTPException(400, "History deletion is not available in local mode.")
    lo, hi = analytics.period_bounds(month, week)
    if not lo:
        raise HTTPException(400, "Choose a month or week to delete.")
    deleted = await db_delete(
        user, "statements", {"and": f"(group_date.gte.{lo},group_date.lte.{hi})"}
    )
    return {"deleted": len(deleted), "from": lo, "to": hi}


# --- assistant ------------------------------------------------------------
# Plain-language questions over the recorded sales history, for buying
# decisions. Read-only: it can answer, never write. See `assistant` for why the
# money is computed deterministically rather than by the model.


@app.get("/api/assistant")
async def assistant_status(user: User | None = Depends(require_user)) -> dict:
    """Whether the assistant is usable, plus example questions for the UI."""
    return {"configured": assistant.configured(), "suggestions": assistant.SUGGESTIONS}


@app.post("/api/assistant")
async def ask_assistant(
    question: str = Form(...), user: User | None = Depends(require_user)
) -> dict:
    """Answer a question about the sales history."""
    question = question.strip()
    if not question:
        raise HTTPException(400, "Ask a question first.")
    if len(question) > 2000:
        raise HTTPException(400, "That question is too long.")
    if not assistant.configured():
        raise HTTPException(
            503,
            "The assistant is not set up yet: ANTHROPIC_API_KEY is missing on the server.",
        )

    rows = await _history_rows(user)
    try:
        answer = await run_in_threadpool(assistant.ask, question, rows)
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"question": question, "answer": answer, "rows_considered": len(rows)}


@app.post("/api/assistant/analyse")
async def run_analysis(user: User | None = Depends(require_user)) -> dict:
    """Run the analyst panel: several specialists, then a buying recommendation.

    Each specialist examines the same complete history from a different angle
    (how stock moved, what prices held, where it sold best, what is changing),
    and a final pass weighs their findings against each other.
    """
    if not assistant.configured():
        raise HTTPException(
            503,
            "The assistant is not set up yet: ANTHROPIC_API_KEY is missing on the server.",
        )
    rows = await _history_rows(user)
    try:
        return await assistant.analyse(rows)
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc


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


@app.post("/api/reconcile")
async def reconcile_payments(
    files: list[UploadFile] = File(...), user: User | None = Depends(require_user)
) -> dict:
    """Reconcile Payment Details report(s) against the accumulated daily history."""
    records: list[dict] = []
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
            if not payment_details.is_payment_details(text):
                raise HTTPException(400, f"“{name}” is not a Payment Details report.")
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

    if empty:
        head = (f"{len(empty)} of the {len(files)} files carry no payments"
                if len(empty) > 1 else "One file carries no payments")
        warnings.append(f"{head}: " + " ".join(empty)
                        + " Re-export those days if you expected figures on them.")

    # Record the payments before matching, so the Tracking tab knows what is
    # outstanding across the whole period rather than only while this file is
    # open. Best-effort: a failed write must not cost the operator their match.
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
    }


# --- frontend -------------------------------------------------------------
# Mounted last so it never shadows an /api route. Serving the UI from the same
# origin as the API keeps the browser out of CORS and lets fetch() use paths.
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
