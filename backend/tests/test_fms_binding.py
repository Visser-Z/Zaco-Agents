"""Tying a payment to its delivery by FMS id, and each line to its consignment.

Supplier Ref cannot carry the join: in this book 20 refs cover more than one
delivery, and some are a date or the letter N. The FMS id is one per delivery
and stable across its account sales; consignment = delivery * 100 + line.
Cases are the ones the September 2026 exports turned up.
"""

from app import reconcile, tracking


def _sale(cid, product, value, day, market, qty_amended=None, dn=None):
    return {"consignment_id": cid, "dn": dn if dn is not None else cid // 100,
            "product": product, "market": market, "market_agent": "x",
            "sales_total": value, "cartons_sold": 1, "price": value,
            "date_received": day, "last_sale": day, "group_date": day,
            "qty_amended": qty_amended}


def _pay(accsale, fms, date, lines, dn=None, nett=None):
    gross = round(sum(l[3] for l in lines), 2)
    return {"accsale": accsale, "fms_id": fms, "dn": dn, "date": date, "gross": gross,
            "nett": nett if nett is not None else round(gross * 0.85, 2),
            "lines": [{"line_no": n, "product": p, "delivered": d, "sold": 1, "sales_total": v}
                      for n, p, d, v in lines]}


DBN = "DURBAN MARKET"
PRE = "TSHWANE MARKET"
MAR = "JOBURG MKT - BR / MAR"
SWEET = "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET 5kg)"
SUGRA = "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)"


def test_the_consignment_is_the_delivery_and_the_line():
    for delivery, line, consignment in ((1855491, 2, 185549102), (3803157, 1, 380315701),
                                        (1185847, 4, 118584704), (1187309, 3, 118730903)):
        assert reconcile.consignment_of(delivery, line) == consignment
        assert reconcile.delivery_of(consignment) == delivery


def test_a_payment_whose_ref_is_the_letter_n_binds_by_its_lines():
    """Durban 1855491Z is filed under ref 20026*N, so the payment has no DN and
    matching on DN could never reach it: the whole delivery read as unpaid."""
    sales = [_sale(185549101, SWEET, 71100.0, "2026-09-05", DBN, dn=1855491),
             _sale(185549102, SUGRA, 55480.0, "2026-09-05", DBN, dn=1855491)]
    pays = [_pay("DUR*13*202568", "743396", "2026-09-09",
                 [(1, "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE PUNNET 5.00 kg", 336, 71100.0),
                  (2, "GRAPES SUGRAONE CLASS 1 NO SIZE PUNNET 5.00 kg", 360, 55480.0)])]
    assert reconcile.bind_deliveries(sales, pays) == {"743396": 1855491}
    out = tracking.payment_status(sales, pays)
    assert out["still_to_come"] == 0.0 and out["unmatched"] == []
    assert out["fms_bound"] == 1


NECT = "NECTARINES OTHER CLASS 1 MEDIUM (MULTI LAYER TRAYER 5kg)"
ORANGES = "ORANGES NAVEL / OTHER CLASS 2 SMALL (EXPORT BOX 15kg)"
WHITE = "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)"


def test_two_deliveries_under_one_ref_are_told_apart():
    """Ref 14628 covers deliveries 1185847Z and 1185853Z. Pooled on the ref,
    Nett from one was spread over the other."""
    sales = [_sale(118584701, NECT, 3000.0, "2026-09-02", PRE, qty_amended=378, dn=14628),
             _sale(118585303, ORANGES, 400.0, "2026-09-08", PRE, qty_amended=10, dn=14628)]
    pays = [_pay("PRE*BT*1", "203475", "2026-09-04",
                 [(1, "NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 5.00 kg", 378, 3000.0)],
                 dn=14628),
            _pay("PRE*BT*2", "208023", "2026-09-10",
                 [(3, "ORANGES NAVEL / OTHER CLASS 2 SMALL EXPORT BOX 15.00 kg", 10, 400.0)],
                 dn=14628)]
    assert reconcile.bind_deliveries(sales, pays) == {"203475": 1185847, "208023": 1185853}


def test_the_booked_quantity_rules_out_a_lookalike_delivery():
    """Same market, same line number, same product: only the Delivered figure,
    which is the market's amended quantity, tells the two apart."""
    sales = [_sale(118584701, NECT, 3000.0, "2026-09-02", PRE, qty_amended=378, dn=14628),
             _sale(118700001, NECT, 900.0, "2026-09-02", PRE, qty_amended=120, dn=14700)]
    pays = [_pay("PRE*BT*1", "203475", "2026-09-04",
                 [(1, "NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 5.00 kg", 378, 3000.0)])]
    assert reconcile.bind_deliveries(sales, pays) == {"203475": 1185847}


def test_a_line_with_no_sales_of_its_own_still_resolves_through_its_delivery():
    """1185853Z line 01 sold before the sales on file, so it has no rows. FMS
    208023 binds to 1185853Z through line 03, and line 01 lands on 118585301
    rather than being lost."""
    sales = [_sale(118585303, ORANGES, 400.0, "2026-09-08", PRE, qty_amended=10, dn=14628)]
    pays = [_pay("PRE*BT*2", "208023", "2026-09-10",
                 [(1, "GRAPES WHITE SEEDLESS CLASS 2 NO SIZE PUNNET 5.00 kg", 90, 7200.0),
                  (3, "ORANGES NAVEL / OTHER CLASS 2 SMALL EXPORT BOX 15.00 kg", 10, 400.0)])]
    out = tracking.payment_status(sales, pays)
    assert out["still_to_come"] == 0.0
    assert [(u["consignment_id"], u["paid"]) for u in out["unmatched"]] == [(118585301, 7200.0)]


def test_the_reports_disagreeing_on_value_does_not_stop_a_match():
    """3803159Z line 04: 14 sold for R5 100 on the payment, 36 for R1 440 on
    the sales. Both are right, and it is the same consignment."""
    skye = "NECTARINES SKYE CLASS 2 MEDIUM (CARTON 8kg)"
    plums = "PLUMS OTHER CLASS 2 LARGE (ECONOMIC PACK 8kg)"
    sales = [_sale(380315904, skye, 1440.0, "2026-09-10", MAR, dn=14633),
             _sale(380315906, plums, 1000.0, "2026-09-10", MAR, dn=14633)]
    pays = [_pay("JOH*MAR*77", "6400001", "2026-09-15",
                 [(4, "NECTARINES SKYE CLASS 2 MEDIUM CARTON 8.00 kg", 40, 5100.0),
                  (6, "PLUMS OTHER CLASS 2 LARGE ECONOMIC PACK 8.00 kg", 104, 16600.0)])]
    assert reconcile.bind_deliveries(sales, pays) == {"6400001": 3803159}
    out = tracking.payment_status(sales, pays)
    assert out["still_to_come"] == 0.0
    assert out["batches_paid"] == 2


def test_an_ambiguous_payment_is_left_unbound_not_guessed():
    """Two deliveries fit equally and the ref names neither: bind nothing."""
    sales = [_sale(118584701, NECT, 3000.0, "2026-09-02", PRE, dn=14628),
             _sale(118700001, NECT, 900.0, "2026-09-02", PRE, dn=14700)]
    pays = [_pay("PRE*BT*1", "203475", "2026-09-04",
                 [(1, "NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 5.00 kg", 50, 3000.0)])]
    assert reconcile.bind_deliveries(sales, pays) == {}


def test_a_payment_saved_before_the_fms_id_still_matches_the_old_way():
    sales = [_sale(118584701, NECT, 3000.0, "2026-09-02", PRE, dn=14628)]
    pays = [{**_pay("PRE*BT*1", None, "2026-09-04",
                    [(None, "NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER 5.00 kg", 50, 3000.0)],
                    dn=14628)}]
    out = tracking.payment_status(sales, pays)
    assert out["still_to_come"] == 0.0 and out["fms_bound"] == 0


def test_a_payment_elsewhere_never_binds_to_this_market():
    sales = [_sale(185549102, SUGRA, 55480.0, "2026-09-05", DBN)]
    pays = [_pay("PRE*BT*9", "900001", "2026-09-09",
                 [(2, "GRAPES SUGRAONE CLASS 1 NO SIZE PUNNET 5.00 kg", 360, 55480.0)])]
    assert reconcile.bind_deliveries(sales, pays) == {}
