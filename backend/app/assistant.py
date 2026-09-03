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

from . import analytics

MODEL = "claude-opus-5"

# Rows sent verbatim for detail questions. The whole history is far inside the
# context window, but this bounds cost and latency on a serverless request.
MAX_ROWS = 2000

# Medium effort, not the `high` default: this runs inside a 60s serverless
# function, and Claude Opus 5 is unusually strong at the lower effort levels.
# Raise it if answers start feeling shallow on harder questions.
EFFORT = "medium"

# Adaptive thinking counts toward max_tokens, so leave headroom above the
# answer's own length.
MAX_TOKENS = 16000

# Panel analysts each have one narrow brief and are asked to be brief, so they
# run leaner and, crucially, concurrently -- the panel has to finish inside the
# same 60s budget as a single answer. Raise if their findings read thin.
ANALYST_EFFORT = "low"
ANALYST_MAX_TOKENS = 8000


def api_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or None


def configured() -> bool:
    return bool(api_key())


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
You are advising the operator of a fresh-produce business on what to buy next. \
Several specialists have each examined one angle of the same sales history; \
their findings follow, along with the exact figures they worked from.

Weigh them against each other and commit to a recommendation. Where two \
findings pull in different directions -- a product earning well but selling \
slowly, say -- resolve it and explain which mattered more and why.

Structure the answer as:
  What to buy more of, and why
  What to be careful with
  Where to send it
  What we cannot tell yet, and what would fix that

Keep it tight, use the operator's product codes, and give the rand figures that \
carry the argument. Say how much history is behind the advice. Never estimate a \
cost price: nothing records what was paid, so say what sells well and where, and \
be explicit that true profit needs purchase prices the system does not hold.\
"""


class AssistantError(RuntimeError):
    """The assistant could not answer -- surfaced to the operator as-is."""


def ask(question: str, rows: list[dict]) -> str:
    """Answer `question` against the recorded sales `rows`."""
    import anthropic

    key = api_key()
    if not key:
        raise AssistantError(
            "The assistant is not configured yet: ANTHROPIC_API_KEY is not set on the server."
        )

    client = anthropic.Anthropic(api_key=key)
    system = [
        {"type": "text", "text": SYSTEM},
        # The data block is stable between questions, so caching it makes each
        # follow-up question cheap. It re-caches whenever new sales are saved.
        {
            "type": "text",
            "text": build_context(rows),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    request = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "messages": [{"role": "user", "content": question}],
    }

    try:
        # Server-side fallback: if a safety classifier ever declines a request,
        # the API answers on a fallback model instead of returning nothing.
        # Harmless here, so it is opt-in insurance rather than a dependency --
        # if the beta is unavailable to this account we retry plainly below.
        response = client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    except anthropic.BadRequestError:
        response = client.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise AssistantError("The server's ANTHROPIC_API_KEY was rejected.") from exc
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


async def _one_call(client, system: list[dict], prompt: str, effort: str, max_tokens: int):
    """A single call, retried without the fallback beta if it isn't available."""
    request = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        return await client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    except Exception as exc:  # noqa: BLE001 -- narrowed by the retry below
        import anthropic

        if isinstance(exc, anthropic.BadRequestError):
            return await client.messages.create(**request)
        raise


async def analyse(rows: list[dict]) -> dict:
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
            "The assistant is not configured yet: ANTHROPIC_API_KEY is not set on the server."
        )
    if not rows:
        raise AssistantError(
            "There are no saved sales to analyse yet. Process a round of PDFs and save first."
        )

    context = build_context(rows)
    data_block = {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}

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
                        [{"type": "text", "text": SYSTEM}, data_block,
                         {"type": "text", "text": ANALYST_SYSTEM}],
                        a["brief"],
                        ANALYST_EFFORT,
                        ANALYST_MAX_TOKENS,
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
                [{"type": "text", "text": SYSTEM}, data_block,
                 {"type": "text", "text": SYNTHESIS_SYSTEM}],
                f"The specialists reported the following.\n\n{briefing}\n\n"
                "Weigh these and give the buying recommendation.",
                EFFORT,
                MAX_TOKENS,
            )
    except anthropic.AuthenticationError as exc:
        raise AssistantError("The server's ANTHROPIC_API_KEY was rejected.") from exc
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
