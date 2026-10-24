import json

import httpx
import pytest

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
                "kind": {"type": "choice", "choice": "genuine", "confidence": 0.95,
                         "probabilities": {"genuine": 0.95, "business_ad": 0.05}},
                "relevance": {"type": "score", "score": 2.9, "confidence": 0.9, "probabilities": {}},
                "deal": {"type": "choice", "choice": "great_deal", "confidence": 0.8,
                         "probabilities": {"great_deal": 0.8, "fair": 0.2}},
                "worth": {"type": "noul", "noul": 0.9},
            },
        })

    client = JevClient(api_key="k", url="https://x/v1/systemone", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)

    assert seen["auth"] == "Bearer k"
    assert set(seen["body"]["questions"]) == {"kind", "relevance", "deal", "worth"}
    assert json.loads(seen["body"]["state"])["buyer_budget"] == "max 350"
    assert out["ai_source"] == "jev-1.13.0"
    assert out["ai_reason"] == "Exact match · great deal"
    assert out["ai_score"] > 80
    assert (out["kind"], out["junk_prob"]) == ("genuine", 0.05)


def test_relevant_ad_cannot_rank():
    def handler(request):
        return httpx.Response(200, json={"answers": {
            "kind": {"choice": "business_ad", "probabilities": {"business_ad": 0.9, "genuine": 0.1}},
            "relevance": {"score": 3}, "deal": {"choice": "great_deal"}, "worth": {"noul": 0.9}}})

    client = JevClient(api_key="k", url="https://x", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)
    assert out["ai_reason"] == "Business ad"
    assert out["ai_score"] < 15
    assert out["junk_prob"] == 0.9


def test_jev_client_retries_on_429():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"answers": {
            "kind": {"choice": "scam"}, "relevance": {"score": 0}, "deal": {"choice": "unclear"}, "worth": {"noul": 0.1}}})

    client = JevClient(api_key="k", url="https://x", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)
    assert len(calls) == 2
    assert out["ai_reason"] == "Likely scam"
    assert out["ai_score"] == 0


def test_heuristic_penalises_over_budget():
    good = heuristic_classify(SEARCH, LISTING)
    pricey = heuristic_classify(SEARCH, {**LISTING, "price": 500})
    assert good["ai_score"] > pricey["ai_score"]
    assert pricey["deal"] == "overpriced"


@pytest.mark.parametrize("title,desc,price,kind", [
    ("WANTED: PS5 any condition", "", 300, "wanted"),
    ("PS5 consoles", "Finance available, many in stock", 650, "business_ad"),
    ("PS5 for rent weekly", "", 50, "service"),
    ("PS5 disc", "", 1, "bait_price"),
    ("PS5 brand new", "message me on whatsapp", 200, "scam"),
    # Wanted/service wording in a description is normal seller copy, not a wanted post.
    ("PS5 disc edition", "Great for anyone looking for a console. No repairs needed.", 300, "genuine"),
])
def test_heuristic_junk_patterns(title, desc, price, kind):
    out = heuristic_classify(SEARCH, {**LISTING, "title": title, "description": desc, "price": price,
                                      "price_text": f"${price}"})
    assert out["kind"] == kind
    assert (out["junk_prob"] >= 0.5) == (kind != "genuine")


def test_combine_bounds():
    assert combine(3, {"great_deal": 1}, 1, 1) == 100
    assert combine(3, {"great_deal": 1}, 0, 1) == 0


def test_openrouter_request_headers_and_cost():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={
            "id": "gen-1", "provider": "TypeSafe", "model": "typesafe/jev-1.14",
            "answers": {
                "kind": {"choice": "genuine", "probabilities": {"genuine": 1.0}},
                "relevance": {"score": 3}, "deal": {"choice": "fair"}, "worth": {"noul": 0.8}},
            "usage": {"input_tokens": 900, "output_tokens": 0, "cost": 0.0000378},
        })

    client = JevClient(api_key="sk-or-1", url="https://openrouter.ai/api/v1/systemone", model="~typesafe/jev-latest",
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.classify(SEARCH, LISTING)
    assert seen["url"] == "https://openrouter.ai/api/v1/systemone"
    assert seen["model"] == "~typesafe/jev-latest"
    assert seen["headers"]["authorization"] == "Bearer sk-or-1"
    assert seen["headers"]["x-title"] == "mkplace"
    assert out["ai_source"] == "typesafe/jev-1.14"
    assert out["cost"] == 0.0000378


def test_config_prefers_openrouter(monkeypatch):
    import importlib

    from app import config
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-1")
    monkeypatch.delenv("JEV_MODEL", raising=False)
    monkeypatch.delenv("JEV_URL", raising=False)
    try:
        importlib.reload(config)
        assert (config.JEV_PROVIDER, config.JEV_API_KEY) == ("OpenRouter", "sk-or-1")
        assert config.JEV_URL == "https://openrouter.ai/api/v1/systemone"
        assert config.JEV_MODEL == "~typesafe/jev-latest"
        monkeypatch.delenv("OPENROUTER_API_KEY")
        importlib.reload(config)
        assert (config.JEV_PROVIDER, config.JEV_API_KEY, config.JEV_MODEL) == ("TypeSafe", "ts-1", "jev-latest")
    finally:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        importlib.reload(config)
