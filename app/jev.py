"""Score listings with TypeSafe's Jev model (falls back to a heuristic without an API key).

Jev answers typed questions about a "state" string: Choice (pick an option), Score (rubric
level) and Noul (probability a statement is true). Its main job here is separating genuine
listings from the noise Marketplace is full of: wanted posts, dealer/reseller ads, services
and rentals, placeholder prices, and scams. Only genuine listings can score well; relevance
and price then rank them.
"""
import base64
import json
import logging
import re
import time

import httpx

from . import config

log = logging.getLogger(__name__)

KIND_OPTIONS = {
    "genuine": "A real seller offering one specific item for sale, with a real asking price",
    "wanted": "A wanted, ISO, or 'looking for' post, or someone offering to buy items ('we buy', 'cash for')",
    "business_ad": (
        "A dealer, store, or reseller ad: stock lists, 'many available', financing or weekly payments, "
        "'message for prices', a brand-new item repeatedly posted by a business, or a catalogue of different items"
    ),
    "service": "Advertises a service, rental, job, lesson, repair, removal, or real estate rather than an item",
    "bait_price": (
        "Uses a fake or misleading price to game search: $0, $1, $123, $1234, a deposit or per-week price, "
        "or 'price is negotiable, DM' with no real price"
    ),
    "scam": (
        "Shows scam signs: too good to be true, asks to move off Facebook (WhatsApp, email, phone), shipping-only "
        "for a big item, payment by gift card or deposit up front, or generic stock text and photos"
    ),
}
KIND_LABEL = {
    "genuine": "Genuine",
    "wanted": "Wanted post",
    "business_ad": "Business ad",
    "service": "Service/rental ad",
    "bait_price": "Bait price",
    "scam": "Likely scam",
}

RELEVANCE_LEVELS = [
    "Not the item the buyer wants",
    "Related but wrong (accessory, parts, different model)",
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
    "kind": {
        "type": "choice",
        "instructions": "What kind of Facebook Marketplace post is this?",
        "criteria": KIND_OPTIONS,
    },
    "relevance": {
        "type": "score",
        "instructions": "How well does this listing match what the buyer is looking for?",
        "criteria": RELEVANCE_LEVELS,
    },
    "deal": {
        "type": "choice",
        "instructions": "Judge the asking price against what this item usually sells for second-hand.",
        "criteria": DEAL_OPTIONS,
    },
    "worth": {
        "type": "noul",
        "instructions": (
            "This is a genuine listing for what the buyer wants, within their budget, and worth messaging the "
            "seller about."
        ),
    },
}


# Photo check: one request per listing and search with ALL of the listing's photos, the
# buyer's request and the listing text, asking general "is this worth a look?" questions.
# It can only pull a score down, and only when Jev leans clearly negative (see photo_factor).
CONDITION_LEVELS = [
    "Broken, badly damaged, or missing parts",
    "Worn, with clearly visible flaws",
    "Good used condition with normal wear",
    "Like new or new",
]
CONDITION_LABEL = ["Broken", "Worn", "Good", "Like new"]
PHOTO_QUESTIONS = {
    "worth": {
        "type": "noul",
        "instructions": (
            "Judging from the photos, this listing is worth the buyer's time to open and look at closely "
            "for what they want."
        ),
    },
    "genuine": {
        "type": "noul",
        "instructions": (
            "The photos are the seller's own pictures of the actual item for sale, not stock or catalogue "
            "images, screenshots, adverts or graphics. Plain, dark or messy photos still count as genuine."
        ),
    },
    "condition": {
        "type": "score",
        "instructions": "Judging from all the photos, what condition is the item in?",
        "criteria": CONDITION_LEVELS,
    },
}
PHOTO_LABEL = {"worth": "Photos not convincing", "genuine": "Not the seller's own photos"}
# A "no" only counts below 50%; at 0% the score is multiplied by 1 - PHOTO_MAX_PENALTY.
PHOTO_PASS, PHOTO_MAX_PENALTY = 0.5, 0.6


def photo_factor(row: dict) -> tuple[float, list[str]]:
    """Multiplier (0.4-1.0) for a text score from its photo check, plus labels to show on the card."""
    if row.get("photo_worth") is None:
        return 1.0, []
    factor, labels = 1.0, []
    for key, label in PHOTO_LABEL.items():
        p = row.get(f"photo_{key}")
        if p is not None and p < PHOTO_PASS:
            factor = min(factor, 1 - PHOTO_MAX_PENALTY * (PHOTO_PASS - p) / PHOTO_PASS)
            labels.append(label)
    if row.get("photo_condition") is not None:
        labels.append(f"Condition: {CONDITION_LABEL[min(3, max(0, round(row['photo_condition'])))]}")
    return factor, labels


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


def combine(relevance: float, deal_probs: dict, genuine: float, worth: float) -> float:
    """relevance is 0-3, genuine is P(kind == genuine); returns 0-100."""
    deal_value = sum(DEAL_VALUE.get(k, 0.4) * p for k, p in deal_probs.items()) if deal_probs else 0.4
    base = 0.45 * worth + 0.35 * (relevance / 3) + 0.20 * deal_value
    # Ads, wanted posts and scams can't rank, however relevant they look.
    return round(100 * base * genuine, 1)


def describe(kind: str, relevance: float, deal: str) -> str:
    if kind != "genuine":
        return KIND_LABEL.get(kind, kind)
    match = ["Not a match", "Loosely related", "Close match", "Exact match"][min(3, max(0, round(relevance)))]
    return f"{match} · {DEAL_LABEL.get(deal, deal)}"


def result(kind: str, kind_probs: dict, relevance: float, deal: str, deal_probs: dict, worth: float,
           source: str, raw=None, cost: float = 0.0) -> dict:
    genuine = kind_probs.get("genuine", 1.0 if kind == "genuine" else 0.0)
    return {
        "text_score": combine(relevance, deal_probs, genuine, worth),
        "text_reason": describe(kind, relevance, deal),
        "ai_source": source,
        "kind": kind,
        "deal": deal,
        "junk_prob": round(1 - genuine, 3),
        "ai_raw": json.dumps(raw) if raw is not None else None,
        "cost": cost,
    }


class JevClient:
    def __init__(self, api_key: str = config.JEV_API_KEY, url: str = config.JEV_URL,
                 model: str = config.JEV_MODEL, http: httpx.Client | None = None):
        self.api_key = api_key
        self.url = url
        self.model = model
        self.http = http or httpx.Client(timeout=30)

    def ask(self, state: str | list, questions: dict) -> dict:
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if "openrouter.ai" in self.url:
            # OpenRouter's optional app attribution headers; requests show up as "mkplace" in its dashboard.
            headers.update({"HTTP-Referer": "https://github.com/mogmyij/mkplace", "X-Title": "mkplace"})
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
        kind, deal = a["kind"]["choice"], a["deal"]["choice"]
        return result(
            kind=kind,
            kind_probs=a["kind"].get("probabilities") or {kind: 1.0},
            relevance=float(a["relevance"]["score"]),
            deal=deal,
            deal_probs=a["deal"].get("probabilities") or {deal: 1.0},
            worth=float(a["worth"]["noul"]),
            source=data.get("model", self.model),
            raw=a,
            # OpenRouter reports the USD cost of each request; TypeSafe direct doesn't.
            cost=float((data.get("usage") or {}).get("cost") or 0),
        )


    def check_photos(self, search: dict, listing: dict, photos: list[tuple[bytes, str]]) -> dict:
        """Ask the photo questions about every photo at once. Images are only accepted via OpenRouter."""
        state: list = [
            "Facebook Marketplace listing and its photos.\n" + build_state(search, listing),
        ]
        for i, (image, mime) in enumerate(photos, 1):
            state.append(f"Photo {i} of {len(photos)}:")
            state.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}})
        data = self.ask(state, PHOTO_QUESTIONS)
        a = data["answers"]
        return {
            "photo_worth": float(a["worth"]["noul"]),
            "photo_genuine": float(a["genuine"]["noul"]),
            "photo_condition": float(a["condition"]["score"]),
            "cost": float((data.get("usage") or {}).get("cost") or 0),
        }


# (kind, where to look, pattern). Wanted/service wording is only trusted in the title; descriptions
# of genuine items often say things like "great for anyone looking for...".
JUNK_PATTERNS = [
    ("wanted", "title", r"\b(wanted|wtb|iso|in search of|looking for|we buy|buying|cash for)\b"),
    ("service", "title", r"\b(for rent|rental|to rent|hire|lessons?|tutor|repairs?|removals?|cleaning|services?|room for)\b"),
    ("business_ad", "text", r"\b(finance available|financing|weekly payments?|afterpay|in stock|many available|"
                    r"wholesale|dealer|showroom|all sizes available|bulk)\b"),
    ("scam", "text", r"\b(whats ?app|text me|e-?mail me|shipping only|gift ?cards?|deposit to hold)\b"),
]
BAIT_PRICES = {0.0, 1.0, 12.0, 123.0, 1234.0, 12345.0, 111.0, 1111.0, 999999.0}


def heuristic_classify(search: dict, listing: dict) -> dict:
    """Pattern matching + keyword overlap, used when no Jev API key is configured."""
    title = (listing.get("title") or "").lower()
    text = f"{title}\n{listing.get('description') or ''}".lower()
    price, max_price = listing.get("price"), search.get("max_price")

    kind = "genuine"
    for k, scope, pattern in JUNK_PATTERNS:
        if re.search(pattern, title if scope == "title" else text):
            kind = k
            break
    if kind == "genuine" and price in BAIT_PRICES and (listing.get("price_text") or "").lower() != "free":
        kind = "bait_price"

    words = {w for w in re.findall(r"\w+", search["keywords"].lower()) if len(w) > 1}
    overlap = sum(1 for w in words if w in text) / max(1, len(words))
    if price is None:
        deal = "unclear"
    elif max_price and price > max_price:
        deal = "overpriced"
    else:
        deal = "fair"
    worth = overlap * (0.3 if deal == "overpriced" else 1.0)
    genuine = 0.9 if kind == "genuine" else 0.15
    return result(kind, {"genuine": genuine}, 3 * overlap, deal, {deal: 1.0}, worth, "heuristic")


def get_photo_checker():
    """Photo checks need Jev's image input, which only OpenRouter offers. None when unavailable."""
    if config.JEV_PROVIDER != "OpenRouter" or not config.JEV_API_KEY:
        return None
    return JevClient().check_photos


def get_classifier():
    if config.JEV_API_KEY:
        client = JevClient()

        def classify(search, listing):
            try:
                return client.classify(search, listing)
            except Exception as e:  # keep crawling even if Jev is down
                log.warning("Jev failed for %s (%s); using heuristic", listing.get("id"), e)
                return heuristic_classify(search, listing)

        return classify
    return heuristic_classify
