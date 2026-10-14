"""Reads longer than Supabase's 1 000 row cap come back whole.

On 14 October 2026 the book reached 1 014 rows; the newest 14 were cut off
every screen without a word. These tests stand in a fake PostgREST that caps
replies the same way.
"""
import asyncio

import httpx

from app import supabase_auth
from app.supabase_auth import User

USER = User(id="u", email=None, token="t")


def fake(rows: list[dict], cap: int = 1000, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if seen is not None:
            seen.append(q)
        off, lim = int(q.get("offset", 0)), int(q.get("limit", cap))
        part = rows[off:off + min(lim, cap)]
        headers = {}
        if request.headers.get("prefer") == "count=exact":
            headers["content-range"] = f"{off}-{off + len(part) - 1}/{len(rows)}"
        return httpx.Response(200, json=part, headers=headers)
    real = httpx.AsyncClient
    return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)


def read(monkeypatch, rows, params, seen=None):
    monkeypatch.setattr(supabase_auth.config, "SUPABASE_URL", "https://db.example.test")
    monkeypatch.setattr(supabase_auth.httpx, "AsyncClient", fake(rows, seen=seen))
    return asyncio.run(supabase_auth.db_get(USER, "statements", params))


def test_a_book_past_the_cap_comes_back_whole(monkeypatch):
    rows = [{"id": i} for i in range(1014)]
    got = read(monkeypatch, rows, {"select": "id", "order": "group_date.asc", "limit": "10000"})
    assert len(got) == 1014 and got[-1] == {"id": 1013}


def test_paged_reads_carry_the_key_as_a_tiebreak(monkeypatch):
    seen = []
    read(monkeypatch, [{"id": i} for i in range(1500)],
         {"select": "id", "order": "group_date.asc", "limit": "10000"}, seen)
    assert all(q["order"] == "group_date.asc,id.asc" for q in seen)
    assert [q["offset"] for q in seen] == ["0", "1000"]


def test_a_short_read_is_one_request(monkeypatch):
    seen = []
    got = read(monkeypatch, [{"id": i} for i in range(50)], {"select": "id", "limit": "1"}, seen)
    assert got == [{"id": 0}] and len(seen) == 1


def test_a_limit_is_still_respected(monkeypatch):
    got = read(monkeypatch, [{"id": i} for i in range(3000)], {"select": "id", "limit": "1200"})
    assert len(got) == 1200
