"""Supabase authentication for the FastAPI backend.

Every request must carry the caller's Supabase access token. We verify it
locally rather than calling Supabase on each request, then forward that same
token to PostgREST so the database evaluates RLS as the caller.

The service_role key is never used here. It bypasses RLS entirely, so using it
for ordinary requests would leave every policy in place but enforcing nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from . import config


@dataclass(frozen=True)
class User:
    """The authenticated caller."""

    id: str          # auth.users.id -- matches profiles.id
    email: str | None
    token: str       # forwarded to PostgREST so RLS sees the real user


# --- token verification ---------------------------------------------------

_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient | None:
    """Cache a JWKS client for projects using asymmetric signing keys."""
    global _jwks_client
    if not config.SUPABASE_URL:
        return None
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"{config.SUPABASE_URL}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


def _decode(token: str) -> dict:
    """Decode and validate a Supabase access token.

    Supabase projects sign tokens either with a shared secret (HS256, older
    projects) or an asymmetric key published via JWKS (newer projects). We try
    whichever the token's own header asks for rather than guessing.
    """
    options = {"verify_aud": True}
    try:
        alg = jwt.get_unverified_header(token).get("alg", "HS256")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token") from exc

    try:
        if alg.startswith("HS"):
            if not config.SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "SUPABASE_JWT_SECRET is not configured.",
                )
            return jwt.decode(
                token,
                config.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                options=options,
            )

        client = _jwks()
        if client is None:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "SUPABASE_URL is not configured.",
            )
        signing_key = client.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
            audience="authenticated",
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc


async def current_user(authorization: str = Header(default="")) -> User:
    """FastAPI dependency: the signed-in caller, or 401."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = _decode(token)
    return User(id=claims["sub"], email=claims.get("email"), token=token)


# --- PostgREST access -----------------------------------------------------

def _headers(user: User) -> dict[str, str]:
    return {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {user.token}",
        "Content-Type": "application/json",
    }


# Supabase answers at most this many rows per request, whatever ``limit`` asks
# for, and says nothing when it cuts a result short. On 14 October 2026 the
# book reached 1 014 rows and the newest 14, read oldest first, silently fell
# off every screen: 14 October showed R 94 630 of R 141 320. So any read that
# may be longer is fetched in pages until the count the database reports.
PAGE = 1000

# Each table's primary key, appended to a paged read's order so that rows
# sharing a sort value (one group_date has dozens) keep one fixed order and no
# page repeats or skips a row at its edge.
_KEYS = {
    "statements": "id", "payments": "accsale", "chat_messages": "id", "chat_threads": "id",
    "consignment_deals": "id", "delivery_notes": "delivery_id", "dismissals": "kind,ref",
    "market_stock": "snapshot,market,fms_id,seq", "market_summaries": "delivery_id,product",
    "payment_checks": "accsale,product", "product_codes": "product", "profiles": "id",
    "purchase_costs": "id", "stock_carryovers": "month,ref", "stock_note_lines": "dn,line_no",
    "stock_notes": "dn", "suppliers": "id", "technofresh_days": "report,day",
    "technofresh_settings": "id",
}


def _ordered(path: str, order: str | None) -> str | None:
    """The read's own order with the table's key after it, as a tiebreak."""
    key = _KEYS.get(path)
    if not key:
        return order
    have = {part.split(".")[0] for part in (order or "").split(",") if part}
    extra = ",".join(f"{k}.asc" for k in key.split(",") if k not in have)
    return ",".join(x for x in (order, extra) if x) or None


async def db_get(user: User, path: str, params: dict | None = None) -> list[dict]:
    """Read from PostgREST as the caller, so RLS applies.

    Every row the read asks for, however many: a ``limit`` above ``PAGE``, or
    none at all, is fetched page by page (see ``PAGE``)."""
    params = dict(params or {})
    want = int(params.pop("limit")) if params.get("limit") else None
    url = f"{config.SUPABASE_URL}/rest/v1/{path}"

    async with httpx.AsyncClient(timeout=15) as client:
        async def page(offset: int, size: int, count: bool) -> httpx.Response:
            headers = _headers(user)
            if count:
                headers["Prefer"] = "count=exact"
            r = await client.get(url, headers=headers,
                                 params={**params, "limit": str(size), "offset": str(offset)})
            if r.status_code >= 400:
                raise HTTPException(r.status_code, f"Database read failed: {r.text}")
            return r

        if want is not None and want <= PAGE:
            return (await page(0, want, False)).json()

        params["order"] = _ordered(path, params.get("order"))
        if params["order"] is None:
            params.pop("order")
        first = await page(0, PAGE, True)
        rows = first.json()
        # "0-999/1014": the total after the slash, "*" when it is not known.
        total_text = (first.headers.get("content-range") or "").rpartition("/")[2]
        total = int(total_text) if total_text.isdigit() else None
        goal = min(x for x in (total, want) if x is not None) if (total or want) else None
        while rows and (goal is None or len(rows) < goal):
            size = PAGE if goal is None else min(PAGE, goal - len(rows))
            more = (await page(len(rows), size, False)).json()
            if not more:
                break
            rows += more
        return rows


async def db_post(
    user: User,
    path: str,
    payload: list[dict] | dict,
    upsert: bool = False,
    on_conflict: str | None = None,
    resolution: str = "merge-duplicates",
) -> list[dict]:
    """Write to PostgREST as the caller, so RLS applies.

    `on_conflict` names the unique columns to merge on when it is not the
    primary key (e.g. the statements table's (market_agent, stm_no) constraint).

    `resolution` chooses what a conflict does: "merge-duplicates" updates the
    existing row (needs UPDATE rights), "ignore-duplicates" skips it (INSERT
    only, so it never trips an admin-only UPDATE policy).
    """
    headers = _headers(user) | {"Prefer": "return=representation"}
    if upsert:
        headers["Prefer"] += f",resolution={resolution}"
    params = {"on_conflict": on_conflict} if on_conflict else None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{config.SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            json=payload,
            params=params,
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Database write failed: {r.text}")
    return r.json() if r.content else []


async def db_patch(user: User, path: str, params: dict, payload: dict) -> list[dict]:
    """Update rows through PostgREST as the caller, so RLS applies. Like the
    delete below, `params` MUST carry a filter: an unfiltered patch would
    rewrite the whole table."""
    if not params:
        raise HTTPException(400, "Refusing an unfiltered update.")
    headers = _headers(user) | {"Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"{config.SUPABASE_URL}/rest/v1/{path}",
            headers=headers, params=params, json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Database update failed: {r.text}")
    return r.json() if r.content else []


async def db_delete(user: User, path: str, params: dict) -> list[dict]:
    """Delete from PostgREST as the caller, so RLS applies. `params` MUST carry a
    filter -- an unfiltered delete would remove the whole table."""
    if not params:
        raise HTTPException(400, "Refusing an unfiltered delete.")
    headers = _headers(user) | {"Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(
            f"{config.SUPABASE_URL}/rest/v1/{path}", headers=headers, params=params
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Database delete failed: {r.text}")
    return r.json() if r.content else []


async def require_user(authorization: str = Header(default="")) -> User | None:
    """Like `current_user`, but a no-op when auth is switched off.

    Returns None only in local development (ZACON_AUTH_REQUIRED=false or no
    Supabase configured). Anywhere the app is internet-facing this always
    enforces a valid token.
    """
    if not (config.AUTH_REQUIRED and config.auth_configured()):
        return None
    return await current_user(authorization)


async def current_profile(user: User = Depends(current_user)) -> dict:
    """The caller's profile row, including their role."""
    rows = await db_get(
        user, "profiles", {"id": f"eq.{user.id}", "select": "id,email,full_name,role"}
    )
    if not rows:
        # The signup trigger creates this row; its absence means the user was
        # made outside the normal flow.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No profile for this account.")
    return rows[0] | {"token": user.token}


async def require_admin(profile: dict = Depends(current_profile)) -> dict:
    if profile.get("role") != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This action requires an admin account."
        )
    return profile
