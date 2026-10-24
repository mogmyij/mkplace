"""Playwright crawler for Facebook Marketplace search results.

Uses cookies exported from a logged-in browser. Results are parsed from the rendered listing
cards (anchors to /marketplace/item/<id>/), which has proven steadier than FB's private GraphQL.
"""
import json
import logging
import random
import re
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from . import config

log = logging.getLogger(__name__)

ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
PRICE_RE = re.compile(r"^(?:free|[A-Z]{0,3}\s?[$£€¥₹]\s?\d[\d,.]*\s*[kK]?|\d[\d,.]*\s?(?:[A-Z]{3}|[$£€]))$", re.I)
SAMESITE = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax", "strict": "Strict", "none": "None"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class CrawlError(Exception):
    pass


def load_cookies(path=config.FB_COOKIES_FILE) -> list[dict]:
    """Accept Playwright storage-state, a plain cookie list, or a Cookie-Editor export."""
    if not path.exists():
        raise CrawlError(f"Cookie file not found at {path}. Export your facebook.com cookies there.")
    data = json.loads(path.read_text())
    raw = data.get("cookies", []) if isinstance(data, dict) else data
    cookies = []
    for c in raw:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
        }
        if exp := c.get("expires") or c.get("expirationDate"):
            cookie["expires"] = float(exp)
        cookies.append(cookie)
    return cookies


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    if text.strip().lower() == "free":
        return 0.0
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return None
    value = float(m.group(0).replace(",", ""))
    return value * 1000 if re.search(r"\d\s*k\b", text, re.I) else value


def parse_card(href: str, text: str, image_url: str | None) -> dict | None:
    m = ITEM_RE.search(href)
    if not m:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    prices = [ln for ln in lines if PRICE_RE.match(ln)]
    rest = [ln for ln in lines if ln not in prices]
    if not rest:
        return None
    # Discounted cards show the current price first and the struck-through old one second.
    price_text = prices[0] if prices else None
    return {
        "id": m.group(1),
        "title": rest[0],
        "price": parse_price(price_text),
        "price_text": price_text,
        "location": rest[1] if len(rest) > 1 else None,
        "image_url": image_url,
        "url": f"https://www.facebook.com/marketplace/item/{m.group(1)}/",
    }


def search_url(search: dict) -> str:
    params = {"query": search["keywords"], "sortBy": "creation_time_descend", "exact": "false"}
    if search.get("min_price") is not None:
        params["minPrice"] = int(search["min_price"])
    if search.get("max_price") is not None:
        params["maxPrice"] = int(search["max_price"])
    if search.get("radius_km"):
        params["radius"] = int(search["radius_km"])
    location = search["location"].strip().strip("/") or "search"
    return f"https://www.facebook.com/marketplace/{location}/search/?{urlencode(params)}"


CARD_JS = """
() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).map(a => ({
    href: a.getAttribute('href'),
    text: a.innerText,
    img: (a.querySelector('img') || {}).src || null,
}))
"""

DETAIL_JS = """
() => {
    const meta = n => (document.querySelector(`meta[property="${n}"], meta[name="${n}"]`) || {}).content || '';
    let desc = meta('og:description') || meta('description');
    // Logged-in item pages render the description in a span after a "Details"/"Description" heading.
    const heads = Array.from(document.querySelectorAll('span, h2')).filter(e => /^(Details|Description)$/.test(e.innerText.trim()));
    for (const h of heads) {
        const block = h.closest('div')?.parentElement?.innerText || '';
        const text = block.replace(/^(Details|Description)\\s*/, '').trim();
        if (text.length > desc.length) desc = text;
    }
    return desc.slice(0, 4000);
}
"""


class MarketplaceCrawler:
    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=config.HEADLESS)
        self.context = self.browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900}, locale="en-US")
        self.context.add_cookies(load_cookies())
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc):
        self.context.close()
        self.browser.close()
        self._pw.stop()

    def _pause(self, lo=1.2, hi=3.0):
        self.page.wait_for_timeout(random.uniform(lo, hi) * 1000)

    def search(self, search: dict) -> list[dict]:
        url = search_url(search)
        log.info("Crawling %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if "/login" in self.page.url or self.page.locator('input[name="pass"]').count():
            raise CrawlError("Facebook asked to log in - cookies are missing or expired. Re-export them.")
        if "/checkpoint" in self.page.url:
            raise CrawlError("Facebook showed a security checkpoint. Log in from a browser and clear it, then re-export cookies.")
        try:
            self.page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=20000)
        except Exception:
            return []  # genuinely no results, or the page layout changed

        found: dict[str, dict] = {}
        for _ in range(config.SCROLL_ROUNDS):
            for card in self.page.evaluate(CARD_JS):
                item = parse_card(card["href"] or "", card["text"] or "", card["img"])
                if item:
                    found.setdefault(item["id"], item)
            self.page.mouse.wheel(0, random.randint(2200, 3200))
            self._pause()
        return list(found.values())

    def fetch_image(self, url: str) -> tuple[bytes, str]:
        """Download a listing photo through the logged-in browser context (FB image URLs are signed)."""
        resp = self.context.request.get(url, timeout=30000)
        if not resp.ok:
            raise CrawlError(f"Image download failed: HTTP {resp.status}")
        body = resp.body()
        if len(body) > 4_000_000:
            raise CrawlError("Image too large")
        return body, resp.headers.get("content-type", "image/jpeg").split(";")[0]

    def description(self, url: str) -> str:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._pause(1.5, 3.5)
            return self.page.evaluate(DETAIL_JS)
        except Exception as e:
            log.warning("Detail fetch failed for %s: %s", url, e)
            return ""
