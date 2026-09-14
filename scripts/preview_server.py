"""Serve the real frontend against invented data, with no database and no login.

Looking at a change means seeing every screen. The live app needs Supabase and
a signed-in session, and a plain local run has neither -- it falls back to demo
mode, where Tracking, Insights and Reports all show their "needs the live app"
placeholder and the only screen you can really look at is the drop zone.

So this serves the real page, read fresh from disk on every request (edit the
CSS, reload, see it), and answers /api from the real backend functions run over
generated rows. The shapes are the shapes the app actually gets, because they
come from the same code that builds them in production.

Nothing here touches the database, and none of it ships -- `api/index.py` is the
deployment. This is for looking at the app.

    python scripts/preview_server.py 8123    # then open http://127.0.0.1:8123
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import analytics, integrity, reports, tracking  # noqa: E402

PAGE = ROOT / "frontend" / "index.html"

PRODUCTS = [
    ("GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)", "Imp White Grapes", 380),
    ("GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET 5kg)", "Imp Pink Grapes", 380),
    ("GRAPES RALLI CLASS 1 NO SIZE (PUNNET 5kg)", "Imp Red SL Grapes", 375),
    ("GRAPES CLASS 2 NO SIZE (PUNNET 5kg)", "Imp Ralli Grapes", 52),
    ("PLUMS FORTUNE CLASS 2 LARGE (ECONOMIC PACK 8kg)", "Imp Plums", 100),
    ("PLUMS ELDORADO CLASS 1 NO SIZE (DOMPEL JUMBLE 10kg)", "Imp Plums", 68),
    ("NECTARINES OTHER CLASS 1 MEDIUM (MULTI LAYER TRAYER 11kg)", "IMP Nect", 95),
    ("STRAWBERRIES NO VARIETY NOT GRADED NO SIZE (PUNNET 2kg)", "Straw", 120),
    ("GRANADILLAS NO VARIETY NOT GRADED NO SIZE (CARTON 5kg)", "Grana", 210),
]
WHERE = [
    ("TSHWANE MARKET", "Farmers Trust"),
    ("JOBURG MKT - TFRESH", "Subtropico"),
    ("JOBURG MKT - BR / MAR", "Growfresh Marco"),
    ("DURBAN MARKET", "Growfresh Port Natal"),
    ("SPRINGS MARKET", "Subtropico"),
]


def fixtures() -> tuple[list[dict], list[dict]]:
    """A few months of trade, shaped the way the history is."""
    rnd = random.Random(7)
    today = date.today()
    sales, payments = [], []
    dn = 14500
    for back in range(150, 0, -3):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        for _ in range(rnd.randint(1, 4)):
            dn += 1
            product, desc, base = rnd.choice(PRODUCTS)
            market, agent = rnd.choice(WHERE)
            sent = rnd.choice([60, 120, 240, 336, 480, 600])
            stm = 118000000 + dn
            # A delivery sells down over a few days, which is what makes the
            # per-day chart and the slow-stock ageing look like real trade.
            left = sent
            for offset in range(rnd.randint(1, 4)):
                if left <= 0:
                    break
                sold = min(left, rnd.randint(1, max(2, sent // 3)))
                left -= sold
                price = round(base * rnd.uniform(0.75, 1.2), 2)
                sold_on = day + timedelta(days=offset)
                if sold_on > today:
                    break
                sales.append({
                    "market_agent": agent, "market": market, "product": product,
                    "description": desc, "cartons_sold": sold, "price": price,
                    "sales_total": round(sold * price, 2),
                    "cartons_returned": 0, "returns_total": 0.0,
                    "group_date": day.isoformat(), "last_sale": sold_on.isoformat(),
                    "invoice_date": day.isoformat(), "date_received": day.isoformat(),
                    "qty_received": sent, "dn": dn, "supplier_ref": dn,
                    "consignment_id": stm, "stm_no": stm, "nett_total": None,
                    "payment_refs": None, "source_file": "dailysales.pdf",
                    "market_avg": round(base * rnd.uniform(0.9, 1.1), 2),
                    "status": "ok", "created_at": day.isoformat(),
                })
            if rnd.random() < 0.55:
                gross = round(sum(r["sales_total"] for r in sales if r["dn"] == dn), 2)
                if gross:
                    payments.append({
                        "accsale": f"PRE*BT*{dn}", "dn": dn, "stm_no": stm,
                        "date": (day + timedelta(days=rnd.randint(3, 20))).isoformat(),
                        "gross": gross, "nett": round(gross * 0.84, 2),
                        "lines": [{"product": product, "sales_total": gross}],
                    })
    return sales, payments


SALES, PAYMENTS = fixtures()
print(f"fixtures: {len(SALES)} sales rows, {len(PAYMENTS)} payments")


def month_bounds(month: str | None) -> tuple[str | None, str | None]:
    if not month:
        return None, None
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def api(path: str, params: dict) -> dict:
    if path == "/api/health":
        return {"status": "ok", "auth_required": False, "supabase_url": "",
                "supabase_anon_key": "", "assistant": True, "build": "preview"}
    if path == "/api/me":
        return {"email": "vanzylvisser0@gmail.com", "role": "staff"}
    if path == "/api/lookup":
        return {"codes": {}}
    if path == "/api/tracking":
        return tracking.compute(SALES, PAYMENTS, closed=set(),
                                month=params.get("month"), week=params.get("week"))
    if path == "/api/analytics":
        month, week = params.get("month"), params.get("week")
        scoped = analytics.filter_rows(SALES, month, week)
        result = analytics.compute(scoped, analytics.trend_granularity(month, week))
        result["available"] = analytics.available_periods(SALES)
        result["filter"] = {"month": month, "week": week}
        result["integrity"] = integrity.summary(scoped)
        return result
    if path == "/api/reports/periods":
        months = sorted({(analytics.selling_day(r) or "")[:7] for r in SALES if r}, reverse=True)
        return {"periods": [{"period_month": m,
                             "first_sale": m + "-01", "last_sale": m + "-28"}
                            for m in months if m]}
    if path == "/api/reports/period":
        return reports.build(SALES, PAYMENTS, params.get("from"), params.get("to"))
    if path == "/api/assistant":
        return {"configured": True, "suggestions": [
            "Which product earned the most last month?",
            "Where am I getting the best price for grapes?",
            "What is taking too long to sell?"]}
    return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        params = {}
        for pair in u.query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = v
        if u.path.startswith("/api/"):
            try:
                body = json.dumps(api(u.path, params), default=str).encode()
                self._send(200, body, "application/json")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"detail": f"{type(exc).__name__}: {exc}"}).encode(),
                           "application/json")
            return
        html = PAGE.read_text(encoding="utf-8")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        self._send(200, b"{}", "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    print(f"preview on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
