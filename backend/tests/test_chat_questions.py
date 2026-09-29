"""The questions the operator actually asks, and whether the chat can answer them.

The chat answers from one block of text built by ``assistant.data_block``. A
question it cannot answer is almost never the model's fault: it is a figure the
screens show that was never put in that block. That happened twice in one week
-- "what is still owed for September" and "what is on hand for September" --
and both times the chat was honest about having nothing to go on.

So every question below names the figure the answer needs, works that figure
out with the same call the screen makes, and checks it is in the block. When
someone adds a screen, the question it answers belongs in here.
"""

from datetime import date

import pytest

from app import analytics, assistant, procurement, tracking

TODAY = date(2026, 9, 29)


def row(cid, dn, product, market, agent, sold, value, day, arrived, qty):
    return {"consignment_id": cid, "dn": dn, "product": product, "market": market,
            "market_agent": agent, "cartons_sold": sold, "price": value / sold,
            "sales_total": value, "last_sale": day, "date_received": arrived,
            "group_date": arrived, "qty_received": qty, "qty_amended": qty}


SUGRAONE = "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"
CRIMSON = "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"
PLUMS = "PLUMS ELDORADO CLASS 1 NO SIZE (DOMPEL JUMBLE 5kg)"

# A small book shaped like the real one: a Durban load that sold across the
# turn of the month and is part paid, a Tshwane load not paid at all, and a
# Tshwane load paid to the cent. July gives the projection a history.
BOOK = [
    row(185549101, 1855491, SUGRAONE, "DURBAN MARKET", "Grow Port Natal",
        60, 24000.0, "2026-07-20", "2026-07-18", 60),
    row(185549102, 1855492, SUGRAONE, "DURBAN MARKET", "Grow Port Natal",
        120, 48000.0, "2026-08-28", "2026-08-27", 200),
    row(185549102, 1855492, SUGRAONE, "DURBAN MARKET", "Grow Port Natal",
        50, 20000.0, "2026-09-02", "2026-08-27", 200),
    row(1462001, 14620, CRIMSON, "TSHWANE MARKET", "Farmers Trust",
        60, 12000.0, "2026-09-17", "2026-09-10", 100),
    row(1463001, 14630, PLUMS, "TSHWANE MARKET", "Farmers Trust",
        50, 5000.0, "2026-09-17", "2026-09-12", 50),
    row(1461001, 14610, CRIMSON, "TSHWANE MARKET", "Farmers Trust",
        80, 16000.0, "2026-07-22", "2026-07-20", 80),
]
PAYS = [
    {"accsale": "DUR*13*1", "dn": 1855491, "date": "2026-07-30", "gross": 24000.0,
     "nett": 20400.0, "lines": [{"product": SUGRAONE, "sales_total": 24000.0, "sold": 60}]},
    {"accsale": "DUR*13*2", "dn": 1855492, "date": "2026-09-05", "gross": 30000.0,
     "nett": 25500.0, "lines": [{"product": SUGRAONE, "sales_total": 30000.0, "sold": 75}]},
    {"accsale": "PRE*BT*1", "dn": 14630, "date": "2026-09-24", "gross": 5000.0,
     "nett": 4250.0, "lines": [{"product": PLUMS, "sales_total": 5000.0, "sold": 50}]},
    {"accsale": "PRE*BT*2", "dn": 14610, "date": "2026-07-30", "gross": 16000.0,
     "nett": 13600.0, "lines": [{"product": CRIMSON, "sales_total": 16000.0, "sold": 80}]},
]


@pytest.fixture(scope="module")
def block() -> str:
    return assistant.data_block(BOOK, PAYS)


def section(block: str, title: str) -> str:
    """One section of the block, so a figure is found where the chat is told
    to look for it, not anywhere at all."""
    start = block.index(title)
    nxt = block.find("\n## ", start + 1)
    return block[start:] if nxt < 0 else block[start:nxt]


def month_owed(month: str) -> float:
    lo, hi = analytics.period_bounds(month=month)
    return tracking.payment_status(BOOK, PAYS, frozenset(), lo, hi)["still_to_come"]


# --- money --------------------------------------------------------------------

def test_what_is_still_owed_for_september(block):
    owed = month_owed("2026-09")
    table = section(block, "## Payment and what is still owed")
    assert "2026-09 | 37 000,00 |" in table      # R 20 000 + R 12 000 + R 5 000 sold
    assert assistant._rand(owed) in table or assistant._fmt(owed) in table


def test_what_does_durban_still_owe(block):
    status = tracking.payment_status(BOOK, PAYS)
    durban = next(m for m in status["outstanding_markets"] if m["market"] == "DURBAN MARKET")
    assert (f"DURBAN MARKET | Grow Port Natal | {assistant._fmt(durban['owed'])}"
            in section(block, "## Payment and what is still owed"))


def test_which_delivery_notes_still_need_paying(block):
    money = section(block, "## Payment and what is still owed")
    assert "| 14620 |" in money          # nothing paid on it
    assert "| 1855492 |" in money        # part paid
    assert "| 14630 |" not in money.split("### Every outstanding line")[1]  # paid to the cent


def test_what_to_chase_and_what_only_to_check(block):
    money = section(block, "## Payment and what is still owed")
    assert "Not paid yet" in money and "Cartons unpaid" in money
    assert "is money genuinely owed" in money


def test_how_long_each_agent_takes_to_pay(block):
    assert "How long each agent takes to pay" in block


# --- stock on hand ------------------------------------------------------------

def test_what_is_on_hand(block):
    """The question that was answered 'there is no stock on hand'."""
    soh = tracking.stock_on_hand(BOOK, date.today())
    stock = section(block, "## Stock on hand")
    assert f"Total: {assistant._count(soh['cartons_left'])} cartons" in stock
    assert "TSHWANE MARKET | Farmers Trust | 40 |" in stock      # 100 sent, 60 sold
    assert "DURBAN MARKET | Grow Port Natal | 30 |" in stock     # 200 sent, 170 sold


def test_what_is_on_hand_for_september(block):
    """September's stock is what September put on the floor, as the tab
    scopes it: the Crimson that arrived on the 10th."""
    stock = section(block, "## Stock on hand")
    assert "2026-09 | 40 | 1 | TSHWANE MARKET 40" in stock
    assert "2026-08 | 30 | 1 | DURBAN MARKET 30" in stock


def test_which_stock_has_been_sitting_longest(block):
    stock = section(block, "## Stock on hand")
    assert "| 14620 | " + CRIMSON + " | 2026-09-10 |" in stock
    assert "red (14 days or more)" in stock or "orange (7 to 13 days)" in stock


# --- days ------------------------------------------------------------------------

def test_what_sold_on_the_17th(block):
    days = section(block, "## Sales per day")
    line = next(l for l in days.split("\n") if l.startswith("2026-09-17 |"))
    assert line.startswith("2026-09-17 | 17 000,00 | 110 |")
    assert "TSHWANE MARKET R 17 000,00" in line


def test_how_much_of_a_day_has_been_paid(block):
    """The 17th sold R 17 000,00: the plums (R 5 000,00) are paid, the
    Crimson (R 12 000,00) is not."""
    line = next(l for l in section(block, "## Sales per day").split("\n")
                if l.startswith("2026-09-17 |"))
    assert "| 12 000,00 |" in line


# --- markets and products --------------------------------------------------------

def test_what_sells_at_each_market_and_through_whom(block):
    markets = section(block, "## What sells at each market")
    assert "### DURBAN MARKET" in markets and "### TSHWANE MARKET" in markets
    assert f"- {SUGRAONE}: R 92 000,00" in markets
    assert "via Grow Port Natal" in markets and "via Farmers Trust" in markets


def test_what_sold_best(block):
    assert "## Products by sales value (exact)" in block
    assert SUGRAONE in section(block, "## Products by sales value")


def test_month_totals(block):
    assert "## Month by month (exact)" in block
    assert "2026-09 | 37 000,00" in section(block, "## Month by month (exact)")


# --- buying --------------------------------------------------------------------

def test_what_to_order_this_week_and_this_month(block):
    plan = procurement.build(BOOK, PAYS, today=date.today(), days=7)
    buy = section(block, "## The buy plan")
    assert "take on, 7 days | take on, month" in buy
    assert f"take on {assistant._count(plan['totals']['cartons'])} cartons" in buy


def test_where_to_send_each_product(block):
    buy = section(block, "## The buy plan")
    assert "DURBAN MARKET via Grow Port Natal" in buy


def test_what_next_month_looks_like(block):
    assert "## Projection for" in block


# --- the guard that stops this happening again -----------------------------

def test_every_section_the_chat_is_sent_to_exists():
    """The prompt tells the chat where each kind of answer lives. A section
    named there and missing from the block is how a real question gets
    'there is nothing on that' for an answer."""
    text = assistant.data_block(BOOK, PAYS)
    for title in ("Payment and what is still owed", "Month by month",
                  "Stock on hand, as of today", "Still on hand, by the month it arrived",
                  "Sales per day", "What sells at each market", "The buy plan",
                  "Projection", "Where to send it"):
        assert title in assistant.SYSTEM, title
        assert title in text, f"the prompt sends the chat to '{title}', which is not there"


def test_the_chat_says_where_it_looked_before_saying_there_is_nothing():
    assert "then say which section you checked" in assistant.SYSTEM


def test_the_block_stays_small_enough_to_answer_quickly():
    """The real book builds a block of about 44 000 tokens. Haiku reads two
    hundred thousand; this keeps a wide margin for a book several times the
    size before anything has to be cut."""
    assert len(assistant.data_block(BOOK, PAYS)) < 60_000
