"""Runtime configuration, read from the environment.

Zacon is an internal system. The primary access control is the network it is
reachable on (a Tailscale tailnet), not anything in this file -- these settings
exist so the app can be pinned down as a second layer, and so it fails closed if
it is ever accidentally started on a public interface.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before any getenv below runs. override=False so a real
# environment variable (e.g. set by systemd) still wins over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

# Tailscale hands every device an address in 100.64.0.0/10 (CGNAT range).
TAILNET = "100.64.0.0/10"
LOOPBACK = "127.0.0.0/8"


def _networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            # A malformed entry must not silently widen access.
            raise ValueError(f"ZACON_ALLOWED_NETWORKS contains an invalid entry: {part!r}")
    return out


# Comma-separated CIDRs permitted to reach the API. Empty disables the check and
# leaves access entirely to the network layer, which is the default because the
# tailnet is doing the real work.
ALLOWED_NETWORKS = _networks(os.getenv("ZACON_ALLOWED_NETWORKS", ""))

# Set ZACON_ALLOWED_NETWORKS="100.64.0.0/10,127.0.0.0/8" to restrict the app to
# tailnet devices plus local access -- a sensible belt-and-braces default once
# Tailscale is in place.

# --- Supabase -------------------------------------------------------------
# Read from the environment (see .env.example). Nothing here is a secret except
# SUPABASE_JWT_SECRET, which must never reach the browser.

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

# Auth can be disabled for local development only. In any deployed environment
# this must stay on -- the app is publicly reachable once it leaves the tailnet,
# and the login is the only thing standing in front of the data.
AUTH_REQUIRED = os.getenv("ZACON_AUTH_REQUIRED", "true").lower() not in ("0", "false", "no")


def auth_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def is_allowed(client_ip: str | None) -> bool:
    if not ALLOWED_NETWORKS:
        return True
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETWORKS)


# --- build identity -------------------------------------------------------
# Which commit is actually serving. Deploys have gone stale silently before --
# the push lands on the remote and the served build stays older -- and from
# outside there was no way to tell a deployed fix from an undeployed one.
# Vercel sets this; empty anywhere else, which reads as "unknown", not as a lie.

BUILD_SHA = (os.getenv("VERCEL_GIT_COMMIT_SHA", "") or os.getenv("ZACON_BUILD_SHA", ""))[:7]
