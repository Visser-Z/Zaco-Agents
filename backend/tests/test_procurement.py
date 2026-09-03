"""What to take on: levels, scoring, and how attention is apportioned.

Zaco takes produce on consignment and earns a commission percentage of the Nett
that comes back; it never buys stock. So there is no budget here, and these
tests guard against the cash model creeping back in.

The levels are computed, not written by a model, so they are fully testable —
and they need to be, because real money is settled off the back of them.
"""

from datetime import date

from app import procurement

TODAY = date(2026, 7, 1)


def _row(product, sold, price, sent, first="2026-06-01", last="2026-06-02", **kw):
    base = {
        # The raw product name is what the list is keyed on, not the short code.
        "product": product,
        "market": "TSHWANE MARKET",
        "market_agent": "Farmers Trust",
        "cartons_sold": sold,
        "price": price,
        "qty_received": sent,
        "date_received": first,
        "last_sale": last,
        "group_date": first,
    }
    base.update(kw)
    return base


def test_the_list_is_keyed_on_the_same_label_insights_ranks_on():
    """Insights, the buy list and the review screen's buy chip all key on one
    label. They only agree while they derive it the same way, and they stopped
    agreeing the moment a product had no short code yet -- so this pins the
    three together rather than trusting each to pick the same field."""
    from app import analytics

    rows = [
        _row("CHERRIES OTHER CLASS 1 LARGE STANDARD TRAY 4.5kg", 100, 200.0, 100,
             description="Imp Cherries"),
        # Same fruit, no code assigned yet. Keyed on the code these were two
        # products; keyed on the name they are one.
        _row("CHERRIES OTHER CLASS 1 LARGE STANDARD TRAY 4.5kg", 50, 200.0, 50,
             description=None),
        _row("PLUMS OTHER CLASS 1 MEDIUM STANDARD TRAY 5kg", 10, 20.0, 100,
             description="Imp Plums"),
    ]
    listed = [r["product"] for r in procurement.recommend(rows, today=TODAY)["recommendations"]]
    ranked = [b["label"] for b in analytics.best_sellers(rows)]

    assert sorted(listed) == sorted(ranked)
    assert "Imp Cherries" not in listed
    assert listed.count("CHERRIES OTHER CLASS 1 LARGE STANDARD TRAY 4.5kg") == 1


def test_no_history_is_answerable():
    out = procurement.recommend([], today=TODAY)
    assert out["recommendations"] == []
    assert out["earnings"]["commission"] == 0.0
    assert out["history"]["months"] == 0


def test_bands_never_disagree_with_the_ranking():
    """A higher-scoring line sitting in a lower band reads as a bug, whatever
    the reason. Confidence is applied to the score, not as a band override."""
    rows = []
    for i in range(20):
        rows += [_row(f"P{i}", sold=10 + i, price=10 + i * 5, sent=10 + i) for _ in range(2)]
    recs = procurement.recommend(rows, today=TODAY)["recommendations"]
    order = {"critical": 0, "high": 1, "low": 2}
    seq = [(order[r["level"]], -r["score"]) for r in recs]
    assert seq == sorted(seq)


def test_a_priority_list_stays_short_at_the_top():
    rows = []
    for i in range(40):
        rows += [_row(f"P{i}", sold=5 + i, price=20 + i, sent=5 + i) for _ in range(2)]
    recs = procurement.recommend(rows, today=TODAY)["recommendations"]
    crit = [r for r in recs if r["level"] == "critical"]
    # Roughly the top sixth, never "most of the catalogue".
    assert 1 <= len(crit) <= len(recs) * 0.25


def test_clears_fast_and_sells_out_beats_slow_and_leftover():
    rows = [
        _row("FAST", sold=100, price=200, sent=100, first="2026-06-01", last="2026-06-01"),
        _row("FAST", sold=100, price=200, sent=100, first="2026-06-10", last="2026-06-10"),
        _row("SLOW", sold=40, price=200, sent=100, first="2026-06-01", last="2026-06-20"),
        _row("SLOW", sold=40, price=200, sent=100, first="2026-06-01", last="2026-06-20"),
    ]
    by = {r["product"]: r for r in procurement.recommend(rows, today=TODAY)["recommendations"]}
    assert by["FAST"]["score"] > by["SLOW"]["score"]
    assert by["FAST"]["sell_through"] == 1.0
    assert by["FAST"]["avg_days_to_sell"] == 0


def test_thin_evidence_is_discounted_not_trusted():
    """One consignment is a data point, not a pattern. The same figures with
    more consignments behind them must score higher."""
    once = [_row("ONE", sold=100, price=200, sent=100)]
    twice = [_row("TWO", sold=100, price=200, sent=100) for _ in range(3)]
    a = procurement.recommend(once, today=TODAY)["recommendations"][0]
    b = procurement.recommend(twice, today=TODAY)["recommendations"][0]
    assert a["thin_evidence"] is True and b["thin_evidence"] is False
    assert b["score"] > a["score"]
    assert any("provisional" in r for r in a["reasons"])


def test_stale_lines_lose_ground():
    """Fruit is seasonal: something that stopped selling months ago is not a
    buy, however well it did at the time."""
    fresh = [_row("FRESH", 100, 200, 100, first="2026-06-25", last="2026-06-26") for _ in range(2)]
    stale = [_row("STALE", 100, 200, 100, first="2026-04-01", last="2026-04-02") for _ in range(2)]
    out = procurement.recommend(fresh + stale, today=TODAY)
    by = {r["product"]: r for r in out["recommendations"]}
    assert by["FRESH"]["score"] > by["STALE"]["score"]
    assert any("season may be over" in r for r in by["STALE"]["reasons"])


def test_share_goes_only_to_lines_worth_backing():
    """There is no budget to divide -- nothing is paid to acquire stock. What
    is apportioned is attention: market slots, handling and supplier calls.
    Directing any of it at a Low line is what this list exists to prevent."""
    rows = []
    for i in range(20):
        rows.append(_row(f"P{i}", sold=10 + i, price=50 + i * 9, sent=10 + i))
    recs = procurement.recommend(rows, today=TODAY)["recommendations"]
    for r in recs:
        if r["level"] == procurement.LOW:
            assert r["share"] == 0.0
        else:
            assert r["share"] > 0.0
    assert round(sum(r["share"] for r in recs), 4) == 1.0


def test_nothing_claims_a_budget_or_a_spend():
    """The old payload handed out `suggested_spend` from a cash `buying_power`.
    Both modelled a purchase that never happens; if either comes back, the UI
    starts asking for money that is never laid out."""
    out = procurement.recommend([_row("A", 10, 100, 10)], today=TODAY)
    assert "buying_power" not in out
    assert "suggested_spend" not in out["recommendations"][0]


def test_best_market_only_shown_when_there_is_a_choice():
    one = [_row("A", 10, 100, 10)]
    assert procurement.recommend(one, today=TODAY)["recommendations"][0]["best_market"] is None

    two = one + [_row("A", 10, 300, 10, market="JOBURG MKT")]
    rec = procurement.recommend(two, today=TODAY)["recommendations"][0]
    assert rec["best_market"]["name"] == "JOBURG MKT"
    assert rec["best_market"]["avg_price"] == 300.0


def test_destination_is_reported_even_with_only_one_place_to_send_it():
    """On consignment the destination is part of the buy decision: it says
    which load the produce goes on. Every product in this business's real
    history has exactly ONE destination, so reporting only a 'best market'
    comparison showed nothing at all on every line, forever."""
    rec = procurement.recommend([_row("A", 10, 100, 10)], today=TODAY)["recommendations"][0]
    d = rec["destination"]
    assert d["market"] == "TSHWANE MARKET"
    assert d["avg_price"] == 100.0
    # There was no choice, so it must not imply one was made.
    assert d["compared"] is False and d["options"] == 1


def test_destination_flags_a_real_comparison():
    rows = [_row("A", 10, 100, 10), _row("A", 10, 300, 10, market="JOBURG MKT",
                                         market_agent="Subtropico")]
    d = procurement.recommend(rows, today=TODAY)["recommendations"][0]["destination"]
    assert d["compared"] is True and d["options"] == 2
    assert d["agent"] == "Subtropico"      # the one that fetched more


# --- once consignment terms exist, it ranks on real commission ------------

def _deal(pct=30.0, ref=14847, product="A", settled=False):
    from app import consignment
    return {consignment.deal_key(ref, product): {
        "commission_pct": pct, "supplier_id": 1, "supplier_name": "Botha Farms",
        "settled_at": "2026-08-01T00:00:00Z" if settled else None,
        "settled_amount": None}}


def _termed(product, sold, price, sent, nett, ref=14847, **kw):
    row = _row(product, sold, price, sent, **kw)
    # Deals are keyed on the RAW product string (as the market reports it),
    # while the ranking groups by label (the short code where one exists), so a
    # realistic row carries both.
    row["product"] = product
    row["supplier_ref"] = ref
    row["nett_total"] = nett
    return row


def test_commission_is_none_rather_than_guessed_without_terms():
    """Assuming the default rate would manufacture a debt to a real person out
    of a missing form field."""
    rec = procurement.recommend([_termed("A", 10, 100, 10, 850.0)],
                                today=TODAY)["recommendations"][0]
    assert rec["earning"] is None
    assert rec["ranked_on_commission"] is False


def test_ranks_on_commission_once_terms_are_known():
    rows = [_termed("A", 10, 100, 10, 850.0)]
    rec = procurement.recommend(rows, today=TODAY, deals=_deal())["recommendations"][0]
    assert rec["ranked_on_commission"] is True
    # 30% of the R850 that landed, over 10 cartons sold.
    assert rec["earning"]["commission"] == 255.0
    assert rec["earning"]["per_carton"] == 25.5
    assert "commission" in rec["signals"]


def test_untermed_lines_still_rank_but_are_marked():
    rows = [_termed("A", 10, 100, 10, 850.0),
            _termed("B", 10, 100, 10, 850.0, ref=99999)]
    out = procurement.recommend(rows, today=TODAY, deals=_deal())
    assert out["termed"] == {"products": 1, "of": 2, "ranked_on_commission": True}
    assert {r["product"] for r in out["recommendations"]} == {"A", "B"}


def test_a_higher_rate_outranks_a_lower_one_all_else_equal():
    """The whole point of recording terms: two lines that sell identically are
    not equally worth taking on if one pays Zaco more."""
    rows = [_termed("A", 10, 100, 10, 850.0, ref=1),
            _termed("B", 10, 100, 10, 850.0, ref=2)]
    from app import consignment
    deals = {
        consignment.deal_key(1, "A"): {"commission_pct": 40.0, "supplier_id": 1,
                                       "supplier_name": "X", "settled_at": None,
                                       "settled_amount": None},
        consignment.deal_key(2, "B"): {"commission_pct": 10.0, "supplier_id": 2,
                                       "supplier_name": "Y", "settled_at": None,
                                       "settled_amount": None},
    }
    recs = procurement.recommend(rows, today=TODAY, deals=deals)["recommendations"]
    assert recs[0]["product"] == "A"
    assert recs[0]["score"] > recs[1]["score"]


def test_basis_switches_once_any_terms_are_known():
    rows = [_termed("A", 10, 100, 10, 850.0)]
    assert "cannot rank by what you earn" in procurement.recommend(rows, today=TODAY)["basis"].lower()
    assert "commission you actually earned" in procurement.recommend(
        rows, today=TODAY, deals=_deal())["basis"]


def test_basis_states_it_cannot_rank_by_earnings_without_terms():
    """The honesty guard. Nothing records the agreed rate, so presenting this
    as earnings advice would be wrong -- if this text is ever dropped, the UI
    starts implying something the data cannot support."""
    basis = procurement.recommend([], today=TODAY)["basis"].lower()
    assert "cannot rank by what you earn" in basis
