"""Plain-language assistant over the recorded sales history.

The operator asks a question in ordinary English ("which grapes sold best in
July?", "what should I buy more of?") and gets an answer grounded in the
`statements` history.

**The model does no arithmetic on money.** Every total, ranking and trend in the
prompt is computed by ``analytics`` -- the same deterministic code behind the
Insights dashboard. Claude receives those figures as established fact, plus the
underlying rows for detail questions, and its job is language: understanding
what was asked and explaining the answer. That keeps the rands exact while
letting the question be open-ended.

Nothing here writes to the database or the workbook. It is read-only by design:
a wrong answer costs a re-ask, never a corrupted financial record.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

from . import analytics, forecast, market_summary, procurement, scorecard, tracking

# Claude Haiku 4.5, the operator's choice: they hold the key and pay for what it
# uses. Overridable per deployment with ZACON_ASSISTANT_MODEL -- to try Sonnet if
# answers read thin, say -- and the request below is shaped for whichever family
# is named, so changing it cannot break the call.
DEFAULT_MODEL = "claude-haiku-4-5"

# Rows sent verbatim for detail questions. The whole history is far inside the
# context window, but this bounds cost and latency on a serverless request.
MAX_ROWS = 2000

# Thinking counts toward max_tokens, so leave headroom above the answer's length.
MAX_TOKENS = 16000

# How much Claude may reason before answering a question or weighing the panel.
# Modest on purpose: this runs inside a 60s serverless function.
THINKING_BUDGET = 4000

# Panel analysts each have one narrow brief and are asked to be brief, so they
# run without thinking and, crucially, concurrently -- the panel has to finish
# inside the same 60s budget as a single answer.
ANALYST_MAX_TOKENS = 8000

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model() -> str:
    return (os.getenv("ZACON_ASSISTANT_MODEL") or DEFAULT_MODEL).strip()


def _thinking(model_id: str, budget: int | None) -> dict:
    """Request fields for thinking, in the shape this model accepts.

    The families disagree, and each rejects the other's form with a 400. Haiku
    4.5 takes a fixed budget and errors on `effort`; the 4.6-and-later models
    take adaptive thinking with an effort level, and Opus 5, Sonnet 5 and Opus
    4.7/4.8 reject a budget outright. The assistant used to send the adaptive
    form unconditionally, which on Haiku failed every single question.

    `budget` None means no thinking: the narrow analyst briefs do not need it.
    """
    if "haiku" in model_id:
        return {"thinking": {"type": "enabled", "budget_tokens": budget}} if budget else {}
    return {"thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium" if budget else "low"}}


def _uses_fallbacks(model_id: str) -> bool:
    """Server-side refusal fallbacks exist for the models whose safety
    classifiers can decline a request (Opus 5 and the Fable tier). Haiku has
    nothing to fall back from, so it is asked plainly rather than sent a beta
    it would reject and then asked twice."""
    return "haiku" not in model_id


# Two Claude keys, one per job, so each can be watched, capped and rotated on
# its own: the Intelligence tab (the written recommendation and the questions),
# and the document check that reads the payment and sales reports back to
# confirm the parser read them right. Either falls back to the one general key,
# so a deployment with a single key still works for both.
INTEL_KEY = "ANTHROPIC_API_KEY_INTEL"
DOCS_KEY = "ANTHROPIC_API_KEY_DOCS"
SHARED_KEY = "ANTHROPIC_API_KEY"


def _key(name: str) -> str | None:
    return (os.getenv(name) or os.getenv(SHARED_KEY) or "").strip() or None


def api_key() -> str | None:
    """The Intelligence tab's key."""
    return _key(INTEL_KEY)


def docs_api_key() -> str | None:
    """The document check's key. Nothing uses it yet; it is read here so the
    health check can already say whether it is in place."""
    return _key(DOCS_KEY)


def configured() -> bool:
    return bool(api_key())


# Every Anthropic API key starts with this. A value that does not is something
# else that was pasted into the box: an OAuth token, a project id, a name.
KEY_PREFIX = "sk-ant-"


def key_rejected() -> str:
    """What to tell the operator when Anthropic refuses the key.

    Says what is wrong with the value that was set without ever printing it:
    which variable it came from, whether it even looks like an API key, and how
    long it is, which is enough to spot a truncated paste or the wrong string
    entirely. The key itself never leaves the server.
    """
    key = api_key() or ""
    where = INTEL_KEY if os.getenv(INTEL_KEY) else SHARED_KEY
    if not key.startswith(KEY_PREFIX):
        return (f"Anthropic refused the key. The value in {where} does not look like an API "
                f"key: it should start with \"{KEY_PREFIX}\" and this one does not. Create one "
                f"at console.anthropic.com under API keys, paste it in Vercel, and redeploy.")
    return (f"Anthropic refused the key in {where} (it starts with \"{KEY_PREFIX}\" and is "
            f"{len(key)} characters). It may have been revoked, pasted incompletely, or "
            f"belong to an organisation this app is not billed through. Create a fresh key "
            f"at console.anthropic.com under API keys, paste it in Vercel, and redeploy.")


def docs_configured() -> bool:
    return bool(docs_api_key())


NOT_SET_UP = (f"The assistant is not set up yet: add {INTEL_KEY} (or {SHARED_KEY}) "
              "in Vercel under Settings, Environment Variables.")


SYSTEM = """\
You are the assistant inside ZacoAgents, the internal system of Zaco Agents \
(Pty) Ltd, a fresh-produce business in South Africa. You are answering the \
operator who runs the buying and selling day to day.

How the business works: Zaco sends consignments of fruit to fresh-produce \
markets (Tshwane, Joburg and others), where a market agent (Farmers Trust, \
Subtropico and others) sells them on Zaco's behalf and reports back what sold, \
at what price. Those reports are what you can see.

Each row below is one consignment of one product:
  date        when the consignment's sales are dated
  product     the short code the operator uses, e.g. "IMP Nect", "Imp Plums"
  market      the fresh-produce market it sold at
  agent       the market agent who sold it
  cartons     units sold
  price       average price per carton, in Rand
  value       gross sales value, in Rand (cartons x price)
  nett        a column the SALES report leaves blank on most rows. It is \
NOT whether the consignment has been paid, and a blank one says nothing at all \
about that.

Money that came in is reported separately, under "Payment and what is still \
owed", where the payment reports have already been matched onto the sales by \
the app. That section is the ONLY place to answer a question about what has \
been paid, what is outstanding, or what a market still owes. Never reason \
about payment from the nett column: an empty nett means the sales report did \
not print that figure, not that the money has not arrived. If the settlement \
section is present, it has the answer; use it, and quote its figures.

For a question about one month ("what is still owed for September"), answer from the "Month by month" table in that section and nowhere else. Never work a month's figure out by adding up outstanding lines: a line is one consignment over every day it sold, dated by its first sale, and one that started in August and kept selling into September belongs partly to each. The table has already split them, exactly as the Tracking tab does.

Outstanding money comes in two kinds, and the settlement section says which \
each line is. "Not paid yet" and "Cartons unpaid" are money genuinely owed: \
chase those. "Price differs", "Sales report missing?" and "No carton count" \
are questions, not debts: every carton may already be paid for, or the book \
may be missing a sales report. When asked what to chase, what is really owed, \
or who to phone, give the money to chase and name the rest separately as \
things to check.

Where each kind of answer lives. Every section below is computed by the app, \
the same figures the screens show; quote them rather than working anything out:
  money paid, owed or outstanding     "Payment and what is still owed"
  one month's money                   its "Month by month" table
  stock on hand, on the floor, unsold "Stock on hand, as of today"
  one month's stock on hand           "Still on hand, by the month it arrived"
  one day, or a run of days           "Sales per day"
  what sells where, and through whom  "What sells at each market"
  what to order, how much, where      "The buy plan"
  what next month looks like          "Projection"
  where a product pays best           "Where to send it"
If a question is about something the app shows, one of these has it. Only say \
the book has nothing on a subject after checking the section it belongs in, \
and then say which section you checked and what it said.

All money is South African Rand. Write amounts as "R 12 500,00".

Two limits you must respect rather than work around:

1. Do the arithmetic from the pre-computed totals given to you, not by adding \
up rows yourself. They are calculated exactly by the system. If a figure you \
need is not among them and you would have to sum many rows to get it, say so \
and give the closest figure you do have.

2. Nothing here records what Zaco PAID for the fruit. There are no cost or \
purchase prices anywhere in this data. So you can say what sells well, at what \
price, how fast, and in which market, but you cannot say what is most \
profitable. When a question turns on profit or margin, answer the part you can \
support from sales performance and state plainly that true margin needs \
purchase prices the system does not yet hold. Never estimate a cost price.

You are expected to make a call, not just describe the past. When asked what \
to buy, what will do well, or what to expect, give a clear recommendation and \
say what it rests on. The signals available to you, in rough order of use:

  how fast it sold   "days to sell" and "sold of sent". A product that clears \
in a day at a good price is worth backing; one that sat for two weeks, or where \
much of what was sent did not sell, is a warning even if its total looks large.
  what it earned     value, and price achieved per carton. Price per carton \
separates a genuinely strong line from one that only looks big because a lot \
of it was sent.
  where it sold      the same product can fetch different prices at different \
markets or through different agents. Say where to send it, not just what to buy.
  season             which months a product appears in, and how it moved in \
each. Fruit is seasonal, so a line that is strong now may be ending.

Be honest about how much history is behind a prediction. Say how many months \
and consignments you are reasoning from. With only a month or two you can point \
to a trend but you cannot call a seasonal pattern, so do not describe something \
as seasonal on that basis. Where the history is thin, say what the operator \
should watch to confirm it. Give your best answer with the caveat attached \
rather than refusing to answer.

Answer like a colleague who knows the trade: lead with the answer, keep it \
short, use the operator's own product codes, and give the rand figures that \
support the point. If the data does not cover what was asked, say that instead \
of guessing.\
"""


def _fmt(value: float | int | None) -> str:
    """South African convention: space thousands, comma decimal (12 500,00).

    Matches the format the system prompt asks the model to write back, so the
    figures it quotes look identical to the ones it was given.
    """
    if value is None:
        return ""
    whole, _, frac = f"{value:,.2f}".partition(".")
    return f"{whole.replace(',', ' ')},{frac}"


def _count(value: float | int | None) -> str:
    """Cartons and consignments are whole things -- "419", not "419,00"."""
    if value is None:
        return ""
    return f"{round(value):,}".replace(",", " ")


def build_context(rows: list[dict]) -> str:
    """The data block: exact aggregates first, then the underlying rows.

    Pure and deterministic, so the prompt can be unit-tested without an API key.
    """
    if not rows:
        return "No sales have been recorded yet, so there is no history to draw on."

    summary = analytics.compute(rows, "month")
    k = summary["kpis"]
    out: list[str] = []

    # The headline figures are NET of returns, which is what the workbook holds.
    # Where anything came back, the net is not the whole story -- a month with a
    # heavy return run reads as an ordinary one on it -- so both halves are
    # spelled out and the net is labelled as a net rather than left to look like
    # a plain sales figure.
    returned = k["cartons_returned"]
    net = " (net of returns)" if returned else ""
    out.append("## Totals across all recorded sales (exact)")
    out.append(f"Sales value: R {_fmt(k['total_value'])}{net}")
    out.append(f"Cartons sold: {_count(k['total_cartons'])}{net}")
    if returned:
        out.append(
            f"Sold before returns: {_count(k['cartons_sold'])} cartons, "
            f"R {_fmt(k['gross_value'])}"
        )
        out.append(
            f"Returned: {_count(returned)} cartons, R {_fmt(k['returns_value'])} "
            f"-- {k['return_rate'] * 100:.1f}% of what sold"
        )
    out.append(f"Consignments: {k['statement_count']}")
    out.append(f"Distinct products: {k['product_count']}")
    out.append(f"Period covered: {k['first_date']} to {k['last_date']}")

    def table(title: str, items: list[dict], note: str = "") -> None:
        if not items:
            return
        out.append("")
        out.append(f"## {title}{note}")
        out.append("name | value (R) | cartons | consignments")
        for item in items:
            out.append(
                f"{item['label']} | {_fmt(item['value'])} | "
                f"{_count(item['cartons'])} | {item['lines']}"
            )

    perf = analytics.product_performance(rows)
    if perf:
        out.append("")
        out.append("## How each product performed (exact)")
        out.append(
            "product | value (R) | cartons | avg price (R) | sold of sent | "
            "avg days to sell | slowest | months seen"
        )
        for p in perf[:25]:
            through = f"{p['sell_through']:.0%}" if p["sell_through"] is not None else "?"
            days = "same day" if p["avg_days_to_sell"] == 0 else (
                f"{p['avg_days_to_sell']}" if p["avg_days_to_sell"] is not None else "?")
            slow = p["slowest_days"] if p["slowest_days"] is not None else "?"
            out.append(
                f"{p['label']} | {_fmt(p['value'])} | {_count(p['cartons'])} | "
                f"{_fmt(p['avg_price'])} | {through} | {days} | {slow} | "
                f"{', '.join(p['months_seen'])}"
            )
        out.append(
            "(\"sold of sent\" is how much of what went to market actually sold. "
            "\"days to sell\" counts first sale to last: 0 means it cleared in a day. "
            "Blank means the report did not carry those figures.)"
        )

    table("Products by sales value (exact)", summary["best_sellers"][:25])
    table("Markets by sales value (exact)", summary["by_market"][:15])
    table("Agents by sales value (exact)", summary["by_agent"][:15])

    if summary["trend"]:
        out.append("")
        out.append("## Month by month (exact)")
        out.append("month | value (R) | cartons")
        for point in summary["trend"]:
            out.append(f"{point['period']} | {_fmt(point['value'])} | {_count(point['cartons'])}")

    recent = rows[-MAX_ROWS:]
    out.append("")
    out.append(f"## Individual consignments ({len(recent)} rows)")
    if len(rows) > len(recent):
        out.append(f"(most recent {len(recent)} of {len(rows)}; totals above cover all of them)")
    out.append("date | product | market | agent | cartons | price | value | nett")
    for row in recent:
        date = analytics.row_date(row)
        out.append(
            " | ".join(
                [
                    date.isoformat() if date else "",
                    analytics.product_label(row),
                    row.get("market") or "",
                    row.get("market_agent") or "",
                    _count(analytics.row_cartons(row)),
                    _fmt(row.get("price")),
                    _fmt(analytics.row_value(row)),
                    _fmt(row.get("nett_total")),
                ]
            )
        )
    return "\n".join(out)


def forecast_context(rows: list[dict], payments: list[dict]) -> str:
    """The forward-looking block: what next month is projected at, and why.

    Every figure here is computed by ``forecast``. The model is being handed a
    projection, not asked to make one -- asked to project, it would write a
    number that reads well and nothing in the answer would show where it came
    from.

    The caveats come first on purpose. They are the part a model is most likely
    to drop when summarising, and they are the part that decides whether the
    numbers under them mean anything.
    """
    f = forecast.build(rows, payments)
    p = f["projection"]
    if not p["products"] and not p["resting"]:
        return "There is not enough recorded history to project anything yet."

    out = ["## Projection for " + p["month"] + " (computed, do not recalculate)"]
    if p["caveats"]:
        out.append("")
        out.append("READ THESE FIRST. They limit everything below, and they must appear "
                   "in any answer that quotes a projected figure:")
        for c in p["caveats"]:
            out.append(f"- {c}")

    t = p["total"]
    out.append("")
    out.append(f"Whole book: R {_fmt(t['estimate'])} expected, somewhere between "
               f"R {_fmt(t['low'])} and R {_fmt(t['high'])}.")
    out.append(f"Built from these months: {', '.join(p['window'])}. "
               f"The book covers {', '.join(p['months_covered'])}.")

    if p["products"]:
        out.append("")
        out.append("### Per product")
        out.append("product | expected (R) | low (R) | high (R) | cartons | confidence | "
                   "months used | days to clear | cartons/day")
        for x in p["products"][:25]:
            out.append(" | ".join([
                x["product"], _fmt(x["estimate"]), _fmt(x["low"]), _fmt(x["high"]),
                _count(x["cartons_estimate"]), x["confidence"], " ".join(x["months_used"]),
                "?" if x["days_to_clear"] is None else str(x["days_to_clear"]),
                "?" if x["cartons_per_day"] is None else str(x["cartons_per_day"]),
            ]))

    if p["resting"]:
        out.append("")
        out.append("### Not projected: nothing sold recently")
        out.append("These are most likely out of season rather than failing. Do not "
                   "recommend buying them back without saying that is what you are doing.")
        out.append("product | last traded")
        for r in p["resting"][:20]:
            out.append(f"{r['product']} | {r['last_traded']}")

    outlets = f["outlets"]
    if outlets:
        out.append("")
        out.append("### What each outlet achieved, per product (exact)")
        out.append("The count is the volume the price was achieved on. A high price on "
                   "one carton is not a better outlet than a fair price on four hundred.")
        out.append("product | market | agent | price/carton (R) | cartons | consignments")
        for o in outlets[:60]:
            out.append(" | ".join([
                o["product"], o["market"] or "?", o["market_agent"] or "?",
                _fmt(o["price"]), _count(o["cartons"]), str(o["consignments"]),
            ]))

    lag = f["payment_lag"]
    if lag:
        out.append("")
        out.append("### How long each agent takes to pay (median days, last sale to money)")
        out.append("agent | usual days | slowest | payments measured")
        for l in lag:
            out.append(f"{l['market_agent']} | {l['days_to_pay']} | {l['slowest_days']} | {l['payments']}")

    return "\n".join(out)


# Questions worth surfacing in the UI, so the operator sees what it can do.
SUGGESTIONS = [
    "What sold best last month, and at what price?",
    "Which market pays the most for grapes?",
    "What should I buy more of going into next month?",
    "Which agent gets the better price for nectarines?",
    "Is anything selling worse than it did earlier in the year?",
]


# --- the analyst panel ----------------------------------------------------
# A buying decision turns on several things at once, and one prompt asked to
# weigh them all tends to blend them into a paragraph. So each specialist below
# gets the SAME complete data and examines one dimension properly; a final pass
# reconciles their findings into a recommendation.
#
# Note they are not given a slice of the data each. Splitting the rows would
# make every analyst reason from a partial picture, which is worse than the
# single-call assistant, not better. The specialisation is in the question, not
# the evidence.

ANALYSTS = [
    {
        "key": "movement",
        "title": "How stock moved",
        "brief": (
            "Examine how quickly stock cleared. Use days-to-sell and sell-through "
            "(the share of what was sent that actually sold). Identify what cleared "
            "fast and nearly completely, and what sat or was left unsold. Call out "
            "any product whose takings look healthy only because a large quantity "
            "was sent, while much of it did not sell or took a long time."
        ),
    },
    {
        "key": "price",
        "title": "What prices held up",
        "brief": (
            "Examine price achieved per carton. Identify which products command a "
            "premium and which are moving cheaply. Distinguish a genuinely strong "
            "line from one that only looks big because a lot of it was sent. Where "
            "the same product sold at very different prices, say so and note the range."
        ),
    },
    {
        "key": "placement",
        "title": "Where it sold best",
        "brief": (
            "Examine where produce sold: the market, and the agent who sold it. "
            "Where the same product went through more than one market or agent, "
            "compare what it fetched at each. The useful output is where to send a "
            "given product, not just which market is biggest overall. If everything "
            "went through a single market or agent, say that plainly instead of "
            "manufacturing a comparison."
        ),
    },
    {
        "key": "ahead",
        "title": "What next month looks like",
        "brief": (
            "Work from the projection block. Every figure in it is already "
            "computed: quote it, never recompute it, and never produce a number "
            "of your own. Say what the book expects next month overall and for "
            "the three or four products that carry it, always as the range rather "
            "than the middle figure alone. State the confidence beside each, and "
            "name the months it rests on. Where a product is projected on a single "
            "month, say that plainly -- it is a reading, not a forecast. Repeat "
            "the caveats that apply; they are not optional context. If the book is "
            "too short or too broken to support a projection, say so and stop, "
            "rather than dressing up a thin one."
        ),
    },
    {
        "key": "cash",
        "title": "When the money lands",
        "brief": (
            "Work from the payment-lag table and the outstanding position. Say how "
            "long each agent actually takes to pay, measured from the last sale to "
            "the money, and what that means for when next month's sales turn into "
            "cash. Name any agent who is slower than the others and by how much. "
            "Distinguish 'slow to pay' from 'has not paid': one is a cash-flow "
            "fact, the other is a debt to chase. Do not speculate about why."
        ),
    },
    {
        "key": "trend",
        "title": "What is changing",
        "brief": (
            "Examine change over time: month to month, which products appear when, "
            "what is growing or fading. Be strict about how much history supports "
            "any claim. With only a month or two, describe what the data shows and "
            "say clearly that it is too early to call a seasonal pattern. Do not "
            "invent a seasonal story."
        ),
    },
]

ANALYST_SYSTEM = """\
You are one of several specialists examining the same sales history for a \
fresh-produce business. Others are covering the other angles, so stay strictly \
within your brief and do not try to give the overall recommendation.

Be concrete and brief: at most six short bullets. Every claim names the product \
and the figures behind it. Use the pre-computed totals as given; do not add up \
rows yourself. If your angle is not supported by this data, say so in one line \
rather than padding. Never estimate a cost or purchase price -- none exists in \
this data.\
"""

SYNTHESIS_SYSTEM = """\
You are advising the operator of a fresh-produce business on the month ahead. \
Several specialists have each examined one angle of the same sales history; \
their findings follow, along with the exact figures they worked from.

Weigh them against each other and commit to a view. Where two findings pull in \
different directions -- a product earning well but selling slowly, say -- \
resolve it and explain which mattered more and why.

Every number you use is already computed in the data block. Quote those figures; \
never derive, scale or estimate one of your own. A projected figure is always \
given as its range, never as the middle number on its own.

Structure the answer as:
  What next month looks like
  What to buy, and how much
  Where to send it
  When the money comes in
  What we cannot tell yet, and what would fix that

Under "how much", use days-to-clear and cartons-per-day rather than value alone: \
a load bigger than the floor can absorb sits, and sitting fruit is what the \
slow-stock list is full of.

Keep it tight and give the rand figures that carry the argument. Carry the \
projection's caveats into the answer -- a reader who acts on the numbers without \
them has been misled. Never estimate a cost price: nothing records what was \
paid, so say what sells well and where, and be explicit that true profit needs \
purchase prices the system does not hold.\
"""


class AssistantError(RuntimeError):
    """The assistant could not answer -- surfaced to the operator as-is."""


# How many outstanding lines are listed one by one. Long enough to answer
# "what does Durban still owe" line by line, short enough not to crowd out the
# rest of the book.
MAX_OUTSTANDING = 60


def month_settlement(rows: list[dict], payments: list[dict],
                     closed=frozenset()) -> list[dict]:
    """Each month's own sales: what they sold for, what has been paid for
    them, and what is still owed on them, market by market.

    Exactly the call the Tracking tab makes when a month is open. Every sale is
    settled against the whole book first, and the month then only decides
    which sales are reported, so a month answers for its own sales and the
    months add up to the whole. Nothing is left for the model to add up.
    """
    months = sorted({d.strftime("%Y-%m") for r in rows if (d := analytics.row_date(r))})
    out = []
    for month in months:
        lo, hi = analytics.period_bounds(month=month)
        status = tracking.payment_status(rows, payments, closed, lo, hi)
        sold = sum(analytics.row_value(r) for r in tracking.in_period(rows, lo, hi))
        out.append({
            "month": month,
            "sold": round(sold, 2),
            "paid": status["total_paid"],
            "paid_gross": status["total_paid_gross"],
            "owed": status["still_to_come"],
            "debt": status["debt"],
            "to_check": status["to_check"],
            "lines": status["batches_outstanding"],
            "credit": status.get("credit_value") or 0.0,
            "markets": [{"market": m["market"], "agents": m.get("agents"),
                         "owed": m["owed"], "lines": m["items"]}
                        for m in status.get("outstanding_markets") or []],
        })
    return out


def settlement_context(rows: list[dict], payments: list[dict],
                       closed=frozenset(), summaries: list[dict] | None = None) -> str:
    """What has been paid and what is still owed, market by market.

    This block exists because of a real wrong answer. Asked what Durban still
    owed, the assistant said it could not tell, because "the nett column is
    blank for all Durban consignments" -- while the Tracking tab, on the same
    screen, showed R 107 740,00 outstanding across three named lines.

    Both readings were of the same book. The `nett` on a sales row is a column
    the SALES report leaves blank; what was actually paid lives in the payment
    reports and is matched onto the sales by ``tracking``. Given only the rows,
    the model could see an empty column and nothing else, so it drew the only
    conclusion available to it, and it was wrong. Everything the settlement
    knows is handed over here, already computed.
    """
    if not rows:
        return ""
    status = tracking.payment_status(rows, payments or [], closed)
    out = ["## Payment and what is still owed (computed by the app, do not recalculate)",
           "The `nett` column on the consignment rows below is a column the SALES report "
           "leaves blank. It is NOT whether a consignment has been paid. What was paid "
           "comes from the payment reports and is matched onto the sales here. Use this "
           "section for anything about money owed or received.",
           f"Paid so far: {_rand(status['total_paid_gross'])} gross, as the market counts it, "
           f"of which {_rand(status['total_paid'])} reached Zaco nett, over "
           f"{status['batches_paid']} settled batches.",
           f"Still to come: {_rand(status['still_to_come'])} over "
           f"{status['batches_outstanding']} batches"
           + (f", oldest {status['oldest_outstanding']}." if status['oldest_outstanding']
              else "."),
           f"Payment reports on record: {status['payments_recorded']}.",
           f"Of what is outstanding, {_rand(status['debt'])} is money genuinely owed "
           f"(nothing paid yet, or fewer cartons paid for than sold) and "
           f"{_rand(status['to_check'])} is to check rather than chase (every carton paid "
           f"for at a different price, payments counting more cartons than the book has "
           f"sold so a sales report is probably missing, or no carton count on the "
           f"payment). Each line below says which."]
    if status.get("credit_value"):
        out.append(f"Credits (paid more than the sale): {_rand(-status['credit_value'])} "
                   f"over {len(status.get('credits') or [])} lines.")
    if status.get("unmatched"):
        out.append(f"Paid but matching nothing on the book: {len(status['unmatched'])} "
                   f"payments.")
    # The Tracking tab's "payments to check": what the matcher could not place
    # with certainty, each waiting for a person to keep it or move it.
    flags = tracking.payment_flags(rows, payments or [])
    if flags["count"]:
        out.append(f"Payments waiting to be checked by hand on the Tracking tab: "
                   f"{flags['count']}, {_rand(flags['value'])}. Until each is settled, "
                   f"what is paid and owed may be out by that much:")
        for f in flags["items"][:20]:
            out.append(f"- {f['accsale']}, paid {f['date']}, {_rand(f['amount'])}, "
                       f"{f['product']}: {f['label']}. {f['message']}")
    else:
        out.append("Payments waiting to be checked by hand: none. Every payment is placed "
                   "by the rules with certainty, or has been checked by a person.")

    months = month_settlement(rows, payments or [], closed)
    if months:
        out.append("")
        out.append("### Month by month (exact; the same figures as the Tracking tab with "
                   "that month open)")
        out.append("A month here is the month the fruit SOLD in. Each month answers only for "
                   "its own sales, and the months add up to the totals above. For any "
                   "question about a particular month, answer from this table and nowhere "
                   "else.")
        out.append("'Paid, gross' is what the market itself reports as paid to date, before "
                   "the agent's deductions; compare it with the market's own reports. 'Reached "
                   "Zaco, nett' is what arrived after the deductions.")
        out.append("month | sold (R) | paid, gross (R) | reached Zaco, nett (R) | still owed "
                   "on it (R) | of which to chase (R) | of which to check (R) | lines outstanding")
        for m in months:
            out.append(f"{m['month']} | {_fmt(m['sold'])} | {_fmt(m['paid_gross'])} | "
                       f"{_fmt(m['paid'])} | "
                       f"{_fmt(m['owed'])} | {_fmt(m['debt'])} | {_fmt(m['to_check'])} | "
                       f"{m['lines']}")
        owing = [m for m in months if m["markets"] or m["credit"]]
        if owing:
            out.append("")
            out.append("Still owed in each month, by market (exact). Where a month also holds "
                       "a credit, it is given after the markets: the markets less the credit "
                       "is that month's figure in the table above.")
            for m in owing:
                parts = ", ".join(f"{x['market']} {_rand(x['owed'])} ({x['lines']} "
                                  f"line{'' if x['lines'] == 1 else 's'})"
                                  for x in m["markets"]) or "no market owes anything"
                if m["credit"]:
                    parts += f", less {_rand(-m['credit'])} of credit"
                out.append(f"- {m['month']}: {parts} = {_rand(m['owed'])}")
        negative = [m for m in months if m["owed"] < 0]
        for m in negative:
            out.append(f"({m['month']} comes out below zero: returns booked that month "
                       f"reversed sales made earlier, so it holds a credit, not a debt.)")

    markets = status.get("outstanding_markets") or []
    if markets:
        out.append("")
        out.append("### Still owed, all months together, by market (exact)")
        out.append("market | agent | owed (R) | lines | oldest")
        for m in markets:
            out.append(f"{m['market']} | {m.get('agents') or ''} | {_fmt(m['owed'])} | "
                       f"{m['items']} | {m.get('oldest') or ''}")

    lines = status.get("outstanding") or []
    # Each line held against the market's own Summary of Deliveries, where one
    # has been loaded, so "does the market agree?" has an answer.
    market = (market_summary.check(rows, payments or [], summaries, lines)
              if summaries else None)
    if market:
        out.append("")
        out.append("### Checked against the market's own Summary of Deliveries")
        out.append(f"The market's summaries cover {market['checked']} delivery lines, as of "
                   f"{market['as_of']}; {market['agree']} agree with the app to the cent. On "
                   "each outstanding line below, 'market says' gives the market's view: "
                   "'agrees' (the market also says it is unpaid, so it is a real debt), "
                   "'market says paid' (a payment is missing from the book: fetch the Payment "
                   "Details for the dates given), or 'not checked' (no summary loaded covers "
                   "that delivery: fetch the Summary of Deliveries for the month named, or the "
                   "market left that product off its summary).")
        for m in market["unchecked_months"]:
            out.append(f"Not checked yet: deliveries sent in {m['month_name']}, "
                       f"{_fmt(m['owed'])} owed. The {m['month_name']} Summary of Deliveries "
                       f"would settle them.")
    if lines:
        out.append("")
        out.append(f"### Every outstanding line, all months together "
                   f"({min(len(lines), MAX_OUTSTANDING)} of {len(lines)}, biggest first)")
        out.append("Each line is one consignment across ALL the days it sold, dated by the "
                   "day it FIRST sold. A consignment that started in one month and carried on "
                   "into the next belongs partly to each, so never filter or add these lines "
                   "up to get a month's figure: use the month table above. 'Unpaid since' "
                   "is the oldest sale on the line still waiting for money, and 'days "
                   "unpaid' counts from it to today: that is how long a line has been owed.")
        out.append("market | agent | delivery note | product | sold (R) | paid (R) | "
                   "owed (R) | first sold | unpaid since | days unpaid | cartons sold | "
                   "cartons paid for | why" + (" | market says" if market else ""))
        today = date.today()
        for r in lines[:MAX_OUTSTANDING]:
            paid_ctn = r.get("cartons_paid")
            since = r.get("unpaid_since") or r.get("date")
            waited = (today - date.fromisoformat(since[:10])).days if since else None
            out.append(" | ".join([
                str(r.get("market") or ""), str(r.get("market_agent") or ""),
                str(r.get("dn") or ""), str(r.get("product") or ""),
                _fmt(r.get("daily_total")), _fmt(r.get("payment_gross")),
                _fmt(r.get("owed")), str(r.get("date") or ""),
                str(since or ""), "" if waited is None else str(waited),
                _count(r.get("cartons_sold") or 0),
                "not printed" if paid_ctn is None else _count(paid_ctn),
                tracking.REASONS.get(r.get("reason"), "")]
                + ([_market_says(r.get("market_view") or {})] if market else [])))
    return "\n".join(out)


# --- every screen, in words ----------------------------------------------
# The rule these follow: anything a screen shows, the chat is handed, computed
# by the same call that screen makes. Asked what was on hand for September, the
# chat said there was no stock on hand at all, and it was right about what it
# had been given: the Stock on Hand block had never been passed to it. A
# question the app can answer on screen must never be one the chat cannot.

# How many trading days the day-by-day block covers, newest first. Two months
# answers "what did we sell on the 17th" without crowding out the rest.
DAILY_DAYS = 62


def _tier_word(tier: str | None) -> str:
    return {"red": "red (14 days or more)", "orange": "orange (7 to 13 days)",
            "green": "green (under 7 days)"}.get(tier or "", tier or "")


def stock_context(rows: list[dict], closed=frozenset(), today=None) -> str:
    """Stock on hand, as the Tracking tab shows it, and per arrival month."""
    if not rows:
        return ""
    today = today or date.today()
    soh = tracking.stock_on_hand(rows, today, closed)
    out = ["## Stock on hand, as of today (computed by the app, the Tracking tab's "
           "Stock on Hand block, do not recalculate)",
           "Cartons still unsold on the market floor: what each consignment was sent, less "
           "what it has sold. Also called what is on the floor, what is sitting, or unsold "
           "stock. Coloured by days since it arrived: green under 7 days, orange 7 to 13, "
           "red 14 or more.",
           f"Total: {_count(soh['cartons_left'])} cartons over {soh['items']} lines at "
           f"{len(soh['markets'])} markets; red {soh['counts'].get('red', 0)}, orange "
           f"{soh['counts'].get('orange', 0)}, green {soh['counts'].get('green', 0)}."]
    if not soh["markets"]:
        out.append("Nothing is on hand: every carton sent has sold.")
        return "\n".join(out)

    out += ["", "### By market", "market | agent | cartons left | lines | red | orange | green"]
    for m in soh["markets"]:
        out.append(f"{m['market']} | {m.get('agents') or ''} | {_count(m['cartons_left'])} | "
                   f"{m['items']} | {m.get('red', 0)} | {m.get('orange', 0)} | {m.get('green', 0)}")

    out += ["", "### Every line on hand, oldest first within each market",
            "market | agent | delivery note | product | arrived | days on hand | sent | "
            "left | colour"]
    for m in soh["markets"]:
        for r in m.get("lines") or []:
            out.append(" | ".join([
                str(r.get("market") or m["market"]), str(r.get("market_agent") or ""),
                str(r.get("dn") or ""), str(r.get("product") or ""),
                str(r.get("arrived") or ""), str(r.get("days_on_hand") or 0),
                _count(r.get("cartons_sent") or 0), _count(r.get("cartons_left") or 0),
                _tier_word(r.get("tier"))]))

    # "What is on hand for September" is what September put on the floor and
    # has not sold yet: the Tracking tab with that month open, exactly.
    months = sorted({str(r["arrived"])[:7] for m in soh["markets"]
                     for r in m.get("lines") or [] if r.get("arrived")})
    if months:
        out += ["", "### Still on hand, by the month it arrived (the Tracking tab with that "
                    "month open)",
                "A question about stock on hand for a month means the stock that ARRIVED in "
                "that month and is still unsold today.",
                "month arrived | cartons left | lines | markets"]
        for month in months:
            lo, hi = analytics.period_bounds(month=month)
            part = tracking.stock_on_hand(rows, today, closed, lo, hi)
            where = ", ".join(f"{m['market']} {_count(m['cartons_left'])}"
                              for m in part["markets"])
            out.append(f"{month} | {_count(part['cartons_left'])} | {part['items']} | {where}")
    return "\n".join(out)


def daily_context(rows: list[dict], payments: list[dict], closed=frozenset(),
                  today=None) -> str:
    """Sales per day, as the Tracking tab shows it: what sold, where, and how
    much of each day's sales has been paid."""
    if not rows:
        return ""
    days = tracking.compute(rows, payments or [], today=today or date.today(),
                            closed=closed)["sales_by_day"]["days"]
    recent = sorted(days, key=lambda d: d["date"], reverse=True)[:DAILY_DAYS]
    out = [f"## Sales per day, the last {len(recent)} trading days (computed by the app, the "
           f"Tracking tab's Sales per day block, do not recalculate)",
           "Each day is what sold THAT day, net of returns, and how much of that day's "
           "sales has been paid for so far. Use this for any question about a particular "
           "day or run of days instead of adding up consignment rows.",
           "date | sold (R) | cartons | returned | paid for (R, nett) | still owed (R) | "
           "by market"]
    for d in recent:
        markets = ", ".join(f"{m['market']} {_rand(m['value'])}"
                            for m in d.get("markets") or [])
        out.append(f"{d['date']} | {_fmt(d['value'])} | {_count(d['cartons'])} | "
                   f"{_count(d.get('returned') or 0)} | {_fmt(d.get('paid') or 0)} | "
                   f"{_fmt(d.get('owed') or 0)} | {markets}")
    return "\n".join(out)


def markets_context(rows: list[dict]) -> str:
    """Which products sell at which market, through which agent, as Insights
    shows them market by market."""
    groups = analytics.market_groups(rows, "month") if rows else []
    if not groups:
        return ""
    out = ["## What sells at each market (exact, the Insights tab's markets)",
           "market | products and their agents, biggest first"]
    for g in groups:
        out.append(f"### {g['label']}: {_rand(g['value'])}, {_count(g['cartons'])} cartons")
        for p in (g.get("products") or [])[:12]:
            agents = ", ".join(p.get("agents") or [])
            out.append(f"- {p['label']}: {_rand(p['value'])}, {_count(p['cartons'])} cartons, "
                       f"{p['lines']} consignments{f', via {agents}' if agents else ''}")
    return "\n".join(out)


def plan_block(rows: list[dict], payments: list[dict], today=None) -> str:
    """The Procurement tab's plan, for a week and for a month side by side, so
    "what should I order this week" has an answer without a recalculation."""
    if not rows:
        return ""
    week = procurement.build(rows, payments or [], today=today, days=7)
    month = procurement.build(rows, payments or [], today=today, days=30)
    if not month["lines"]:
        return ""
    by_week = {l["product"]: l for l in week["lines"]}
    tw, tm = week["totals"], month["totals"]
    out = ["## The buy plan, the Procurement tab (computed by the app, do not recalculate)",
           "What to take on, less what is already on the floor, and where to send it. Every "
           "rand is what the market is expected to return, never a margin.",
           f"For the next 7 days: take on {_count(tw['cartons'])} cartons over "
           f"{tw['to_take_on']} lines, expected back {_rand(tw['expected_value'])}.",
           f"For the next month: take on {_count(tm['cartons'])} cartons over "
           f"{tm['to_take_on']} lines, expected back {_rand(tm['expected_value'])}. Room to "
           f"grow: {_count(tm['growth_cartons'])} more cartons worth about "
           f"{_rand(tm['growth_worth'])}, plus {tm['trials']} test loads.",
           "product | priority | take on, 7 days | take on, month | on the floor | send to | "
           "expected back, month (R) | room to grow | test load"]
    for l in month["lines"]:
        w = by_week.get(l["product"], {})
        grow = f"+{_count(l['headroom']['cartons'])}" if l.get("headroom") else ""
        trial = (f"{_count(l['trial']['cartons'])} to {l['trial']['market']}"
                 if l.get("trial") else "")
        where_to = (f"{l['market']} via {l['market_agent']}" if l.get("market")
                    else "no destination on record")
        out.append(f"{l['product']} | {l['priority']} | {_count(w.get('take_on', 0))} | "
                   f"{_count(l['take_on'])} | {_count(l['on_hand'])} | {where_to} | "
                   f"{_fmt(l['expected_value'])} | {grow} | {trial}")
    return "\n".join(out)


def _market_says(m: dict) -> str:
    """One outstanding line's verdict from the market, in a few words."""
    v = m.get("verdict")
    if v == "agrees":
        return f"agrees it is unpaid (as of {m.get('as_of')})"
    if v == "market_paid":
        return (f"market says paid as of {m.get('as_of')}: payment missing from the book, "
                f"fetch Payment Details {m.get('fetch_from')} to {m.get('fetch_to')}")
    if v == "owes_more":
        return f"market says {_fmt(m.get('market_unpaid'))} unpaid, more than the app"
    if m.get("reason") == "not_listed":
        return "not checked: the market's summary leaves this product off"
    return f"not checked: needs the {m.get('month_name') or 'right'} Summary of Deliveries"


def data_block(rows: list[dict], payments: list[dict] | None = None,
               closed=frozenset(), summaries: list[dict] | None = None) -> str:
    """Everything the model is given: what happened, then what is expected.

    Built in one place so a question and the panel always see the same book.
    `closed` is the lines the team has closed off on Tracking, so the chat's
    outstanding figures are the ones on the screen.
    """
    parts = [build_context(rows)]
    if rows:
        parts.append(settlement_context(rows, payments or [], closed, summaries))
        parts.append(stock_context(rows, closed))
        parts.append(daily_context(rows, payments or [], closed))
        parts.append(markets_context(rows))
        parts.append(plan_block(rows, payments or []))
        parts.append(forecast_context(rows, payments or []))
        parts.append(where_context(scorecard.build(rows, payments or [])))
    return "\n\n".join(p for p in parts if p)


def _rand(value) -> str:
    return "n/a" if value is None else "R " + f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def where_context(card: dict) -> str:
    """The destination comparison as the model reads it: verdicts first, then
    the figures behind each, all computed by ``scorecard``."""
    if not card.get("fruits"):
        return "## Where to send it\nNot enough recorded history to compare destinations."
    w = card["window"]
    out = [f"## Where to send it, {w['from']} to {w['to']} (computed, do not recalculate)",
           "Score per destination = rand back per carton sent: price per carton sold, less "
           "the agent's cut, times the share of what was sent that sold. A destination needs "
           f"{card['rules']['min_consignments']} consignments of a product to count.",
           *("Caveat: " + c for c in card["caveats"])]
    for fruit in card["fruits"]:
        out.append(f"\n### {fruit['label']}")
        for p in fruit["products"]:
            v = p["verdict"]
            where_to = f"{v.get('market')} via {v.get('market_agent')}"
            if v["kind"] == "best":
                verdict = (f"send to {where_to}: {_rand(v['back_per_carton'])} back per carton "
                           f"sent, {_rand(v['lead_per_carton'])} more than {v['runner_up']}")
            elif v["kind"] == "only":
                verdict = f"only ever sent to {where_to}; nothing to compare"
            elif v["kind"] == "thin":
                verdict = f"leaning {where_to}, but too little history to call it"
            else:
                verdict = "figures missing"
            out.append(f"- {p['product']} ({p['cartons']:.0f} ctn, {_rand(p['value'])}): {verdict}")
            for d in p["destinations"]:
                out.append(
                    f"    {d['market']} / {d['market_agent']}: {d['cartons']:.0f} ctn, "
                    f"{_rand(d['price'])}/ctn, vs market avg {_pct(d['vs_market'])}, "
                    f"sold {_pct(d['sell_through'])} of sent, {d['days_to_sell'] or 'n/a'} days "
                    f"to sell, agent cut {_pct(d['agent_cut'])}"
                    f"{'' if d['agent_cut_known'] else ' (usual rate, none paid here)'}, "
                    f"{_rand(d['back_per_carton'])} back per carton sent, "
                    f"{d['consignments']} consignments")
    return "\n".join(out)


PLAN_SYSTEM = """You write the buy plan at the top of the Procurement screen in ZacoAgents, for the operator of Zaco Agents, a South African fresh-produce business.

How the business works: Zaco takes fruit from growers on consignment, sends it to a market agent at a fresh-produce market, and earns a commission on what the market returns. There is no purchase price anywhere in this data, so never talk about margin, cost or profit: every rand figure you are given is what the market is expected to return.

You are given the plan already computed: every product worth taking on, its priority, how many cartons to take on (what is expected to sell, less what is already sitting on the floor), where it pays best, and the figures behind each. Some lines also carry room to grow: extra cartons on top of the expectation, where that market took everything sent, took it within a couple of days and paid about the going rate, and test loads at a market a product has never been to. Use only those figures. Never add, average or estimate anything yourself, and never name a product or a destination that is not in the list. Never suggest more of something the plan does not say there is room for.

Write it the way the operator will act on it:
- Open with the shape of it in one line: what period the order covers, how many lines to take on, how many cartons, and what the market is expected to return. Never call it a month unless the plan says the order covers a month.
- Then the Critical and High lines, each in one sentence: how much of what, where to send it, and the single figure that justifies it.
- Then the growth, which is the part that makes the month bigger than last month: name the lines with room to grow, how many extra cartons and what they should return, and the test loads worth taking a chance on, each with what it is worth and what to watch.
- Call out anything that still has stock on the floor, where taking on more would add to what is already unsold.
- Say plainly where the history is too thin to be sure, and where a product has only ever gone to one market so there is nothing to compare.
- Under 260 words. Plain sentences, a short list is fine, no headings. Money as "R 12 500,00"."""


def plan_context(plan: dict) -> str:
    """The computed plan as the model reads it: the totals, then line by line."""
    if not plan.get("lines"):
        return "## The buy plan\nNot enough recorded history to plan anything yet."
    t = plan["totals"]
    covers = (plan.get("horizon") or {}).get("label") or "the next month"
    out = [f"## Buy plan for {covers} (computed, do not recalculate)",
           f"The order covers {covers}. "
           f"To take on: {t['cartons']} cartons across {t['to_take_on']} of "
           f"{t['lines']} lines. Expected back from the market: {_rand(t['expected_value'])}. "
           f"Already on the floor: {t['on_hand']} cartons. "
           f"{t['untested']} products have only ever gone to one market.",
           f"Room to grow: {t['growth_cartons']} extra cartons across {t['growth_lines']} lines, "
           f"worth about {_rand(t['growth_worth'])} more, plus {t['trials']} test loads "
           f"({t['trial_cartons']} cartons) worth about {_rand(t['trial_worth'])} more.",
           *("Caveat: " + c for c in plan.get("caveats", []))]
    for group in plan["priorities"]:
        out.append(f"\n### {group['key'].title()} ({len(group['lines'])} lines, "
                   f"{group['cartons']} cartons to take on)")
        for l in group["lines"]:
            where_to = (f"{l['market']} via {l['market_agent']}" if l["market"]
                        else "no destination on record")
            lead = (f", {_rand(l['lead_per_carton'])} a carton better than the next market"
                    if l["where_kind"] == "best"
                    else ", only ever sent there" if l["where_kind"] == "only" else "")
            out.append(
                f"- {l['product']}: take on {l['take_on']} cartons "
                f"(expects {l['expected_cartons']:.0f}, {l['on_hand']} on the floor), "
                f"send to {where_to}{lead}. Expected {_rand(l['expected_value'])} "
                f"({_rand(l['expected_low'])} to {_rand(l['expected_high'])}, "
                f"{l['confidence']}). {'; '.join(l['reasons'])}."
                + _growth_note(l))
    return "\n".join(out)


def _growth_note(line: dict) -> str:
    """The room the plan sees in a line, in its own figures, for the write-up."""
    out = ""
    if h := line.get("headroom"):
        worth = f", worth about {_rand(h['worth'])} more" if h["worth"] is not None else ""
        out += (f" ROOM TO GROW: {h['cartons']} cartons on top of the "
                f"{line['expected_cartons']:.0f} expected{worth} from {h['market']} "
                f"({'; '.join(h['why'])}).")
    if t := line.get("trial"):
        out += (f" WORTH A TRY: a test load of {t['cartons']} cartons to {t['market']}"
                f"{' via ' + t['market_agent'] if t['market_agent'] else ''}, worth about "
                f"{_rand(t['worth'])} more than the same cartons where it goes now "
                f"({t['why']}); watch what it fetches against the market average and how "
                f"long it takes to clear.")
    return out


def plan_brief(rows: list[dict], payments: list[dict], months: int) -> str:
    """The written buy plan over the computed one."""
    plan = procurement.build(rows, payments, months)
    if not plan.get("lines"):
        raise AssistantError("There is not enough history yet to plan a buy.")
    return _short_answer(PLAN_SYSTEM, plan_context(plan))


SUPPLY_SYSTEM = """You tell the operator of Zaco Agents, a South African fresh-produce business, where to send this week's stock from the supplier.

How the business works: Zaco takes fruit on consignment, sends it to market agents at fresh-produce markets, and earns a commission on what the market returns. There is no purchase price anywhere in this data, so never talk about margin, cost or profit: every rand figure is what the market is expected to return.

You are given the supplier's stock and the split already computed: for each market and agent, the cartons of each product to send there, why that market (it sold what it got, how fast it clears, how much it has shown it can take), where a market is being sent more than it has taken before, and how each market pays, including money it owes and for how long. Use only those figures. Never move cartons between markets yourself, never add or estimate anything, and never name a product or market that is not in the list.

Write it as loading instructions:
- Open with one line: how many cartons of how many products, to how many markets.
- Then market by market, biggest load first: the market and agent, then each product and its cartons on one line, with the one reason that matters.
- Where a market is being sent more than it has taken before, say so plainly and that the price may give.
- Where a market is sitting on old money, say so before its load, and that it is the owner's call whether to send it more.
- Where the supplier has more of a product than the markets have been taking, or less than the plan wanted, say so in one line each.
- List anything on the supplier's list that could not be matched to a product on the book.
- Under 300 words. Plain sentences, short lists, no headings. Money as "R 12 500,00"."""


def supply_context(plan: dict) -> str:
    """The supplier's stock as split, for the model to write up."""
    d = plan.get("dispatch") or {}
    sup = plan.get("supply") or {}
    days = (plan.get("horizon") or {}).get("label") or "this week"
    out = ["## This week's supplier stock, split by market (computed, do not recalculate)",
           f"Stock: {sup.get('cartons', 0)} cartons of {sup.get('products', 0)} products, "
           f"for {days}."]
    for g in d.get("markets") or []:
        head = f"\n### {g['market']}" + (f" via {g['market_agent']}" if g.get("market_agent") else "")
        out.append(head + f": {g['cartons']} cartons, expected back {_rand(g.get('value') or 0)}.")
        if g.get("note"):
            out.append(f"Payment warning: {g['note']}.")
        elif g.get("days_to_pay") is not None:
            out.append(f"Pays about {g['days_to_pay']:.0f} days after the sale.")
        for l in g["lines"]:
            over = (f" That is {l['over']} cartons more than this market has taken in the time."
                    if l.get("over") else "")
            out.append(f"- {l['product']}: {l['cartons']} cartons. Why: {l.get('why') or 'n/a'}.{over}")
    for x in sup.get("over") or []:
        out.append(f"Supplier has more than the markets have been taking: {x['product']}, "
                   f"{x['have']} cartons against about {x['plan']} the plan expected to sell.")
    for x in sup.get("short") or []:
        out.append(f"Supplier has less than the plan wanted: {x['product']}, {x['have']} "
                   f"cartons against {x['plan']}.")
    for x in sup.get("unknown") or []:
        out.append(f"Not matched to any product on the book: \"{x['text']}\" ({x['cartons']} cartons).")
    return "\n".join(out)


def supply_brief(plan: dict) -> str:
    """Where to send this week's supplier stock, written over the computed split."""
    if not (plan.get("dispatch") or {}).get("markets") and not (plan.get("supply") or {}).get("unknown"):
        raise AssistantError("Nothing on the supplier's list matched a product with history.")
    return _short_answer(SUPPLY_SYSTEM, supply_context(plan))


WHERE_SYSTEM = """You write the recommendation at the top of the "Where to send it" section of ZacoAgents, for the operator of Zaco Agents, a South African fresh-produce business that consigns fruit to market agents at several fresh-produce markets.

You are given a computed comparison of every market and agent each product has gone to. Every figure in it is exact. Use only those figures: never add, average or estimate anything yourself, and never invent a destination.

Write for someone deciding where this week's loads go:
- Lead with the three to five moves that matter most, each naming the product, where to send it, and the rand figure that justifies it ("R 38 more back per carton sent than Durban").
- The deciding figure is rand back per carton sent, not the price. Where a higher price loses because of the agent's cut or fruit that did not sell, say so.
- Where a product has only ever gone to one place, say that trying a second market is the only way to learn whether it could do better, and only suggest it for products with real volume.
- Where the history is too thin to call, say so plainly rather than picking one.
- Keep it under 200 words. Plain sentences, a short list is fine, no headings. Money as "R 12 500,00"."""


def where_brief(rows: list[dict], payments: list[dict], months: int) -> str:
    """The written recommendation over the destination comparison.

    One short call. The comparison is computed first and handed over whole;
    the model's job is only to say which parts matter and why.
    """
    card = scorecard.build(rows, payments, months)
    if not card.get("fruits"):
        raise AssistantError("There is not enough history yet to compare destinations.")
    return _short_answer(WHERE_SYSTEM, where_context(card))


def _short_answer(system: str, prompt: str) -> str:
    """One short call: the figures are computed, the model only writes them up.

    Handed a whole computed block rather than the rows, so there is nothing for
    it to add up, and no way for a figure it prints to have come from anywhere
    but the block.
    """
    import anthropic

    key = api_key()
    if not key:
        raise AssistantError(NOT_SET_UP)
    client = anthropic.Anthropic(api_key=key)
    model_id = model()
    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **_thinking(model_id, None),
        )
    except anthropic.AuthenticationError as exc:
        raise AssistantError(key_rejected()) from exc
    except anthropic.NotFoundError as exc:
        raise AssistantError(f"The model {model_id!r} is not available to this API key.") from exc
    except anthropic.RateLimitError as exc:
        raise AssistantError("The assistant is busy right now. Try again shortly.") from exc
    except anthropic.BadRequestError as exc:
        raise AssistantError(f"The assistant could not take that request: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AssistantError(f"The assistant service failed ({exc.status_code}). Try again.") from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantError("Could not reach the assistant service.") from exc

    text = _text_of(response)
    if not text:
        raise AssistantError("No recommendation came back. Try again.")
    return text


# How much of a conversation goes back with the next question. Long enough to
# follow a line of thought ("and at Durban?"), short enough that the book, not
# the chat, stays the bulk of what is read.
HISTORY_TURNS = 12
HISTORY_CHARS = 4000


def conversation(history: list[dict] | None, question: str) -> list[dict]:
    """The turns so far, as the API wants them, with the new question last.

    Stored turns are (role, body) where role is the screen's own "q" and "a".
    Anything that failed is left out: an error message is not something the
    assistant said, and feeding it back invites an apology for it. The API
    refuses two turns of the same role in a row, so a question that never got
    an answer is dropped rather than doubled up.
    """
    turns: list[dict] = []
    for item in (history or [])[-HISTORY_TURNS:]:
        if item.get("failed"):
            continue
        body = str(item.get("body") or "").strip()[:HISTORY_CHARS]
        role = "user" if item.get("role") == "q" else "assistant"
        if not body:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] = body if role == "assistant" else turns[-1]["content"]
            continue
        turns.append({"role": role, "content": body})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    return turns + [{"role": "user", "content": question}]


def ask(question: str, rows: list[dict],
        payments: list[dict] | None = None,
        history: list[dict] | None = None,
        closed=frozenset(), summaries: list[dict] | None = None) -> str:
    """Answer `question` against the recorded sales `rows`.

    `history` is the conversation this question belongs to, so a follow-up can
    lean on what was already said instead of starting cold every time.
    """
    import anthropic

    key = api_key()
    if not key:
        raise AssistantError(
            NOT_SET_UP
        )

    client = anthropic.Anthropic(api_key=key)
    system = [
        {"type": "text", "text": SYSTEM},
        # The data block is stable between questions, so caching it makes each
        # follow-up question cheap. It re-caches whenever new sales are saved.
        {
            "type": "text",
            "text": data_block(rows, payments, closed, summaries),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    model_id = model()
    request = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": conversation(history, question),
        **_thinking(model_id, THINKING_BUDGET),
    }

    try:
        if _uses_fallbacks(model_id):
            # Opt-in insurance: if a safety classifier declines, the API answers
            # on a fallback model. If this account lacks the beta, ask plainly --
            # inside its own try, so a failure there is still handled below
            # rather than escaping as a raw 500, which is what it used to do.
            try:
                response = client.beta.messages.create(
                    **request, betas=[FALLBACK_BETA], fallbacks="default")
            except anthropic.BadRequestError:
                response = client.messages.create(**request)
        else:
            response = client.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise AssistantError(key_rejected()) from exc
    except anthropic.NotFoundError as exc:
        raise AssistantError(
            f"The model {model_id!r} is not available to this API key.") from exc
    except anthropic.BadRequestError as exc:
        raise AssistantError(f"The assistant could not take that request: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise AssistantError("The assistant is busy right now. Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantError("Could not reach the assistant service.") from exc

    if response.stop_reason == "refusal":
        raise AssistantError(
            "The assistant declined to answer that one. Try rephrasing the question."
        )

    answer = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return answer or "No answer came back. Try asking that a different way."


def _text_of(response) -> str:
    if getattr(response, "stop_reason", None) == "refusal":
        return ""
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


async def _one_call(client, system: list[dict], prompt: str, max_tokens: int,
                    budget: int | None):
    """A single call, shaped for the configured model."""
    model_id = model()
    request = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        **_thinking(model_id, budget),
    }
    if not _uses_fallbacks(model_id):
        return await client.messages.create(**request)
    try:
        return await client.beta.messages.create(
            **request, betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as exc:  # noqa: BLE001 -- narrowed by the retry below
        import anthropic

        if isinstance(exc, anthropic.BadRequestError):
            return await client.messages.create(**request)
        raise


async def analyse(rows: list[dict], payments: list[dict] | None = None,
                  closed=frozenset()) -> dict:
    """Run the analyst panel and synthesise a buying recommendation.

    The specialists run concurrently, so the wall clock is roughly one call plus
    the synthesis rather than the sum of five -- which is what keeps this inside
    a serverless request. They run at low effort because each has a single
    narrow brief; the synthesis, which has to weigh them against each other,
    runs higher.
    """
    import anthropic

    key = api_key()
    if not key:
        raise AssistantError(
            NOT_SET_UP
        )
    if not rows:
        raise AssistantError(
            "There are no saved sales to analyse yet. Process a round of PDFs and save first."
        )

    context = data_block(rows, payments, closed)
    block = {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}

    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        async with client:
            # Every analyst sees the whole dataset; only the brief differs. The
            # shared, cached data block means the extra calls are mostly cache
            # reads rather than full-price input.
            results = await asyncio.gather(
                *(
                    _one_call(
                        client,
                        [{"type": "text", "text": SYSTEM}, block,
                         {"type": "text", "text": ANALYST_SYSTEM}],
                        a["brief"],
                        ANALYST_MAX_TOKENS,
                        None,        # a narrow brief: no thinking, so all four stay fast
                    )
                    for a in ANALYSTS
                ),
                return_exceptions=True,
            )

            findings: list[dict] = []
            for spec, res in zip(ANALYSTS, results):
                if isinstance(res, Exception):
                    findings.append({**{k: spec[k] for k in ("key", "title")},
                                     "finding": "", "failed": True})
                else:
                    findings.append({**{k: spec[k] for k in ("key", "title")},
                                     "finding": _text_of(res), "failed": False})

            usable = [f for f in findings if f["finding"]]
            if not usable:
                raise AssistantError(
                    "None of the analysts could complete. Try again shortly."
                )

            briefing = "\n\n".join(
                f"### {f['title']}\n{f['finding']}" for f in usable
            )
            final = await _one_call(
                client,
                [{"type": "text", "text": SYSTEM}, block,
                 {"type": "text", "text": SYNTHESIS_SYSTEM}],
                f"The specialists reported the following.\n\n{briefing}\n\n"
                "Weigh these and give the buying recommendation.",
                MAX_TOKENS,
                THINKING_BUDGET,
            )
    except anthropic.AuthenticationError as exc:
        raise AssistantError(key_rejected()) from exc
    except anthropic.RateLimitError as exc:
        raise AssistantError("The assistant is busy right now. Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantError("Could not reach the assistant service.") from exc

    recommendation = _text_of(final)
    if not recommendation:
        raise AssistantError("The recommendation came back empty. Try again.")

    return {
        "recommendation": recommendation,
        "findings": findings,
        "rows_considered": len(rows),
    }
