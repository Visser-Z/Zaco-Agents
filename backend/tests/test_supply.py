"""The supplier's stock: read as pasted, matched to products, split across markets."""

from app import supply

LINES = [
    {"product": "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)", "monthly_cartons": 400},
    {"product": "GRAPES SUGRAONE CLASS 2 NO SIZE (PUNNET 5kg)", "monthly_cartons": 90},
    {"product": "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET 5kg)", "monthly_cartons": 300},
    {"product": "GRAPES SWEET CELEBRATION CLASS 2 NO SIZE (PUNNET 5kg)", "monthly_cartons": 200},
    {"product": "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)", "monthly_cartons": 150},
    {"product": "CHERRIES OTHER CLASS 1 MEDIUM (STANDARD TRAY 4.5kg)", "monthly_cartons": 40},
    {"product": "PLUMS ELDORADO CLASS 1 NO SIZE (DOUBLE LAYER TRAY 5kg)", "monthly_cartons": 180},
]


def read(text):
    return [(i["cartons"], i["product"]) for i in supply.read(text, LINES)]


def test_cartons_are_the_count_not_the_weight_or_the_class():
    assert supply.cartons_in("Sugraone class 1 5kg - 240 ctns") == 240
    assert supply.cartons_in("Crimson seedless cl2 x 120") == 120
    assert supply.cartons_in("Plums 4.5kg 60") == 60
    assert supply.cartons_in("Cherries") is None


def test_a_line_that_names_its_variety_and_class_is_matched():
    assert read("Sugraone class 1 5kg 240 ctns") == [
        (240, "GRAPES SUGRAONE CLASS 1 NO SIZE (PUNNET 5kg)")]
    assert read("Crimson seedless cl2 x 120") == [
        (120, "GRAPES CRIMSON SEEDLESS CLASS 2 NO SIZE (PUNNET 5kg)")]


def test_a_line_two_products_fit_is_left_for_a_person():
    """No class given: Sweet Celebration Class 1 or Class 2. Both are offered,
    the one traded more first, and neither is chosen."""
    items = supply.read("Sweet celebration 180 cartons", LINES)
    assert items[0]["product"] is None
    assert items[0]["candidates"][:2] == [
        "GRAPES SWEET CELEBRATION CLASS 1 NO SIZE (PUNNET 5kg)",
        "GRAPES SWEET CELEBRATION CLASS 2 NO SIZE (PUNNET 5kg)"]


def test_the_only_product_of_a_fruit_is_taken_and_an_unknown_fruit_is_not():
    assert read("Cherries 30") == [(30, "CHERRIES OTHER CLASS 1 MEDIUM (STANDARD TRAY 4.5kg)")]
    items = supply.read("Mangoes 50", LINES)
    assert items[0]["product"] is None and items[0]["candidates"] == []


def test_windows_line_endings_and_bullets_are_not_part_of_the_line():
    items = supply.read("- Cherries 30\r\n• Plums eldorado 60 boxes\r\n", LINES)
    assert [i["text"] for i in items] == ["Cherries 30", "Plums eldorado 60 boxes"]


def dest(market, back, cartons):
    return {"market": market, "market_agent": market.title(), "back_per_carton": back,
            "cartons": cartons, "sell_through": 1.0, "days_to_sell": 5.0, "price": back * 1.2}


def test_exactly_the_suppliers_stock_is_split_and_nothing_on_top():
    full = {"month": "2026-10", "horizon": {"days": 7, "label": "the next 7 days"},
            "window": {"from": "2026-07-01", "to": "2026-09-28"},
            "lines": [{"product": LINES[0]["product"], "take_on": 100, "priority": "high",
                       "fruit": "Grapes", "market": "DURBAN", "market_agent": None,
                       "back_per_carton": 300.0,
                       "headroom": {"cartons": 20, "market": "DURBAN"},
                       "trial": {"cartons": 10, "market": "SPRINGS"},
                       "destinations": [dest("DURBAN", 300.0, 900), dest("TSHWANE", 250.0, 900)]}]}
    items = [{"product": LINES[0]["product"], "cartons": 150},
             {"product": None, "text": "Mangoes 50", "cartons": 50}]
    out = supply.plan(full, items, [], [], 3)
    d = out["dispatch"]
    assert d["cartons"] == 150 and d["optional"] == 0
    # Each takes about 70 a week; the 10 beyond both go to the better one,
    # marked as more than it takes.
    assert {g["market"]: (g["cartons"], g["over"]) for g in d["markets"]} == {
        "DURBAN": (80, 10), "TSHWANE": (70, 0)}
    assert out["supply"]["unknown"] == [{"text": "Mangoes 50", "cartons": 50}]
    assert out["supply"]["over"] == [{"product": LINES[0]["product"], "have": 150, "plan": 100}]
    # The plan it was cut from is untouched.
    assert full["lines"][0]["take_on"] == 100
