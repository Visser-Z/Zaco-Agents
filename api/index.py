"""Vercel entry point.

Vercel's Python runtime serves the module-level ASGI app named `app`. We reuse
the existing FastAPI app so Vercel and the VPS run identical code.

Routing note: Vercel now hands a rewritten function the *destination* path, not
the original request path (a 2026 change for "backend framework projects").
`vercel.json` therefore rewrites every request to `/api/index/<original path>`,
and the wrapper below strips the `/api/index` prefix so FastAPI routes on the
real path again. Without this, every request arrives as `/api/index` and 404s.
"""

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import app as _fastapi_app  # noqa: E402

_PREFIX = "/api/index"


async def app(scope, receive, send):
    """Restore the real request path, then delegate to FastAPI."""
    if scope["type"] in ("http", "websocket"):
        path = scope.get("path", "")
        if path == _PREFIX or path.startswith(_PREFIX + "/"):
            real = path[len(_PREFIX):] or "/"
            scope = dict(scope)
            scope["path"] = real
            scope["raw_path"] = real.encode()
    await _fastapi_app(scope, receive, send)


__all__ = ["app"]
