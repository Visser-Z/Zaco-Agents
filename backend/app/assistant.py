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

from . import analytics, forecast, procurement, scorecard

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
  nett        what Zaco was actually paid after the agent's deductions, in \
Rand. Often blank, because it arrives later on a separate payment report.

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


def data_block(rows: list[dict], payments: list[dict] | None = None) -> str:
    """Everything the model is given: what happened, then what is expected.

    Built in one place so a question and the panel always see the same book.
    """
    parts = [build_context(rows)]
    if rows:
        parts.append(forecast_context(rows, payments or []))
        parts.append(where_context(scorecard.build(rows, payments or [])))
    return "\n\n".join(parts)


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

You are given the plan already computed: every product worth taking on, its priority, how many cartons to take on (what is expected to sell, less what is already sitting on the floor), where it pays best, and the figures behind each. Use only those figures. Never add, average or estimate anything yourself, and never name a product or a destination that is not in the list.

Write it the way the operator will act on it:
- Open with the shape of it in one line: how many lines to take on, how many cartons, and what the market is expected to return.
- Then the Critical and High lines, each in one sentence: how much of what, where to send it, and the single figure that justifies it.
- Call out anything that still has stock on the floor, where taking on more would add to what is already unsold.
- Say plainly where the history is too thin to be sure, and where a product has only ever gone to one market so there is nothing to compare.
- Under 220 words. Plain sentences, a short list is fine, no headings. Money as "R 12 500,00"."""


def plan_context(plan: dict) -> str:
    """The computed plan as the model reads it: the totals, then line by line."""
    if not plan.get("lines"):
        return "## The buy plan\nNot enough recorded history to plan anything yet."
    t = plan["totals"]
    out = [f"## Buy plan for {plan['month']} (computed, do not recalculate)",
           f"To take on: {t['cartons']} cartons across {t['to_take_on']} of "
           f"{t['lines']} lines. Expected back from the market: {_rand(t['expected_value'])}. "
           f"Already on the floor: {t['on_hand']} cartons. "
           f"{t['untested']} products have only ever gone to one market.",
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
                f"{l['confidence']}). {'; '.join(l['reasons'])}.")
    return "\n".join(out)


def plan_brief(rows: list[dict], payments: list[dict], months: int) -> str:
    """The written buy plan over the computed one."""
    plan = procurement.build(rows, payments, months)
    if not plan.get("lines"):
        raise AssistantError("There is not enough history yet to plan a buy.")
    return _short_answer(PLAN_SYSTEM, plan_context(plan))


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
        raise AssistantError("The server's Claude API key was rejected.") from exc
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


def ask(question: str, rows: list[dict],
        payments: list[dict] | None = None) -> str:
    """Answer `question` against the recorded sales `rows`."""
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
            "text": data_block(rows, payments),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    model_id = model()
    request = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": question}],
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
        raise AssistantError("The server's Claude API key was rejected.") from exc
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


async def analyse(rows: list[dict], payments: list[dict] | None = None) -> dict:
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

    context = data_block(rows, payments)
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
        raise AssistantError("The server's Claude API key was rejected.") from exc
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
