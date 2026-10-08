"""Pulling the two TechnoFresh reports straight from the portal.

The portal (crm.technofresh.co.za) has no API. Its CSV buttons are plain form
posts, found by recording them in a browser:

    Payment Details      POST /reports/view/24/<pdf|csv>   fromDate, toDate
    Daily sales details  POST /reports/view/8/<pdf|csv>    from_date, to_date

with market, agent and product left empty for "all". The PDF is what is
pulled: the app has read those reliably for months, and a PDF sales row is
filed under its Consignment ID, so it goes on the book the day it sold. A CSV
row waits for its account sale number, which the market assigns days later. The session is one
PHPSESSID cookie, got by posting the login form, and it dies after under two
hours idle, so every pull signs in afresh rather than keeping a session.

The portal password is stored encrypted (Fernet, key in TECHNOFRESH_KEY). The
key lives only in the environment, so the database alone never yields it.

The 8am job runs with nobody signed in. It signs in to Supabase as a dedicated
"robot" account (ZACON_ROBOT_EMAIL / ZACON_ROBOT_PASSWORD) so every write goes
through row-level security exactly like a person's would. The service_role key
is still never used.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from . import config, daily_sales, payment_details
from .extraction import pdf_to_page_texts
from .supabase_auth import User

PORTAL = "https://crm.technofresh.co.za"
REPORTS_PAGE = PORTAL + "/reports/"
PACE_SECONDS = 2.0          # between requests to the portal; it is a shared server
TIME_BUDGET = 45.0          # stop starting new days after this, the function has 60s
RECHECK_DAYS = 14           # how long a day's unpaid sales are pulled again
CATCH_UP_LIMIT = 31         # never reach further back than this in one go


@dataclass(frozen=True)
class Report:
    key: str
    title: str
    report_id: int
    from_field: str
    to_field: str
    extra: tuple[str, ...]

    def form(self, day: date) -> dict[str, str]:
        data = {"report_id": str(self.report_id), **{k: "" for k in self.extra}}
        data[self.from_field] = data[self.to_field] = day.isoformat()
        data["submit"] = "Run Report"
        return data


PAYMENTS = Report("payments", "Payment Details", 24, "fromDate", "toDate", ("market", "agent"))
SALES = Report("sales", "Daily sales details", 8, "from_date", "to_date",
               ("market", "agent", "product"))


class PortalError(Exception):
    """Something the person looking at the settings screen should read."""


# --- the stored password ---------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    key = os.getenv("TECHNOFRESH_KEY", "")
    if not key:
        raise PortalError("TECHNOFRESH_KEY is not set on the server, so the TechnoFresh "
                          "password cannot be stored or read.")
    return Fernet(key.encode())


def encrypt(password: str) -> str:
    return _fernet().encrypt(password.encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise PortalError("The stored TechnoFresh password cannot be read with this "
                          "server's TECHNOFRESH_KEY. Enter it again in Settings.") from exc


# --- the portal ------------------------------------------------------------

class Portal:
    """One signed-in portal session, closed when the pull is done."""

    def __init__(self, username: str, password: str):
        self._client = httpx.Client(follow_redirects=False, timeout=50,
                                    headers={"User-Agent": "ZacoAgents auto-pull"})
        self._last = 0.0
        self._sign_in(username, password)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._client.close()

    def _pace(self) -> None:
        wait = self._last + PACE_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _sign_in(self, username: str, password: str) -> None:
        try:
            self._client.get(PORTAL + "/user/login")
            self._pace()
            self._client.post(PORTAL + "/user/login", data={
                "username": username, "password": password, "submit": "Login"})
            self._pace()
            # Where the login lands is no guide (it can sit on /user/login
            # showing "Error: 404" when it worked), so ask for the Reports page.
            check = self._client.get(REPORTS_PAGE)
        except httpx.HTTPError as exc:
            raise PortalError(f"Could not reach TechnoFresh: {exc}") from exc
        if check.status_code != 200:
            raise PortalError("TechnoFresh did not accept the username or password.")

    def fetch(self, report: Report, day: date) -> bytes:
        """One day of one report as a PDF, or a PortalError saying why not."""
        self._pace()
        try:
            r = self._client.post(f"{PORTAL}/reports/view/{report.report_id}/pdf",
                                  data=report.form(day))
        except httpx.HTTPError as exc:
            raise PortalError(f"{report.title} {day}: could not reach TechnoFresh ({exc}).") from exc
        if r.is_redirect:
            raise PortalError(f"{report.title} {day}: TechnoFresh ended the session.")
        if r.status_code != 200:
            raise PortalError(f"{report.title} {day}: TechnoFresh answered HTTP {r.status_code}.")
        if not r.content.startswith(b"%PDF"):
            head = r.content[:400].decode("utf-8", errors="replace").lower()
            what = "a web page" if "<html" in head or head.lstrip().startswith("<") else "something else"
            raise PortalError(f"{report.title} {day}: TechnoFresh sent {what}, not a PDF.")
        return r.content


def is_report(report: Report, pages: list[str]) -> bool:
    """Whether a pulled PDF is the report asked for, read the way an upload is."""
    text = "\n".join(pages)
    return (payment_details.is_payment_details(text) if report is PAYMENTS
            else daily_sales.is_daily_sales(text))


# --- the robot account -----------------------------------------------------

async def robot_user() -> User:
    """Sign in to Supabase as the auto-pull account, so RLS applies to it."""
    email = os.getenv("ZACON_ROBOT_EMAIL", "")
    password = os.getenv("ZACON_ROBOT_PASSWORD", "")
    if not (email and password and config.auth_configured()):
        raise PortalError("ZACON_ROBOT_EMAIL and ZACON_ROBOT_PASSWORD are not set, so the "
                          "8am pull has no account to save with.")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{config.SUPABASE_URL}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": config.SUPABASE_ANON_KEY},
            json={"email": email, "password": password},
        )
    if r.status_code != 200:
        raise PortalError("The auto-pull account could not sign in to ZacoAgents.")
    body = r.json()
    return User(id=body["user"]["id"], email=email, token=body["access_token"])


# --- which days to pull ----------------------------------------------------

def days_to_pull(report: str, held: list[dict], today: date) -> list[date]:
    """The days this run should pull, oldest first, one request each.

    Yesterday, every day missed since the last one pulled (the job did not
    run, or failed), and for sales any recent day that still had sales with no
    account sale number: the market assigns those a few days late, and the
    app cannot record a sale without one.
    """
    yesterday = today - timedelta(days=1)
    mine = {date.fromisoformat(h["day"]): h for h in held if h["report"] == report}
    good = [d for d, h in mine.items() if h["status"] != "failed"]
    start = max(good) + timedelta(days=1) if good else yesterday
    start = max(start, yesterday - timedelta(days=CATCH_UP_LIMIT - 1))
    want = {start + timedelta(days=i) for i in range((yesterday - start).days + 1)}
    # A day that failed is tried again even when later days have since worked.
    want |= {d for d, h in mine.items()
             if h["status"] == "failed" and d >= yesterday - timedelta(days=CATCH_UP_LIMIT - 1)}
    if report == "sales":
        want |= {d for d, h in mine.items()
                 if h["status"] == "waiting" and d >= today - timedelta(days=RECHECK_DAYS)}
    return sorted(d for d in want if d <= yesterday)


def due_today(schedule: str, today: date) -> bool:
    """Weekly runs on Mondays and catches up the week, one day at a time."""
    return schedule != "weekly" or today.weekday() == 0
