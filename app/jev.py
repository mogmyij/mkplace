"""Score listings with TypeSafe's Jev model (falls back to a heuristic without an API key).

Jev answers typed questions about a "state" string: Choice (pick an option), Score (rubric
level) and Noul (probability a statement is true). We ask four questions per listing and
blend them into a 0-100 "worth looking at" score.
"""
import json
import logging
import re
import time

import httpx

from . import config

log = logging.getLogger(__name__)

RELEVANCE_LEVELS = [
    "Not the item the buyer wants",
    "Related but wrong (accessory, parts, different model, wanted ad)",
    "Close match with some differences from the request",
    "Exactly what the buyer asked for",
]
DEAL_OPTIONS = {
    "great_deal": "Price is clearly below the typical used price for this item and condition",
    "fair": "Price is around the typical used price for this item and condition",
    "overpriced": "Price is above the typical used price for this item and condition",
    "unclear": "Not enough information to judge whether the price is good",
}
DEAL_VALUE = {"great_deal": 1.0, "fair": 0.6, "unclear": 0.4, "overpriced": 0.1}
DEAL_LABEL = {"great_deal": "great deal", "fair": "fair price", "overpriced": "overpriced", "unclear": "price unclear"}

QUESTIONS = {
    "relevance": {
        "type": "score",
        "instructions": "How well does this Facebook Marketplace listing match what the buyer is looking for?",
        "criteria": RELEVANCE_LEVELS,
    },
    "deal": {
        "type": "choice",
        "instructions": "Judge the asking price against what this item usually sells for second-hand.",
        "criteria": DEAL_OPTIONS,
    },
    "scam": {
        "type": "noul",
        "instructions": (
            "This listing shows signs of being a scam or bait: a price too good to be true, a placeholder "
            "price like $1 or $123, requests to move off Facebook, shipping-only, or vague copy-pasted text."
        ),
    },
    "worth": {
        "type": "noul",
        "instructions": (
            "Given the buyer's request and budget, this listing is worth the buyer's time to open and look at."
        ),
    },
}


def build_state(search: dict, listing: dict) -> str:
    budget = []
    if search.get("min_price") is not None:
        budget.append(f"min {search['min_price']:g}")
    if search.get("max_price") is not None:
        budget.append(f"max {search['max_price']:g}")
    return json.dumps(
        {
            "buyer_request": search["query"],
            "buyer_budget": ", ".join(budget) or "not specified",
            "listing": {
                "title": listing.get("title"),
                "price": listing.get("price_text") or listing.get("price"),
                "location": listing.get("location"),
                "description": (listing.get("description") or "")[:3000] or "not available",
            },
        },
        ensure_ascii=False,
    )


def combine(relevance: float, deal_probs: dict, scam: float, worth: float) -> float:
    """relevance is 0-3; returns 0-100."""
    deal_value = sum(DEAL_VALUE.get(k, 0.4) * p for k, p in deal_probs.items()) if deal_probs else 0.4
    base = 0.45 * worth + 0.35 * (relevance / 3) + 0.20 * deal_value
    return round(100 * base * (1 - 0.8 * scam), 1)


def describe(relevance: float, deal: str, scam: float) -> str:
    match = ["Not a match", "Loosely related", "Close match", "Exact match"][min(3, max(0, round(relevance)))]
    parts = [match, DEAL_LABEL.get(deal, deal)]
    if scam >= 0.5:
        parts.append("likely scam")
    elif scam >= 0.25:
        parts.append("some scam signals")
    return " · ".join(parts)


class JevClient:
    def __init__(self, api_key: str = config.TYPESAFE_API_KEY, url: str = config.TYPESAFE_URL,
                 model: str = config.JEV_MODEL, http: httpx.Client | None = None):
        self.api_key = api_key
        self.url = url
        self.model = model
        self.http = http or httpx.Client(timeout=30)

    def ask(self, state: str, questions: dict) -> dict:
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        delay = 0.5
        for attempt in range(4):
            resp = self.http.post(self.url, json=body, headers=headers)
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                if attempt == 3:
                    resp.raise_for_status()
                retry_after = resp.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 5)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def classify(self, search: dict, listing: dict) -> dict:
        data = self.ask(build_state(search, listing), QUESTIONS)
        a = data["answers"]
        relevance = float(a["relevance"]["score"])
        deal = a["deal"]["choice"]
        scam = float(a["scam"]["noul"])
        worth = float(a["worth"]["noul"])
        return {
            "ai_score": combine(relevance, a["deal"].get("probabilities") or {deal: 1.0}, scam, worth),
            "ai_reason": describe(relevance, deal, scam),
            "ai_source": data.get("model", self.model),
            "deal": deal,
            "scam_prob": scam,
            "ai_raw": json.dumps(a),
        }


def heuristic_classify(search: dict, listing: dict) -> dict:
    """Keyword overlap + budget check, used when no Jev API key is configured."""
    words = {w for w in re.findall(r"\w+", search["keywords"].lower()) if len(w) > 1}
    title = (listing.get("title") or "").lower() + " " + (listing.get("description") or "").lower()
    overlap = sum(1 for w in words if w in title) / max(1, len(words))
    relevance = 3 * overlap
    price, max_price = listing.get("price"), search.get("max_price")
    if price is None:
        deal = "unclear"
    elif max_price and price > max_price:
        deal = "overpriced"
    else:
        deal = "fair"
    scam = 0.6 if price is not None and 0 < price <= 5 else 0.05
    worth = overlap * (0.3 if deal == "overpriced" else 1.0)
    return {
        "ai_score": combine(relevance, {deal: 1.0}, scam, worth),
        "ai_reason": describe(relevance, deal, scam),
        "ai_source": "heuristic",
        "deal": deal,
        "scam_prob": scam,
        "ai_raw": None,
    }


def get_classifier():
    if config.TYPESAFE_API_KEY:
        client = JevClient()

        def classify(search, listing):
            try:
                return client.classify(search, listing)
            except Exception as e:  # keep crawling even if Jev is down
                log.warning("Jev failed for %s (%s); using heuristic", listing.get("id"), e)
                return heuristic_classify(search, listing)

        return classify
    return heuristic_classify
