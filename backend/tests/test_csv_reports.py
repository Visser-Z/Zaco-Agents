"""CSV export readers, and the exact reconciliation they unlock.

The CSVs replace PDF layout-guessing with real values, and carry three fields
the PDFs never printed: the delivery date, the date paid, and the payment
reference each docket was settled under. That reference is a direct join
between a sale and its payment.
"""

from datetime import date

from app import csv_reports as cr, reconcile

DAILY = """\
"Delivery Date","Date Sold","Date Paid","Docket Number","Payment Reference","Qty Sold","Market Avg",Price,"Sales Value"
"TSHWANE MARKET","Farmers Trust (Pre)"
"Delivery ID : ",1178361Z,"Supplier Ref : ",20026*30559,"Qty Sent : ",25,"Qty Amended To : ",
"Consignment ID :",117836101Z
"Product :","NECTARINES OTHER CLASS 1 MEDIUM (MULTI LAYER TRAYER 11kg) 72"
2026-06-13,2026-06-17,2026-06-19,PRE*B6F17B47560*01Z,PRE*BT*385673,1,0.00,400.00,400.00
2026-06-13,2026-06-18,2026-06-19,PRE*B6F18BX8250*01Z,PRE*BT*385673,4,0.00,400.00,1600.00
2026-06-13,2026-06-22,2026-06-24,PRE*B6F22BT4084*01Z,PRE*BT*386367,5,0.00,400.00,2000.00
" "," "," "," "," ",10,,400.00,4000
"""

PAYMENTS = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"

203441,20026*30559,PRE*BT*385673,2026-06-19,1700.00,260.00,40.00,2000,

,,,"Line No","Supplier Ref",Commodity,Delivered,Sold,"Sales Total"
,,,01,20026*30559,"NECTARINES OTHER CLASS 1 MEDIUM MULTI LAYER TRAYER ",25,5,2000,

,,,,"Total Sales: ",,,2000,

,"DAILY TOTAL",,,1700.00,260.00,40.00,2000,

,"GRAND TOTAL",,,1700.00,260.00,40.00,2000,
"""


def test_format_detection():
    assert cr.is_daily_sales_csv(DAILY) and not cr.is_payment_details_csv(DAILY)
    assert cr.is_payment_details_csv(PAYMENTS) and not cr.is_daily_sales_csv(PAYMENTS)


def test_decode_handles_a_utf8_bom():
    assert cr.decode('﻿"Date Sold"'.encode("utf-8")).startswith('"Date Sold"')


def test_looks_like_csv_distinguishes_a_pdf():
    assert cr.looks_like_csv("June.csv", b"anything")
    assert not cr.looks_like_csv("June.pdf", b"%PDF-1.7 ...")


def _rows():
    """The one consignment in DAILY was settled under two account sales, so it
    is two rows."""
    rows = cr.parse_daily_sales_csv(DAILY, "june.csv")
    assert len(rows) == 2
    return rows


def _row():
    return _rows()[0]


def test_daily_csv_consignment():
    r = _row()
    assert r.supplier_ref == 30559
    assert r.dn == 30559                       # column A follows the Supplier Ref
    assert r.stm_no == 385673                  # column E is the account sale
    assert r.consignment_id == 117836101       # the delivery those rows share
    assert r.market == "TSHWANE MARKET" and r.market_agent == "Farmers Trust"
    assert r.qty_received == 25
    assert r.cartons_sold == 5                 # only what sold under 385673
    assert r.sales_total == 2000.0
    assert r.price == 400.0


def test_one_row_per_account_sale():
    """The account sale is the unit the market pays on and the unit the sheet
    keeps, so a consignment settled twice is two rows -- not one row summing
    both, which is what hid the stock position."""
    first, second = _rows()
    assert (first.stm_no, first.cartons_sold) == (385673, 5)
    assert (second.stm_no, second.cartons_sold) == (386367, 5)
    assert first.consignment_id == second.consignment_id
    assert first.qty_received == second.qty_received == 25


def test_stock_carries_forward_between_account_sales():
    """The second run opens at what the first left behind, and Baby Stock is
    that leftover."""
    first, second = _rows()
    assert (first.opening_stock, first.baby_stock) == (25, 20)
    assert (second.opening_stock, second.baby_stock) == (20, 15)


def test_delivery_date_is_used_for_arrival():
    """The PDFs never printed this, so time on market had to be guessed from
    the first sale. The CSV gives the real arrival date, and it belongs to the
    delivery, so every row of the consignment carries the same one."""
    first, second = _rows()
    assert first.date_received.isoformat() == "2026-06-13"    # delivered
    assert second.date_received.isoformat() == "2026-06-13"
    assert first.last_sale.isoformat() == "2026-06-18"        # last sold in this run


def test_column_d_dates_the_load_leaving_not_the_market_booking_it_in():
    """Checked against the operator's book: their Date column runs exactly one
    day before the export's Delivery Date on 38 of 38 rows, with no exceptions.
    Arrival is kept as the export states it, because time on market is measured
    from there -- only column D is dated the day the load went out."""
    first, second = _rows()
    assert first.date_received.isoformat() == "2026-06-13"
    assert first.date_sent.isoformat() == "2026-06-12"
    assert second.date_sent == first.date_sent      # one delivery, one date


def test_an_account_sales_pdf_date_is_never_shifted():
    """That format prints its own DATE RECEIVED, which is already the date column
    D wants. Shifting it would move a date the statement states outright."""
    from app import extraction
    from app.schemas import StatementRow

    row = StatementRow(source_file="AS_1.pdf", dn=1, date_received=date(2026, 6, 13))
    extraction.apply_group_dates([row])
    assert row.date == date(2026, 6, 13)


def test_status_is_the_account_sales_own_date():
    """Column T is the day the run was closed off and paid, not the day the
    fruit sold. Checked against the historical sheet: 38 of 38 rows agree with
    the payment date, none with the first sale date."""
    first, second = _rows()
    assert first.invoice_date.isoformat() == "2026-06-19" and first.status == "19.06"
    assert second.invoice_date.isoformat() == "2026-06-24" and second.status == "24.06"


def test_stray_number_is_stripped_from_the_csv_product_too():
    assert _row().product == "NECTARINES OTHER CLASS 1 MEDIUM (MULTI LAYER TRAYER 11kg)"


def test_payment_references_are_kept_per_reference():
    """Each row carries the one reference it was settled under, which is what
    makes the join to the payment report exact."""
    first, second = _rows()
    assert reconcile.parse_payment_refs(first.payment_refs) == {"PRE*BT*385673": 2000.0}
    assert reconcile.parse_payment_refs(second.payment_refs) == {"PRE*BT*386367": 2000.0}


def test_payments_csv():
    recs = cr.parse_payment_details_csv(PAYMENTS, "pay.csv")
    assert len(recs) == 1
    r = recs[0]
    assert r["accsale"] == "PRE*BT*385673" and r["stm_no"] == 385673
    assert r["dn"] == 30559
    assert r["gross"] == 2000.0 and r["nett"] == 1700.0
    assert r["market_agent"] == "Farmers Trust"
    assert [l["sales_total"] for l in r["lines"]] == [2000.0]


def test_totals_rows_are_not_read_as_data():
    """DAILY TOTAL / GRAND TOTAL / Total Sales rows would otherwise become
    phantom account sales and double the money."""
    recs = cr.parse_payment_details_csv(PAYMENTS, "pay.csv")
    assert len(recs) == 1
    assert round(sum(r["gross"] for r in recs), 2) == 2000.0


# --- the exact join -------------------------------------------------------

def test_reconcile_by_payment_reference_is_exact():
    daily = [{"dn": 30559, "stm_no": 117836101, "sales_total": 4000.0,
              "payment_refs": "PRE*BT*385673=2000.00;PRE*BT*386367=2000.00"}]
    recs = cr.parse_payment_details_csv(PAYMENTS, "pay.csv")
    out = {r["reference"]: r for r in reconcile.by_payment_reference(daily, recs)}
    assert out["PRE*BT*385673"]["status"] == "matched"
    assert out["PRE*BT*385673"]["payment_nett"] == 1700.0
    # Sold under a reference this payment file doesn't cover yet.
    assert out["PRE*BT*386367"]["status"] == "unpaid"


def test_nett_is_the_share_of_each_payment():
    """The consignment sold R2000 under a payment whose whole gross was R2000,
    so it takes that payment's entire nett -- and nothing for the run that
    hasn't arrived."""
    daily = [{"dn": 30559, "stm_no": 1, "sales_total": 4000.0,
              "payment_refs": "PRE*BT*385673=2000.00;PRE*BT*386367=2000.00"}]
    recs = cr.parse_payment_details_csv(PAYMENTS, "pay.csv")
    assert reconcile.fill_netts_by_reference(daily, recs) == 1
    assert daily[0]["nett_total"] == 1700.0


def test_partial_share_of_a_shared_payment():
    """Two consignments paid under one account sale each take their share."""
    recs = [{"accsale": "A", "dn": 1, "gross": 1000.0, "nett": 800.0, "lines": []}]
    daily = [
        {"stm_no": 1, "payment_refs": "A=250.00"},
        {"stm_no": 2, "payment_refs": "A=750.00"},
    ]
    reconcile.fill_netts_by_reference(daily, recs)
    assert daily[0]["nett_total"] == 200.0 and daily[1]["nett_total"] == 600.0


def test_rows_without_references_are_left_alone():
    recs = [{"accsale": "A", "dn": 1, "gross": 100.0, "nett": 80.0, "lines": []}]
    daily = [{"stm_no": 1, "payment_refs": None}]
    assert reconcile.fill_netts_by_reference(daily, recs) == 0
    assert "nett_total" not in daily[0]


# --- catching a filtered export ------------------------------------------
# The person running the export usually believes they took everything. The
# file says otherwise, in a line of its own, so read it back rather than trust.

FILTERED = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"

203423,20026*14799,PRE*BT*382405,2026-06-05,1700,260,40,2000,
"""

UNFILTERED = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Market,"ALL",,Agent,"ALL"

"JOBURG MKT - TFRESH","Subtropico (Jhb)"

6379636,20026*14565,JOH*SUB*5644102/1,2026-06-08,5112.71,771.56,115.73,6000,

"TSHWANE MARKET","Farmers Trust (Pre)"

203423,20026*14799,PRE*BT*382405,2026-06-05,1700,260,40,2000,
"""


def test_an_unpaid_docket_placeholder_is_not_a_payment_reference():
    """A docket awaiting payment carries PRE*BT*0 with Date Paid 0000-00-00, not a
    blank. Taken literally, every unpaid consignment across every DN pooled under
    one imaginary account sale."""
    assert cr._settled_under("PRE*BT*0") == ""
    assert cr._settled_under("PRE*BT*000") == ""
    assert cr._settled_under("") == ""
    assert cr._settled_under(None) == ""
    # A real one survives untouched.
    assert cr._settled_under("PRE*BT*385673") == "PRE*BT*385673"
    assert cr._settled_under("JOH*SUB*5644102/1") == "JOH*SUB*5644102/1"


def test_a_filtered_export_declares_itself():
    f = cr.export_filter(FILTERED)
    assert f["filtered"] is True
    assert f["market"] == "TSHWANE MARKET"
    assert f["agent"] == "Farmers Trust"


def test_all_is_not_a_filter():
    f = cr.export_filter(UNFILTERED)
    assert f["filtered"] is False
    assert f["market"] is None and f["agent"] is None


def test_a_file_with_no_filter_line_is_not_accused():
    """The Daily Sales export carries no filter line at all. Treating its
    absence as suspicious would cry wolf on every single import."""
    assert cr.export_filter(DAILY)["filtered"] is False


DOUBLE_QUOTED = "\n".join(
    '"' + line.replace('"', '""') + '"' for line in DAILY.strip().splitlines()
) + "\n"

DESTINATION_PAY = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Destination,"Subtropico (Jhb)"

6381269,20026*14986,JOH*SUB*5645755/2,2026-07-20,6166.64,1246.4,186.96,7600,EFT

,,,"Line No","Supplier Ref",Commodity,Delivered,Sold,"Sales Total"
,,,02,20026*14986,"NECTARINES OTHER CLASS 1 NO SIZE CARTON 8.00 kg",136,136,7600,
"""

TWO_SECTIONS = DESTINATION_PAY + """
Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"

203441,20026*30559,PRE*BT*385673,2026-06-19,1700.00,260.00,40.00,2000,
"""


def test_a_row_quoted_a_second_time_still_reads_as_columns():
    """Most of the weekly exports arrive double-encoded: the whole line inside
    one quoted field. csv.reader hands that back as a single cell, and every
    parser then matched nothing and reported ZERO rows -- no error, no rows. 11
    of 14 real weekly files were being read as empty."""
    assert cr.is_daily_sales_csv(DOUBLE_QUOTED)
    rows = cr.parse_daily_sales_csv(DOUBLE_QUOTED, "week24.csv")
    assert [(r.stm_no, r.cartons_sold) for r in rows] == [(385673, 5), (386367, 5)]
    assert rows[0].dn == 30559 and rows[0].product.startswith("NECTARINES")


def test_a_genuine_one_column_row_is_not_torn_apart():
    """The unwrapping must only fire on a row that really is a quoted line: a
    single cell with no commas in it is left exactly as it is."""
    assert cr._unwrap(["JOBURG MKT - TFRESH"]) == ["JOBURG MKT - TFRESH"]
    assert cr._unwrap(["one", "two"]) == ["one", "two"]


def test_destination_names_the_agent_when_market_is_absent():
    """Subtropico's exports declare their scope as Destination,"Subtropico
    (Jhb)" instead of Market/Agent. Missing it left every row on those weeks
    with no agent at all -- the field the workbook and every per-agent figure
    key on. The market is genuinely not in the file, so it stays unset rather
    than being guessed from the account-sale prefix."""
    recs = cr.parse_payment_details_csv(DESTINATION_PAY, "week29.csv")
    assert [r["market_agent"] for r in recs] == ["Subtropico"]
    assert recs[0]["market"] is None
    f = cr.export_filter(DESTINATION_PAY)
    assert f["agent"] == "Subtropico" and f["market"] is None and f["filtered"] is True


def test_a_file_of_several_sections_is_not_called_narrowed_to_the_first():
    """A weekly export can hold two reports one after another, each with its own
    header. Reading only the first would report the file as narrowed to
    Subtropico while half its money was Farmers Trust."""
    f = cr.export_filter(TWO_SECTIONS)
    assert f["filtered"] is False
    assert f["agents"] == ["Farmers Trust", "Subtropico"]
    assert {r["market_agent"] for r in cr.parse_payment_details_csv(TWO_SECTIONS, "w.csv")} == {
        "Subtropico", "Farmers Trust",
    }


def test_all_export_is_never_read_as_a_market_called_all():
    """The trap this guards: 'ALL' is a filter value, not a market. Taken
    literally every consignment would be labelled market 'ALL' and the real
    market names would be lost."""
    recs = cr.parse_payment_details_csv(UNFILTERED, "all.csv")
    markets = {r["market"] for r in recs}
    assert "ALL" not in markets
    assert markets == {"JOBURG MKT - TFRESH", "TSHWANE MARKET"}
    assert {r["market_agent"] for r in recs} == {"Subtropico", "Farmers Trust"}


# --- more than one market / agent in a file ------------------------------

MULTI_DAILY = """\
"Delivery Date","Date Sold","Date Paid","Docket Number","Payment Reference","Qty Sold","Market Avg",Price,"Sales Value"
"TSHWANE MARKET","Farmers Trust (Pre)"
"Delivery ID : ",1180699Z,"Supplier Ref : ",20026*14799,"Qty Sent : ",10,"Qty Amended To : ",
"Consignment ID :",118069901Z
"Product :","CHERRIES OTHER CLASS 1 LARGE (HALF TRAY 2.5kg)"
2026-06-01,2026-06-02,2026-06-05,PRE*B6E27C39125*01Z,PRE*BT*382405,10,0.00,200.00,2000.00
"JOBURG MKT - TFRESH","Subtropico (Jhb)"
"Delivery ID : ",1180700Z,"Supplier Ref : ",20026*14565,"Qty Sent : ",20,"Qty Amended To : ",
"Consignment ID :",118070001Z
"Product :","GRAPES THOMPSON SEEDLESS CLASS 1 NO SIZE (PUNNET 5kg)"
2026-06-03,2026-06-04,2026-06-08,JOH*S6E04X1234*01Z,JOH*SUB*5644102/1,20,0.00,300.00,6000.00
"""

MULTI_PAY = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"

203423,20026*14799,PRE*BT*382405,2026-06-05,1700,260,40,2000,

,,,"Line No","Supplier Ref",Commodity,Delivered,Sold,"Sales Total"
,,,01,20026*14799,"CHERRIES OTHER CLASS 1 LARGE HALF TRAY ",10,10,2000,

Market,"JOBURG MKT - TFRESH",,Agent,"Subtropico (Jhb)"

6379636,20026*14565,JOH*SUB*5644102/1,2026-06-08,5112.71,771.56,115.73,6000,

,,,"Line No","Supplier Ref",Commodity,Delivered,Sold,"Sales Total"
,,,01,20026*14565,"GRAPES THOMPSON SEEDLESS CLASS 1 NO SIZE PUNNET 5.00 kg",20,20,6000,
"""


def test_a_second_market_and_agent_are_not_lost():
    """An export covering more than one market must keep both. If the second
    band were missed its consignments would silently be attributed to the
    first agent, or dropped -- and the dashboards would show one market where
    there are two."""
    rows = cr.parse_daily_sales_csv(MULTI_DAILY, "multi.csv")
    assert [(r.market, r.market_agent) for r in rows] == [
        ("TSHWANE MARKET", "Farmers Trust"),
        ("JOBURG MKT - TFRESH", "Subtropico"),
    ]


def test_second_agents_payments_parse_and_reconcile():
    """Subtropico's account sales use a different numbering scheme
    (JOH*SUB*.../1) from Farmers Trust's (PRE*BT*...). Both must join."""
    recs = cr.parse_payment_details_csv(MULTI_PAY, "multi.csv")
    assert [(r["accsale"], r["market_agent"]) for r in recs] == [
        ("PRE*BT*382405", "Farmers Trust"),
        ("JOH*SUB*5644102/1", "Subtropico"),
    ]
    assert recs[1]["stm_no"] == 5644102        # sequence suffix dropped

    daily = [{"dn": r.dn, "stm_no": r.stm_no, "sales_total": r.sales_total,
              "payment_refs": r.payment_refs}
             for r in cr.parse_daily_sales_csv(MULTI_DAILY, "multi.csv")]
    out = {x["reference"]: x for x in reconcile.by_payment_reference(daily, recs)}
    assert out["JOH*SUB*5644102/1"]["status"] == "matched"
    assert out["PRE*BT*382405"]["status"] == "matched"


# --- edge cases seen in real April/May exports ---------------------------

RETURNS = """\
"Delivery Date","Date Sold","Date Paid","Docket Number","Payment Reference","Qty Sold","Market Avg",Price,"Sales Value"
"TSHWANE MARKET","Farmers Trust (Pre)"
"Delivery ID : ",1180699Z,"Supplier Ref : ",20026*14799,"Qty Sent : ",14,"Qty Amended To : ",
"Consignment ID :",118069901Z
"Product :","CHERRIES OTHER CLASS 1 LARGE (HALF TRAY 2.5kg)"
2026-05-27,2026-05-27,2026-05-29,PRE*B6E27C39125*01Z,PRE*BT*382405,3,0.00,200.00,600.00
2026-05-27,2026-05-28,2026-05-29,PRE*06E28B40280*01Z,PRE*BT*382405,-1,0.00,200.00,-200.00
2026-05-27,2026-05-29,2026-06-01,PRE*B6E29BT2574*01Z,PRE*BT*382860,1,0.00,200.00,200.00
" "," "," "," "," ",3,,200.00,600
"""

ODD_PAYMENTS = """\
"FMS ID","Supplier Ref","Acc Sales Number",Date,"Nett Payment","Total Deductions","Deduction VAT","Gross Payments","Payment Ref"
Market,"TSHWANE MARKET",,Agent,"Farmers Trust (Pre)"

203370,20026*14878,PRE*BT*378057,2026-04-30,0,-12952.51,-247.49,-13200,

203372,20026*14013,PRE*BT*378058,2026-04-30,0,0,0,0,

207279,20026*14890,PRE*BT*376975,2026-04-22,8552.61,1262.07,189.32,10004,

207312,20026*20026,PRE*BT*379661,2026-05-11,207.3,45.8,6.9,260,

,,,"Line No","Supplier Ref",Commodity,Delivered,Sold,"Sales Total"
,,,02,20026*20026,"GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE PUNNET 5.00 kg",63,2,-10,
,,,03,20026*20026,"GRAPES  CLASS 2 NO SIZE PUNNET 5.00 kg",31,2,240,

,,,,"Total Sales: ",,,260,

,"GRAND TOTAL",,,166260.68,21977.15,3292.1,140991.43,
"""


def test_returned_dockets_net_off_rather_than_being_dropped():
    """A return is a negative docket. Dropping it would overstate both the
    cartons sold and the takings. The return was booked under the same account
    sale as the sale it reverses, so it nets off within that row."""
    first, second = cr.parse_daily_sales_csv(RETURNS, "may.csv")
    assert (first.cartons_sold, first.sales_total) == (2, 400.0)    # 3 - 1
    assert (second.cartons_sold, second.sales_total) == (1, 200.0)
    assert sum(r.cartons_sold for r in (first, second)) == 3
    # The net is what the run paid and what the stock did, but the sale and the
    # return both happened and are kept as their own figures beside it.
    assert (first.cartons_returned, first.returns_total) == (1, 200.0)
    assert (second.cartons_returned, second.returns_total) == (0, 0.0)
    assert reconcile.parse_payment_refs(first.payment_refs) == {"PRE*BT*382405": 400.0}
    assert reconcile.parse_payment_refs(second.payment_refs) == {"PRE*BT*382860": 200.0}


def test_negative_and_zero_value_account_sales():
    """April carries a wholly negative account sale (a reversal) and two zero
    ones. Both must survive: a dropped reversal silently inflates the month."""
    recs = {r["accsale"]: r for r in cr.parse_payment_details_csv(ODD_PAYMENTS, "apr.csv")}
    assert recs["PRE*BT*378057"]["gross"] == -13200.0
    assert recs["PRE*BT*378057"]["deductions"] == -12952.51
    assert recs["PRE*BT*378058"]["gross"] == 0.0


def test_account_sale_with_no_commodity_lines_still_parses():
    recs = {r["accsale"]: r for r in cr.parse_payment_details_csv(ODD_PAYMENTS, "apr.csv")}
    assert recs["PRE*BT*376975"]["lines"] == []
    assert recs["PRE*BT*376975"]["gross"] == 10004.0


def test_negative_commodity_line_is_kept():
    recs = {r["accsale"]: r for r in cr.parse_payment_details_csv(ODD_PAYMENTS, "apr.csv")}
    lines = recs["PRE*BT*379661"]["lines"]
    assert [l["sales_total"] for l in lines] == [-10.0, 240.0]
    assert round(sum(l["sales_total"] for l in lines), 2) == 230.0


def test_grand_total_row_is_never_read_as_an_account_sale():
    """Its columns are shifted (gross sits in the nett position), so reading it
    as data would both invent a payment and corrupt the totals."""
    recs = cr.parse_payment_details_csv(ODD_PAYMENTS, "apr.csv")
    assert len(recs) == 4
    assert all(r["accsale"].startswith("PRE*BT*") for r in recs)
    assert not any(r["gross"] == 140991.43 for r in recs)


def test_parse_payment_refs_tolerates_a_bare_list():
    assert reconcile.parse_payment_refs("A;B") == {"A": 0.0, "B": 0.0}
    assert reconcile.parse_payment_refs(None) == {}
    assert reconcile.parse_payment_refs("") == {}
