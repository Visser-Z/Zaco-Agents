"""Zaco's own DN, which no export carries.

Column A of the workbook is Zaco's delivery note number. The market's exports
give a Delivery ID (the market's own) and a Supplier Ref, and the Supplier Ref
only *sometimes* holds the DN. Measured against the operator's own book over
June: 26 of 43 statements agreed, 17 did not, because that field held

    20026*14013   a producer code, seen across three different deliveries
    20026*20026   a placeholder: the producer code repeated
    20026*30559   another number entirely

and in those cases the real DN is in the export nowhere at all. One Zaco DN can
also cover several market deliveries (14841 covers both 1178361 and 1180695), so
it cannot be derived even in principle.

So it is captured rather than guessed, like the product short codes: corrected
once for a delivery, reused from then on. Two things here:

  ``apply_notes``  fill in the DN for deliveries already known
  ``flag_unproven`` say so where the Supplier Ref demonstrably is NOT a DN

The second only fires on positive evidence, never on suspicion, because a flag on
every row would be ignored within a week.
"""

from __future__ import annotations

from .schemas import Flag, StatementRow


def apply_notes(rows: list[StatementRow], notes: dict[int, int]) -> int:
    """Set column A from the known Delivery ID -> DN mapping. Returns rows set."""
    changed = 0
    for row in rows:
        dn = notes.get(row.delivery_id) if row.delivery_id is not None else None
        if dn is not None and row.dn != dn:
            row.dn = dn
            changed += 1
    return changed


def producer_codes(rows: list[StatementRow]) -> set[int]:
    """Numbers used as producer codes somewhere in this round.

    The Supplier Ref is written ``producer*reference``, and the giveaway for a
    ref that is not a delivery note is that the same number is used as a
    *producer* elsewhere: June carries both ``14013*14798`` and ``20026*14013``,
    so 14013 is a producer, and the 14013 in the second is not a delivery.

    Note what is deliberately NOT used as evidence: a ref appearing under two
    different Delivery IDs. One Zaco DN genuinely covers several market
    deliveries (14841 covers both 1178361 and 1180695), so that test called 35
    correct rows wrong out of 57 it flagged.
    """
    return {r.producer_code for r in rows if r.producer_code is not None}


# Below this many known DNs there is no reliable range to judge a new one by.
_MIN_KNOWN_FOR_RANGE = 10


def flag_unproven(rows: list[StatementRow], notes: dict[int, int]) -> int:
    """Flag rows whose column A is not known to be Zaco's DN. Returns rows flagged.

    Only on positive evidence: the ref is missing, it is a producer code, or it
    falls outside the range of DNs already on file. Anything else is left alone,
    because it is right far more often than not and a warning on every row is a
    warning nobody reads by the second week.
    """
    producers = producer_codes(rows)
    known = sorted(notes.values())
    lo, hi = (known[0], known[-1]) if len(known) >= _MIN_KNOWN_FOR_RANGE else (None, None)

    flagged = 0
    for row in rows:
        if row.delivery_id is not None and row.delivery_id in notes:
            continue
        ref = row.supplier_ref
        # A number used as a producer code is not a delivery note, including the
        # producer code echoed back at itself ("20026*20026").
        is_producer = ref is not None and ref in producers
        # A DN nowhere near the ones on file. Only once there are enough to say.
        out_of_range = ref is not None and lo is not None and not (lo <= ref <= hi)
        if ref is None or is_producer or out_of_range:
            row.flags.append(
                Flag(
                    field="dn",
                    severity="warning",
                    code="dn_unproven",
                    message=(
                        f"The market's Supplier Ref here is not your delivery note number "
                        f"(delivery {row.delivery_id}). Enter your DN once and every row of "
                        f"this delivery will use it, now and in future."
                    ),
                )
            )
            flagged += 1
    return flagged


def seed_from_history(rows: list[StatementRow], book: dict[int, int]) -> dict[int, int]:
    """Recover the mapping from the operator's own workbook, typing nothing.

    The DN cannot be derived from the market's data, but it does not have to be.
    The workbook holds **DN plus account sale**; the export holds **Delivery ID
    plus account sale**. The account sale is common to both, so joining them
    produces Delivery ID -> DN for free.

    Measured on the real Golden book against the June export: 49 account sales
    bridged, **15 of 15 deliveries recovered, no conflicts**, which answers every
    row and corrects the 30 that were wrong.

    ``book`` is ``{account sale: DN}`` as read back out of the open workbook.
    Two cases are refused rather than guessed:

      * an account sale covering more than one delivery, which cannot be split
      * a delivery whose account sales disagree about the DN, which means one of
        the two sides is wrong and a coin toss would bury it
    """
    # Which deliveries does each account sale in this round belong to?
    per_statement: dict[int, set[int]] = {}
    for row in rows:
        if row.stm_no is not None and row.delivery_id is not None:
            per_statement.setdefault(row.stm_no, set()).add(row.delivery_id)

    candidates: dict[int, set[int]] = {}
    for stm, deliveries in per_statement.items():
        dn = book.get(stm)
        if dn is None or len(deliveries) != 1:
            continue
        candidates.setdefault(next(iter(deliveries)), set()).add(dn)

    return {d: next(iter(dns)) for d, dns in candidates.items() if len(dns) == 1}


def learned(rows: list[StatementRow], notes: dict[int, int]) -> dict[int, int]:
    """Delivery ID -> DN pairs worth remembering from a reviewed round.

    Only where the operator's row disagrees with what was already on file, so a
    round that changed nothing writes nothing.
    """
    out: dict[int, int] = {}
    for row in rows:
        if row.delivery_id is None or row.dn is None:
            continue
        if notes.get(row.delivery_id) != row.dn:
            out[row.delivery_id] = row.dn
    return out
