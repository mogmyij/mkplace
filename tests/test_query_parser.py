from app.query_parser import parse_query


def test_max_price_and_keywords():
    r = parse_query("I'm looking for a used PS5 under $350 with two controllers")
    assert r["max_price"] == 350
    assert r["min_price"] is None
    assert r["keywords"] == "PS5"


def test_range():
    r = parse_query("herman miller aeron chair $300 - $600")
    assert (r["min_price"], r["max_price"]) == (300, 600)
    assert r["keywords"] == "herman miller aeron chair"


def test_k_suffix_and_preference_clause():
    r = parse_query("toyota corolla below 8k, must have low km")
    assert r["max_price"] == 8000
    assert r["keywords"] == "toyota corolla"


def test_model_numbers_are_not_prices():
    r = parse_query("rtx 3080 graphics card")
    assert r["min_price"] is None and r["max_price"] is None
    assert r["keywords"] == "rtx 3080 graphics card"


def test_min_price():
    r = parse_query("road bike over $500")
    assert r["min_price"] == 500
    assert r["keywords"] == "road bike"
