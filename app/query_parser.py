"""Turn a natural-language request into Marketplace search keywords and price bounds.

Jev only classifies (it doesn't generate text), so extraction here is rule-based. The full
request is still passed to Jev at classification time, so nuance like "must have 16GB RAM"
or "no water damage" is judged per listing rather than lost.
"""
import re

_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?"

_RANGE = re.compile(rf"(?:between\s+)?{_NUM}\s*(?:-|to|and|–)\s*{_NUM}", re.I)
_MAX = re.compile(rf"(?:under|below|less than|max(?:imum)?|up to|no more than|<|budget(?: of| is)?)\s*{_NUM}", re.I)
_MIN = re.compile(rf"(?:over|above|more than|at least|min(?:imum)?|>)\s*{_NUM}", re.I)

STOPWORDS = {
    "i", "im", "i'm", "want", "wanna", "looking", "look", "for", "need", "find", "me", "a", "an",
    "the", "some", "any", "please", "that", "is", "are", "in", "on", "my", "to", "get", "buy",
    "searching", "search", "would", "like", "something", "around", "cheap", "dollars", "bucks",
    "budget", "price", "priced", "and", "or", "of", "it", "good", "great", "decent", "condition",
    "ideally", "preferably", "near", "nearby", "local", "used", "secondhand", "second-hand",
}


def _num(value: str, k: str | None) -> float:
    n = float(value.replace(",", ""))
    return n * 1000 if k else n


def parse_query(text: str) -> dict:
    remaining = text
    min_price = max_price = None

    # Only treat a range as a price if it looks like money (has $ or a price word nearby).
    m = _RANGE.search(remaining)
    if m and ("$" in m.group(0) or re.search(r"between|budget|price|\$", text, re.I)):
        lo, hi = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))
        min_price, max_price = min(lo, hi), max(lo, hi)
        remaining = remaining.replace(m.group(0), " ")
    else:
        if m_max := _MAX.search(remaining):
            max_price = _num(m_max.group(1), m_max.group(2))
            remaining = remaining.replace(m_max.group(0), " ")
        if m_min := _MIN.search(remaining):
            min_price = _num(m_min.group(1), m_min.group(2))
            remaining = remaining.replace(m_min.group(0), " ")

    # Drop clauses that describe preferences rather than the item ("that works", "with no scratches").
    remaining = re.split(r"[,.;!?]|\b(?:that|which|with|without|ideally|preferably|must|should|but)\b", remaining, maxsplit=1, flags=re.I)[0]
    words = [w for w in re.findall(r"[\w'+\-/]+", remaining) if w.lower() not in STOPWORDS]
    keywords = " ".join(words).strip() or text.strip()

    return {"keywords": keywords, "min_price": min_price, "max_price": max_price}
