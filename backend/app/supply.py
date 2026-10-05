"""What the supplier actually has this week, and where each carton of it goes.

The buy plan says what the markets would take. The supplier says what there
is, and there is never as much of everything as the plan would like: it comes
in per week, a few hundred cartons of this and none of that. So the operator
pastes the supplier's list as it arrived, and this reads it, matches each line
to a product on the book, and splits exactly that stock across the markets the
same way the plan splits its own order.

Reading the list is done by rules, not by the model: a carton count read wrong
is fruit sent to the wrong place. Each line gets the products it could be,
best first, and where two fit equally well (Sugraone Class 1 or Class 2) it
says so and leaves the choice to a person instead of guessing.
"""

from __future__ import annotations

import copy
import re

from . import analytics, dispatch, reconcile

# Words every product name carries; they say little about which product it is.
GENERIC = {"CLASS", "NO", "SIZE", "NOT", "GRADED", "VARIETY", "OTHER", "PUNNET", "CARTON",
           "BOX", "TRAY", "DOMPEL", "JUMBLE", "MULTI", "LAYER", "TRAYER", "DOUBLE", "STANDARD",
           "HALF", "BAG", "POCKET", "ECONOMIC", "PACK", "EXPORT", "BANANA", "CONTAINERS",
           "LARGE", "MEDIUM", "SMALL", "EXTRA", "UNCLASSED", "CTN", "CTNS", "CARTONS", "X"}
# How the supplier counts: "240 ctn", "240 cartons", "240 boxes", "x240".
_COUNT = re.compile(r"(\d+)\s*(?:ctns?|cartons?|boxes|box|bx|cases?|units?)\b", re.I)
_WEIGHT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:kg|kgs|g|gms)\b", re.I)
_CLASS = re.compile(r"\b(?:CLASS|CL|KL|C)\s*([1-4])\b")


def _tokens(text: str) -> set[str]:
    return {t for t in re.sub(r"[^A-Z0-9 ]", " ", text.upper()).split() if not t.isdigit()}


def _class_of(text: str) -> str | None:
    m = _CLASS.search(text.upper())
    return m[1] if m else None


def cartons_in(line: str) -> int | None:
    """The carton count on a supplier's line: the number named as cartons, or
    failing that the last number that is not a weight."""
    if m := _COUNT.search(line):
        return int(m[1])
    bare = _WEIGHT.sub(" ", line)
    bare = _CLASS.sub(" ", bare.upper())
    numbers = re.findall(r"\b\d+\b", bare)
    return int(numbers[-1]) if numbers else None


def _score(line: str, words: set[str], cls: str | None, product: str) -> float:
    name = reconcile.normalise_product(product)
    have = set(name.split())
    fruit = analytics.product_type(product).upper().split()[0]
    # Singular or plural, the supplier means the same fruit.
    plural = {w + "S" for w in words} | {w + "ES" for w in words}
    if fruit not in words and not (plural & {fruit}):
        # A line that names a fruit names this one or no other.
        fruits_named = {w for w in words | plural if w in _FRUITS}
        if fruits_named:
            return 0.0
    hits = (words | plural) & have
    score = sum(1.0 if w in GENERIC else 3.0 for w in hits)
    pcls = _class_of(name)
    if cls and pcls:
        score += 2.0 if cls == pcls else -6.0
    return score


_FRUITS = {"GRAPES", "NECTARINES", "PLUMS", "CHERRIES", "ORANGES", "GRANADILLAS",
           "STRAWBERRIES", "PEACHES", "APRICOTS", "LEMONS", "MANDARINS", "PEARS", "APPLES",
           "AVOCADOS", "MANGOES", "GRAPEFRUIT", "BLUEBERRIES", "LITCHIS", "PINEAPPLES"}


def read(text: str, products: list[dict]) -> list[dict]:
    """The supplier's list, line by line, each with what it could be.

    `products` is the plan's lines: every product with history, with how much
    of it moves in a month, which breaks a tie in favour of the one the
    business actually trades.
    """
    out = []
    for raw in re.split(r"[\n;]+", text or ""):
        line = raw.strip(" \t\r-*•·")
        if not line or not re.search(r"[A-Za-z]", line):
            continue
        cartons = cartons_in(line)
        words = _tokens(_WEIGHT.sub(" ", line))
        cls = _class_of(line)
        scored = sorted(
            ((s, p) for p in products if (s := _score(line, words, cls, p["product"])) > 0),
            key=lambda sp: (-sp[0], -(sp[1].get("monthly_cartons") or 0)))
        candidates = [p["product"] for _, p in scored[:6]]
        # Only a clear winner is chosen: a variety named and nothing as good.
        specific = bool(scored) and any(w not in GENERIC and w not in _FRUITS
                                        and w + "S" not in _FRUITS
                                        for w in (words & set(reconcile.normalise_product(
                                            scored[0][1]["product"]).split())))
        sure = len(scored) == 1 or (specific and scored[0][0] > scored[1][0])
        out.append({"text": line, "cartons": cartons,
                    "product": candidates[0] if sure else None,
                    "candidates": candidates})
    return out


def plan(full: dict, items: list[dict], rows: list[dict], payments: list[dict],
         months: int) -> dict:
    """The plan cut to exactly what the supplier has, and split across markets.

    Each product's order becomes the supplier's cartons of it, whatever the
    plan would have liked. Room to grow and test loads are left out: with a
    fixed supply there is nothing on top to grow with, and a test load would
    come out of a market that has already shown it sells the fruit.
    """
    by_product = {l["product"]: l for l in full.get("lines") or []}
    lines = []
    unknown = []
    want: dict[str, int] = {}
    for item in items:
        product, cartons = item.get("product"), int(item.get("cartons") or 0)
        if cartons <= 0:
            continue
        if not product or product not in by_product:
            unknown.append({"text": item.get("text") or product, "cartons": cartons})
            continue
        want[product] = want.get(product, 0) + cartons
    for product, cartons in want.items():
        line = copy.deepcopy(by_product[product])
        line.update(take_on=cartons, headroom=None, trial=None,
                    planned=by_product[product]["take_on"])
        lines.append(line)
    constrained = {**{k: v for k, v in full.items() if k not in ("lines", "priorities", "dispatch")},
                   "lines": lines}
    constrained["dispatch"] = dispatch.build(constrained, rows, payments, months)
    constrained["supply"] = {
        "cartons": sum(want.values()),
        "products": len(want),
        "unknown": unknown,
        # Where the supplier has less than the markets would take, and more.
        "short": [{"product": l["product"], "have": l["take_on"], "plan": l["planned"]}
                  for l in lines if l["planned"] > l["take_on"]],
        "over": [{"product": l["product"], "have": l["take_on"], "plan": l["planned"]}
                 for l in lines if l["take_on"] > l["planned"]],
    }
    return constrained
