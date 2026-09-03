"""Zaco's DN, which no export carries.

Measured against the operator's own book over June: column A agreed on 26 of 43
statements and differed on 17, every one of them a case where the market's
Supplier Ref held a producer code, a placeholder, or another number entirely.
"""

from app import delivery
from app.schemas import StatementRow


def _row(dn=None, ref=None, producer=20026, delivery_id=1180698, stm=1):
    return StatementRow(source_file="june.csv", dn=dn if dn is not None else ref,
                        supplier_ref=ref, producer_code=producer,
                        delivery_id=delivery_id, stm_no=stm)


def test_a_captured_dn_is_used_for_every_row_of_its_delivery():
    rows = [_row(ref=14013, delivery_id=1180698, stm=1),
            _row(ref=14013, delivery_id=1180698, stm=2),
            _row(ref=14828, delivery_id=1180697, stm=3)]
    assert delivery.apply_notes(rows, {1180698: 14815}) == 2
    assert [r.dn for r in rows] == [14815, 14815, 14828]


def test_a_number_used_as_a_producer_code_is_not_a_delivery_note():
    """The real evidence: June carries both 14013*14798 and 20026*14013, so 14013
    is a producer and the ref that equals it is not a delivery."""
    rows = [_row(ref=14798, producer=14013, delivery_id=1176362),
            _row(ref=14013, producer=20026, delivery_id=1180698)]
    assert delivery.producer_codes(rows) == {14013, 20026}
    assert delivery.flag_unproven(rows, {}) == 1
    assert rows[0].flags == []                       # 14798 is a real DN
    assert rows[1].flags[0].code == "dn_unproven"


def test_a_ref_on_two_deliveries_is_not_evidence_of_anything():
    """One Zaco DN genuinely covers several market deliveries -- 14841 covers both
    1178361 and 1180695. Treating that as suspicious called 35 correct rows wrong
    out of the 57 it flagged, which is why it is not used."""
    rows = [_row(ref=14841, delivery_id=1178361), _row(ref=14841, delivery_id=1180695)]
    assert delivery.flag_unproven(rows, {}) == 0


def test_the_producer_code_repeated_is_a_placeholder():
    """20026*20026 is the producer code echoed back, which the book replaced with
    a DN of its own."""
    rows = [_row(ref=20026, producer=20026, delivery_id=1180701)]
    assert delivery.flag_unproven(rows, {}) == 1
    assert rows[0].flags[0].code == "dn_unproven"
    assert rows[0].flags[0].severity == "warning"     # never blocks the save


def test_a_dn_nowhere_near_the_ones_on_file_is_flagged():
    """30559 is the one bad case with no other tell: it is not a producer code
    anywhere. Once enough real DNs are on file, being far outside their range is
    itself the signal."""
    notes = {1180000 + i: 14800 + i for i in range(12)}
    rows = [_row(ref=30559, delivery_id=1178361)]
    assert delivery.flag_unproven(rows, notes) == 1


def test_the_range_test_stays_quiet_until_there_is_history_to_judge_by():
    """On a first import there is nothing to compare against, and inventing a
    plausible range would flag every row on the operator's first day."""
    rows = [_row(ref=30559, delivery_id=1178361)]
    assert delivery.flag_unproven(rows, {1180698: 14815}) == 0


def test_a_ref_that_looks_like_a_delivery_note_is_left_alone():
    """It is right far more often than not, and a warning on every row is a
    warning nobody reads by the second week."""
    rows = [_row(ref=14828, producer=20026, delivery_id=1180697)]
    assert delivery.flag_unproven(rows, {}) == 0
    assert rows[0].flags == []


def test_nothing_is_flagged_once_the_delivery_is_on_file():
    rows = [_row(ref=20026, producer=20026, delivery_id=1180701)]
    assert delivery.flag_unproven(rows, {1180701: 14776}) == 0


def test_a_missing_ref_is_flagged():
    rows = [_row(ref=None, delivery_id=1180701)]
    assert delivery.flag_unproven(rows, {}) == 1


def test_the_mapping_is_recovered_from_the_workbook_by_account_sale():
    """The DN cannot be derived from the market's data, but it does not have to
    be: the workbook has DN plus account sale, the export has Delivery ID plus
    account sale, so the account sale bridges them. On the real Golden book
    against the June export this recovered 15 of 15 deliveries with no
    conflicts, answering every row and correcting the 30 that were wrong."""
    rows = [_row(ref=14013, delivery_id=1180698, stm=383735),
            _row(ref=14013, delivery_id=1180698, stm=384100),
            _row(ref=20026, delivery_id=1180701, stm=384423)]
    book = {383735: 14815, 384100: 14815, 384423: 14776}
    assert delivery.seed_from_history(rows, book) == {1180698: 14815, 1180701: 14776}


def test_an_account_sale_spanning_two_deliveries_is_refused():
    """It settles both, so it cannot say which one the DN belongs to. Splitting
    it would attach a real supplier's money to the wrong delivery."""
    rows = [_row(ref=14013, delivery_id=1180698, stm=385673),
            _row(ref=14013, delivery_id=1180710, stm=385673)]
    assert delivery.seed_from_history(rows, {385673: 14815}) == {}


def test_a_delivery_whose_statements_disagree_is_refused():
    """One of the two sides is wrong. A coin toss would bury which."""
    rows = [_row(ref=14013, delivery_id=1180698, stm=1),
            _row(ref=14013, delivery_id=1180698, stm=2)]
    assert delivery.seed_from_history(rows, {1: 14815, 2: 14841}) == {}


def test_a_statement_the_workbook_has_never_seen_is_left_for_the_operator():
    rows = [_row(ref=20026, delivery_id=1180701, stm=999)]
    assert delivery.seed_from_history(rows, {383735: 14815}) == {}


def test_only_corrections_are_learned():
    """A round that changed nothing must write nothing, or every save rewrites
    the whole mapping with what it was already told."""
    rows = [_row(dn=14815, delivery_id=1180698), _row(dn=14828, delivery_id=1180697)]
    assert delivery.learned(rows, {1180698: 14815}) == {1180697: 14828}
    assert delivery.learned(rows, {1180698: 14815, 1180697: 14828}) == {}


def test_a_corrected_dn_is_learned_over_the_old_one():
    rows = [_row(dn=14776, delivery_id=1180701)]
    assert delivery.learned(rows, {1180701: 20026}) == {1180701: 14776}
