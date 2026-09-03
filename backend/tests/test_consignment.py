"""Consignment settlement tests.

Zaco takes produce on consignment and pays the supplier only once it sells,
keeping a commission percentage of the Nett that comes back. These tests guard
the arithmetic that decides what a real person is owed, so they are deliberately
picky about the cases where the system must REFUSE to compute rather than guess.
"""

from app import consignment

REF = 14847


def _row(product="PLUMS", sent=100, sold=80, price=100.0, nett=8000.0, ref=REF, **kw):
    row = {
        "supplier_ref": ref,
        "product": product,
        "description": product,
        "qty_received": sent,
        "cartons_sold": sold,
        "price": price,
        "nett_total": nett,
        "market": "TSHWANE MARKET",
        "market_agent": "Farmers Trust",
        "date_received": "2026-07-01",
        "last_sale": "2026-07-03",
        "group_date": "2026-07-01",
    }
    row.update(kw)
    return row


def _deal(pct=30.0, product="PLUMS", ref=REF, name="Botha Farms",
          settled_at=None, settled_amount=None):
    return {consignment.deal_key(ref, product): {
        "commission_pct": pct, "supplier_id": 1, "supplier_name": name,
        "settled_at": settled_at, "settled_amount": settled_amount}}


# --- the split ------------------------------------------------------------

def test_the_worked_example():
    """R1 000 sells, the market agent deducts R150, R850 lands with Zaco. At
    30% Zaco keeps R255 and the supplier is owed R595."""
    line = consignment.settle_row(_row(nett=850.0), _deal(30.0)[consignment.deal_key(REF, "PLUMS")])
    assert line["nett"] == 850.0
    assert line["commission"] == 255.0
    assert line["owed_to_supplier"] == 595.0


def test_commission_comes_off_the_nett_not_the_gross():
    """Gross is the market's sale value, not Zaco's money. Taking the cut from
    gross would pay away the market agent's deductions twice."""
    line = consignment.settle_row(
        _row(nett=850.0, price=100.0, sold=10),      # gross would be R1 000
        _deal(30.0)[consignment.deal_key(REF, "PLUMS")])
    assert line["commission"] == 255.0               # 30% of 850, not of 1 000


def test_unsold_cartons_are_the_suppliers_loss_not_zacos():
    """On consignment Zaco pays nothing for stock, so cartons that never move
    cost it nothing. In a buy-and-resell business those 20 would be a loss."""
    key = consignment.deal_key(REF, "PLUMS")
    full = consignment.settle_row(_row(sent=100, sold=100), _deal()[key])
    part = consignment.settle_row(_row(sent=100, sold=80), _deal()[key])
    assert full["commission"] == part["commission"] == 2400.0


# --- when it must refuse to compute ---------------------------------------

def test_no_terms_means_no_settlement_rather_than_a_default_rate():
    """Applying the default 30% here would invent a debt to a real person out
    of a missing form field."""
    assert consignment.settle_row(_row(), None) is None
    out = consignment.settle([_row()], {})
    assert out["lines"] == []
    assert len(out["awaiting_terms"]) == 1
    assert out["commission_earned"] == 0.0
    # Its money is excluded from the totals and reported separately, not hidden.
    assert out["unattributed_nett"] == 8000.0


def test_a_recorded_deal_with_no_rate_still_does_not_settle():
    deal = {"commission_pct": None, "supplier_id": 1, "supplier_name": "Botha Farms",
            "settled_at": None, "settled_amount": None}
    assert consignment.settle_row(_row(), deal) is None


def test_unpaid_consignments_are_separated_from_untermed_ones():
    """Two different problems needing two different actions: chase the market,
    or agree the terms."""
    out = consignment.settle([_row(nett=None), _row(product="PEARS")], _deal())
    assert len(out["awaiting_payment"]) == 1
    assert len(out["awaiting_terms"]) == 1


# --- positions ------------------------------------------------------------

def test_owed_and_paid_are_kept_apart():
    rows = [_row(product="PLUMS"), _row(product="PEARS")]
    deals = {**_deal(product="PLUMS"),
             **_deal(product="PEARS", settled_at="2026-08-01T00:00:00Z")}
    out = consignment.settle(rows, deals)
    assert out["commission_earned"] == 4800.0        # earned on both
    assert out["owed_to_suppliers"] == 5600.0        # only the unsettled one
    assert out["paid_to_suppliers"] == 5600.0


def test_what_was_actually_paid_wins_over_the_computed_figure():
    """A rounding or an advance means the payment can differ from the sum. The
    computed figure is never overwritten, so the difference stays visible."""
    deals = _deal(settled_at="2026-08-01T00:00:00Z", settled_amount=5000.0)
    out = consignment.settle([_row()], deals)
    assert out["paid_to_suppliers"] == 5000.0
    assert out["lines"][0]["owed_to_supplier"] == 5600.0


def test_supplier_position_groups_and_ranks_by_what_is_owed():
    rows = [_row(product="PLUMS"), _row(product="PEARS", nett=1000.0)]
    deals = {**_deal(product="PLUMS", name="Botha Farms"),
             **_deal(product="PEARS", name="Van Wyk Boerdery")}
    positions = consignment.by_supplier(consignment.settle(rows, deals))
    assert [p["supplier"] for p in positions] == ["Botha Farms", "Van Wyk Boerdery"]
    assert positions[0]["owed"] == 5600.0
    assert positions[0]["unsold"] == 20          # 100 handed over, 80 sold


def test_outstanding_stock_is_reported_without_terms():
    """Produce sitting unsold is at risk right now, whether or not anyone has
    got round to agreeing the commission on it."""
    out = consignment.outstanding_stock([_row(sent=100, sold=80)], {})
    assert len(out) == 1
    assert out[0]["cartons_left"] == 20
    assert out[0]["share_unsold"] == 0.2


def test_fully_sold_consignments_are_not_outstanding():
    assert consignment.outstanding_stock([_row(sent=100, sold=100)], {}) == []


# --- one delivery, several account sales ----------------------------------
# Rows are account sales now, and each carries the delivery's full Qty Sent.
# Anything measuring what was HANDED OVER has to count the delivery once.

def test_stock_left_counts_everything_sold_off_the_delivery():
    """Both runs came off one delivery of 100. Measuring a single row would
    report 40 cartons still on the floor when only 10 are -- stock that a later
    run had already cleared."""
    rows = [_row(sent=100, sold=60, consignment_id=900, nett=6000.0),
            _row(sent=100, sold=30, consignment_id=900, nett=3000.0)]
    out = consignment.outstanding_stock(rows, {})
    assert len(out) == 1
    assert (out[0]["cartons_sent"], out[0]["cartons_sold"], out[0]["cartons_left"]) == (100, 90, 10)


def test_a_delivery_settled_twice_is_not_double_counted_against_the_supplier():
    """Commission and Nett are per account sale and add up. What the supplier
    handed over does not: counted per row it would say they brought 200 cartons
    and that 110 never sold, when they brought 100 and 10 did not."""
    rows = [_row(sent=100, sold=60, consignment_id=900, nett=6000.0),
            _row(sent=100, sold=30, consignment_id=900, nett=3000.0)]
    position = consignment.by_supplier(consignment.settle(rows, _deal(30.0)))[0]
    assert position["nett"] == 9000.0
    assert position["commission"] == 2700.0
    assert position["cartons_sold"] == 90
    assert position["cartons_sent"] == 100       # the delivery, counted once
    assert position["consignments"] == 1
    assert position["unsold"] == 10


def test_expected_commission_per_carton():
    out = consignment.expected_commission([_row()], _deal(), "PLUMS")
    assert out["commission"] == 2400.0
    assert out["per_carton"] == 30.0             # R2 400 over the 80 that sold
    assert out["consignments_with_terms"] == 1
