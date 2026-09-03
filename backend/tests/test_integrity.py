"""Agent-integrity flags: questions with figures attached, never accusations.

Grounded in real June data: the deduction rate's median is 15.0% with 120 of 161
account sales between 14% and 16%, so a line well outside that band is worth
asking about.

The price used to be unanswerable, because every export up to July 2026 left the
Market Avg column at 0.00 in every line. From August 2026 the agent fills it in,
so ``price_concerns`` can finally compare what a carton fetched against what the
market was paying for it. Where the column is still empty, the app must say the
price cannot be checked rather than report a clean result.
"""

from app import integrity


def _row(nett, sold=100, price=100.0, sent=None, **kw):
    row = {"stm_no": 1, "dn": 14847, "product": "PLUMS", "description": "Imp Plums",
           "cartons_sold": sold, "price": price, "nett_total": nett,
           "market_agent": "Farmers Trust", "group_date": "2026-06-01"}
    if sent is not None:
        row["qty_received"] = sent
    row.update(kw)
    return row


def _normal(n=10):
    """A history at the going rate of 15%."""
    return [_row(8500.0, stm_no=i) for i in range(n)]


def test_the_rate_is_what_the_agent_kept():
    assert round(integrity.deduction_rate(_row(8500.0)), 4) == 0.15
    assert integrity.deduction_rate(_row(None)) is None      # not paid yet
    assert integrity.deduction_rate(_row(8500.0, sold=0)) is None


def test_the_going_rate_comes_from_the_business_own_history():
    """Not a number of mine: if the agent's terms change, the baseline moves with
    them and the flags stay meaningful."""
    assert round(integrity.going_rate(_normal()), 4) == 0.15
    # Too few to have a going rate at all.
    assert integrity.going_rate([_row(8500.0)]) is None


def test_a_deduction_well_above_the_going_rate_is_flagged():
    rows = _normal() + [_row(6600.0, stm_no=99)]          # 34% kept
    concerns = integrity.rate_concerns(rows)
    assert [c["stm_no"] for c in concerns] == [99]
    assert concerns[0]["rate"] == 0.34
    assert concerns[0]["baseline"] == 0.15
    assert concerns[0]["kept"] == 3400.0
    assert concerns[0]["severity"] == "watch"


def test_nothing_coming_back_is_severe():
    rows = _normal() + [_row(0.0, stm_no=98)]
    concerns = {c["stm_no"]: c for c in integrity.rate_concerns(rows)}
    assert concerns[98]["severity"] == "severe"
    assert "Nothing came back" in concerns[98]["why"]


def test_keeping_more_than_it_paid_over_is_severe_whatever_the_median():
    rows = _normal() + [_row(2700.0, stm_no=97)]          # 73% kept, like PRE*BT*378061
    concerns = {c["stm_no"]: c for c in integrity.rate_concerns(rows)}
    assert concerns[97]["severity"] == "severe"


def test_the_normal_rate_raises_nothing():
    """120 of 161 real account sales sit in the normal band. Flagging those would
    bury the five that matter."""
    assert integrity.rate_concerns(_normal(20)) == []


def test_severe_lines_come_first_then_the_biggest_money():
    rows = _normal() + [
        _row(660.0, stm_no=1, sold=10),       # 34% kept, but only R340 of it
        _row(6600.0, stm_no=2),               # 34% kept, R3 400 of it
        _row(0.0, stm_no=3, sold=1),          # nothing back, on R100
    ]
    order = [c["stm_no"] for c in integrity.rate_concerns(rows)]
    assert order[0] == 3                      # severe, though it is the smallest
    assert order[1:] == [2, 1]                # then by money kept


def test_mostly_unsold_consignments_are_surfaced():
    """The agent decides what moves off the floor, and produce reported unsold is
    the one thing these reports can never contradict."""
    rows = [_row(4000.0, sold=451, sent=957, consignment_id=900),
            _row(1000.0, sold=0, sent=957, consignment_id=900)]
    out = integrity.unsold_concerns(rows)
    assert len(out) == 1
    assert (out[0]["cartons_sent"], out[0]["cartons_sold"]) == (957, 451)
    assert out[0]["cartons_left"] == 506       # the real crimson grape case
    assert out[0]["runs"] == 2


def test_a_consignment_that_mostly_sold_is_not_a_concern():
    assert integrity.unsold_concerns([_row(8500.0, sold=90, sent=100,
                                           consignment_id=900)]) == []


def test_the_unsold_total_counts_each_delivery_once():
    """A consignment settled over three account sales is three rows repeating the
    same Qty Sent. Summing the column turned 4 487 real cartons into 13 060 and
    reported 76% unsold instead of 30%."""
    rows = [_row(1.0, sold=40, sent=100, consignment_id=900),
            _row(1.0, sold=30, sent=100, consignment_id=900),
            _row(1.0, sold=10, sent=100, consignment_id=900)]
    t = integrity.unsold_total(rows)
    assert t["cartons_sent"] == 100          # not 300
    assert t["cartons_sold"] == 80
    assert t["cartons_left"] == 20
    assert t["share_unsold"] == 0.2


def test_the_unsold_total_includes_leftovers_the_flags_ignore():
    """The headline said "sent but never sold" while summing only consignments
    that sold half or less, understating June by 75 cartons while reading as the
    whole figure."""
    rows = [_row(1.0, sold=90, sent=100, consignment_id=1),   # 10 left, not flagged
            _row(1.0, sold=10, sent=100, consignment_id=2)]   # 90 left, flagged
    assert len(integrity.unsold_concerns(rows)) == 1
    assert integrity.unsold_total(rows)["cartons_left"] == 100
    assert integrity.summary(rows)["unsold_cartons"] == 100


def test_a_products_unsold_share_is_its_own_not_one_bad_consignment():
    """Oranges showed "85% not sold" from a single consignment while the product's
    real figure was 7%, because the chip indexed consignments by product and the
    last one won."""
    rows = [_row(1.0, sold=930, sent=1000, consignment_id=1, product="ORANGES"),
            _row(1.0, sold=3, sent=20, consignment_id=2, product="ORANGES")]
    # The bad consignment is still flagged on its own terms.
    assert len(integrity.unsold_concerns(rows)) == 1
    # But the product is 73 of 1020 unsold, which is not notable.
    assert "ORANGES" not in integrity.by_product(rows)


def test_a_product_genuinely_mostly_unsold_is_flagged():
    rows = [_row(1.0, sold=49, sent=100, consignment_id=1, product="NECTARINES")]
    p = integrity.by_product(rows)["NECTARINES"]
    assert p["share_unsold"] == 0.51
    assert p["consignments"] == 1


def test_a_product_with_nothing_recorded_as_sent_gets_no_verdict():
    """No denominator, so no percentage. Absence of evidence must not render as
    100% unsold, or as 0%."""
    rows = [_row(1.0, sold=10, product="MYSTERY FRUIT")]
    assert integrity.by_product(rows) == {}


def test_price_spread_is_reported_without_a_verdict():
    rows = [_row(1.0, sold=10, price=p, stm_no=i) for i, p in enumerate((50.0, 100.0, 280.0))]
    spread = integrity.price_spread(rows)
    assert spread[0]["low"] == 50.0 and spread[0]["high"] == 280.0
    assert spread[0]["ratio"] == 5.6           # the real 26 June white grape case


def test_a_single_dumped_carton_does_not_become_a_price_spread():
    """One carton written off at R1 is real and belongs in the unsold figure. In
    a price range it reported 107x for strawberries and drowned the comparison."""
    rows = [_row(1.0, sold=10, price=p, stm_no=i) for i, p in enumerate((100.0, 120.0, 140.0))]
    rows.append(_row(1.0, sold=1, price=1.0, stm_no=9))
    spread = integrity.price_spread(rows)
    assert spread[0]["low"] == 100.0
    assert spread[0]["ratio"] == 1.4


def test_the_summary_says_what_it_cannot_check():
    """A panel that only reports what it CAN check reads as a clean bill of health
    on the one thing it is blind to."""
    s = integrity.summary(_normal())
    assert "Market Avg" in s["price_unverifiable"]
    assert s["going_rate"] == 0.15
    assert s["flagged"] == 0


# --- what a carton fetched against what the market was paying --------------

def _priced(realised, avg, cartons=10, **kw):
    """A row that sold `cartons` at `realised` each, against a market average."""
    return _row(None, sold=cartons, price=realised, market_avg=avg, **kw)


def test_a_row_at_the_market_average_is_not_flagged():
    assert integrity.price_concerns([_priced(100.0, 100.0)]) == []


def test_beating_the_market_is_never_a_concern():
    assert integrity.price_concerns([_priced(250.0, 100.0)]) == []


def test_a_row_well_under_the_market_average_is_flagged():
    c = integrity.price_concerns([_priced(50.0, 200.0, cartons=100)])
    assert len(c) == 1
    assert c[0]["ratio"] == 0.25
    assert c[0]["severity"] == "severe"            # under 0.40
    assert c[0]["short_by"] == 15000.0             # 100 cartons x R150 of gap


def test_the_middle_band_is_a_question_not_a_severe_one():
    c = integrity.price_concerns([_priced(60.0, 100.0)])
    assert c[0]["severity"] == "watch"             # 0.60: under 0.70, over 0.40


def test_concerns_are_ranked_by_money_not_by_ratio_or_severity():
    """Three cartons at a tenth of the market average is a curiosity. A hundred
    cartons at two thirds of it is real money. Ranking on the ratio, or on
    severity first, buries the money under the curiosity."""
    curiosity = _priced(10.0, 100.0, cartons=3, stm_no=1)    # 0.10 severe, R270
    money = _priced(65.0, 100.0, cartons=100, stm_no=2)      # 0.65 watch, R3 500
    got = integrity.price_concerns([curiosity, money])
    assert [c["stm_no"] for c in got] == [2, 1]
    assert [c["severity"] for c in got] == ["watch", "severe"]


def test_a_missing_market_average_is_not_a_price_of_zero():
    """Every export up to July 2026 left the column at 0.00. Read as a real
    average, each of those rows looks infinitely below the market."""
    assert integrity.price_concerns([_priced(100.0, None)]) == []
    assert integrity.price_concerns([_priced(100.0, 0.0)]) == []


def test_price_position_reports_its_own_coverage():
    rows = [_priced(100.0, 100.0), _priced(50.0, 200.0), _priced(100.0, None)]
    p = integrity.price_position(rows)
    assert p["known"] is True
    assert (p["covered"], p["of"]) == (2, 3)
    assert p["realised"] == 1500.0                 # 10x100 + 10x50
    assert p["at_market_average"] == 3000.0        # 10x100 + 10x200
    assert p["short_by"] == 1500.0
    assert p["share_of_market"] == 0.5


def test_a_period_with_no_market_average_says_so_rather_than_reporting_clean():
    rows = [_priced(100.0, None), _priced(80.0, None)]
    assert integrity.price_position(rows)["known"] is False
    assert integrity.summary(rows)["price_unverifiable"]


def test_the_caveat_is_dropped_once_the_price_can_be_checked():
    assert integrity.summary([_priced(100.0, 100.0)])["price_unverifiable"] is None
