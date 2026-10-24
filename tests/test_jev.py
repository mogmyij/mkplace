import json

import httpx

from app.jev import JevClient, combine, heuristic_classify

SEARCH = {"query": "PS5 under $350", "keywords": "PS5", "min_price": None, "max_price": 350}
LISTING = {"id": "1", "title": "Sony PS5 disc edition", "price": 300, "price_text": "$300", "location": "Sydney"}


def test_jev_client_request_and_scoring():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {
                "relevance": {"type": "score", "score": 2.9, "confidence": 0.9, "probabilities": {}},
                "deal": {"type": "choice", "choice": "great_deal", "confidence": 0.8,
                         "probabilities": {"great_deal": 0.8, "fair": 0.2}},
                "scam": {"type": "noul", "noul": 0.05},
                "worth": {"type": "noul", "noul": 0.9},
            },
        })

    client = JevClient(api_key="k", url="https://x/v1/systemone", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)

    assert seen["auth"] == "Bearer k"
    assert set(seen["body"]["questions"]) == {"relevance", "deal", "scam", "worth"}
    assert json.loads(seen["body"]["state"])["buyer_budget"] == "max 350"
    assert out["ai_source"] == "jev-1.13.0"
    assert out["ai_reason"] == "Exact match · great deal"
    assert out["ai_score"] > 85


def test_jev_client_retries_on_429():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"answers": {
            "relevance": {"score": 0}, "deal": {"choice": "unclear"}, "scam": {"noul": 0.9}, "worth": {"noul": 0.1}}})

    client = JevClient(api_key="k", url="https://x", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)
    assert len(calls) == 2
    assert "likely scam" in out["ai_reason"]
    assert out["ai_score"] < 15


def test_heuristic_penalises_over_budget():
    good = heuristic_classify(SEARCH, LISTING)
    pricey = heuristic_classify(SEARCH, {**LISTING, "price": 500})
    assert good["ai_score"] > pricey["ai_score"]
    assert pricey["deal"] == "overpriced"


def test_combine_bounds():
    assert combine(3, {"great_deal": 1}, 0, 1) == 100
    assert combine(0, {"overpriced": 1}, 1, 0) < 1
