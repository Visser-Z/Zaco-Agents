"""Request/response models shared with the frontend."""

from __future__ import annotations

# Aliased: StatementRow has a field literally named `date` (worksheet column D),
# which would otherwise shadow the type when annotations are evaluated.
from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["error", "warning"]


class Flag(BaseModel):
    """Something the extractor could not resolve confidently.

    `field` is the StatementRow attribute in question; the review screen uses it
    to highlight the offending cell. A flag with severity "error" blocks the
    append until a human resolves it.
    """

    field: str
    severity: Severity = "error"
    message: str
    # What kind of flag this is, for the UI to key on. Several different warnings
    # can land on the same field -- a statement already in the history and a
    # statement not paid yet both attach to `stm_no` -- and telling them apart by
    # reading the message would break the moment the wording changed.
    code: str | None = None


class StatementRow(BaseModel):
    """One extracted account-sales statement == one worksheet row."""

    # Provenance
    source_file: str

    # Extracted straight off the PDF
    dn: int | None = None                      # REFNO
    stm_no: int | None = None                  # ACCOUNT SALES NO
    product: str | None = None                 # PRODUCT (raw string from the PDF)
    qty_received: int | None = None            # QUANTITY RECEIVED
    opening_stock: int | None = None           # QUANTITY B/F
    cartons_sold: int | None = None            # price-table QUANTITY total
    price: float | None = None                 # AVER.PRICE
    # What came back. A negative docket is not a sale of minus one carton, it
    # reverses a sale already booked -- fruit returned to the floor. The
    # workbook columns stay NET (column J feeds K = H - J, so the stock only
    # balances if what came back is off the sold figure, and M = J x L only
    # recovers the money if L is priced over the same net), so the two figures
    # folded into that net are kept here instead of being thrown away. Both are
    # held POSITIVE: "280 returned", never "-280". None means the source could
    # not tell (the account-sales PDF, or a row read back out of a workbook);
    # 0 means the report showed the figure and it was nothing.
    cartons_returned: int | None = None
    returns_total: float | None = None
    # What the market itself averaged for this commodity that day, as the agent
    # reports it. Older exports left this column at 0.00 in every line, which is
    # why the price could never be checked; it is populated from August 2026.
    # None means the report did not carry it, which is not the same as zero.
    market_avg: float | None = None
    nett_total: float | None = None            # NETT AMOUNT
    date_received: Date | None = None          # DATE RECEIVED (when it reached the market)
    invoice_date: Date | None = None           # DATE (drives column T)
    # The day the load left, which is the date the operator's book records in
    # column D. It runs exactly one day before the market's Delivery Date on all
    # 38 rows that could be checked against their book, with no exceptions: the
    # load goes out and the market books it in the next morning. Only the CSV
    # export can give this -- the account-sales PDF prints its own DATE RECEIVED,
    # which is already the right date for column D and must not be shifted, so
    # that path leaves this empty.
    date_sent: Date | None = None

    # Derived / supplied
    description: str | None = None             # product -> short code, via lookup
    market_agent: str | None = None            # chosen per session in the UI
    market: str | None = None                  # market/depot the agent sold at (Daily Sales header)
    supplier_ref: int | None = None            # Supplier Ref (the DN shared with Payment Details)
    # The Consignment ID. Column E holds the *account sale* number, because that
    # is what the workbook is keyed on and what the payment report pays against;
    # a consignment sold over several payment runs becomes several rows. This
    # keeps the consignment those rows came from, so quantities that belong to
    # the consignment as a whole (what was sent) are not counted once per row.
    consignment_id: int | None = None
    # The market's own Delivery ID, and the producer code in front of the
    # Supplier Ref. Column A is Zaco's DN, which no export carries, so the
    # Delivery ID is what a captured DN is keyed on and the producer code is what
    # gives a placeholder ref (20026*20026) away. See ``delivery.py``.
    delivery_id: int | None = None
    producer_code: int | None = None
    sales_total: float | None = None           # exact consignment sales value, for Nett reconciliation
    last_sale: Date | None = None              # final sale date; with date_received gives days-to-sell
    # AccSale numbers this consignment was paid under, comma-separated. The CSV
    # export names them per docket, which is an exact join to the payment
    # report; one consignment paid across several runs keeps all of them.
    payment_refs: str | None = None
    completed: str = "Incomplete"
    date: Date | None = None                   # earliest date_received in the DN group
    status: str | None = None                  # invoice_date as DD.MM
    # Column U, the operator's own margin: "FT Received 87", "FT HET NIE
    # INGENEEM NIE". Nothing derives it and nothing may overwrite it; it is here
    # so a row read back out of the workbook keeps the note that was on it.
    notes: str | None = None

    flags: list[Flag] = Field(default_factory=list)

    @field_validator("stm_no", "dn", "consignment_id", mode="before")
    @classmethod
    def _zero_is_not_an_identity(cls, value):
        """0 means "unknown", never a real number.

        Two rows reached a saved workbook claiming to be statement 0, from a
        blank cell being coerced through Number(). A zero statement cannot be
        matched to a payment, and recorded as one it becomes a bucket that
        unrelated rows collide in. Nothing is lost by admitting it is unknown.
        """
        return None if value == 0 else value

    @property
    def blocking(self) -> bool:
        return any(f.severity == "error" for f in self.flags)

    @property
    def baby_stock(self) -> int | None:
        """What was left on the floor after this statement: column K, ``H - J``.

        Derived rather than stored, so it can never disagree with the two
        columns it comes from. The workbook holds it as that same formula, which
        is why the sheet's own Baby Stock column fills itself in.
        """
        if self.opening_stock is None or self.cartons_sold is None:
            return None
        return self.opening_stock - self.cartons_sold


class NettMatch(BaseModel):
    """Outcome of filling Nett onto sales rows from Nett Adjustment report(s)."""

    reports: int                 # how many adjustment PDFs were read this round
    report_statements: int       # distinct statements those reports covered
    matched: int                 # sales rows that received a Nett
    unmatched_rows: int          # sales rows with no matching statement
    unused: int                  # report statements that matched no row


class ExtractResponse(BaseModel):
    rows: list[StatementRow]
    unknown_products: list[str] = Field(default_factory=list)
    nett_match: NettMatch | None = None
    # Reasons to doubt this import is the whole picture: a filtered export, or
    # an agent that appears in the saved history but not in this file.
    warnings: list[str] = Field(default_factory=list)
    # statement number -> Nett, from any adjustment reports in this round. Lets
    # the client fill Nett onto rows added in an earlier drop too, so the two
    # reports need not arrive in the same drop.
    netts: dict[str, float] = Field(default_factory=dict)


class LookupEntry(BaseModel):
    product: str
    code: str
