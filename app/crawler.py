"""Playwright crawler for Facebook Marketplace search results.

Uses cookies exported from a logged-in browser. Results are parsed from the rendered listing
cards (anchors to /marketplace/item/<id>/), which has proven steadier than FB's private GraphQL.
"""
import io
import json
import logging
import random
import re
from urllib.parse import urlencode, urlparse

from PIL import Image

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

# fbcdn photo URLs that aren't part of this listing's gallery: other listings ("similar items"),
# the seller's profile picture, and page chrome.
_NOT_GALLERY = 'a[href*="/marketplace/item/"], a[href*="/profile"], a[href*="/marketplace/profile"], [role="banner"], [role="navigation"], [role="complementary"]'

MAIN_PHOTO_JS = """
() => {
    // The photo currently open in the listing's viewer: the largest visible fbcdn image.
    let best = null, bestArea = 40000;
    for (const img of document.querySelectorAll('img')) {
        const src = img.currentSrc || img.src || '';
        if (!/fbcdn|scontent/.test(src) || img.closest('%s')) continue;
        const r = img.getBoundingClientRect();
        const area = r.width * r.height;
        if (r.bottom > 0 && r.top < innerHeight && area > bestArea) { best = src; bestArea = area; }
    }
    return best;
}
""" % _NOT_GALLERY

GALLERY_JS = """
() => [...document.querySelectorAll('img')]
    .filter(img => /fbcdn|scontent/.test(img.currentSrc || img.src || '') && !img.closest('%s'))
    .filter(img => Math.max(img.naturalWidth, img.naturalHeight) >= 60)
    .map(img => img.currentSrc || img.src)
""" % _NOT_GALLERY


def photo_key(url: str) -> str:
    """FB serves the same photo at several sizes; the file name identifies the photo itself."""
    return urlparse(url).path.rsplit("/", 1)[-1]


def shrink(image: bytes, max_side: int = 1024) -> bytes:
    """Re-encode as a JPEG no larger than max_side, to keep Jev requests small."""
    with Image.open(io.BytesIO(image)) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue()


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
        """Download a listing photo through the logged-in browser context and shrink it to a JPEG."""
        resp = self.context.request.get(url, timeout=30000)
        if not resp.ok:
            raise CrawlError(f"Image download failed: HTTP {resp.status}")
        body = resp.body()
        if len(body) > 15_000_000:
            raise CrawlError("Image too large")
        return shrink(body, config.PHOTO_MAX_SIDE), "image/jpeg"

    def details(self, url: str) -> dict:
        """Open a listing and return its description and the URLs of all its photos."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._pause(1.5, 3.5)
            description = self.page.evaluate(DETAIL_JS)
        except Exception as e:
            log.warning("Detail fetch failed for %s: %s", url, e)
            return {"description": "", "photos": []}
        return {"description": description, "photos": self._gallery()}

    def _gallery(self) -> list[str]:
        # Step through the photo viewer so each photo is captured at full size...
        photos: dict[str, str] = {}
        start_url = self.page.url
        try:
            for _ in range(config.MAX_PHOTOS):
                src = self.page.evaluate(MAIN_PHOTO_JS)
                if not src or photo_key(src) in photos:
                    break
                photos[photo_key(src)] = src
                nxt = self.page.locator('[aria-label*="next" i]:visible').first
                if not nxt.count():
                    break
                nxt.click(timeout=3000)
                self.page.wait_for_timeout(random.uniform(500, 900))
                if self.page.url.split("?")[0] != start_url.split("?")[0]:
                    # That "next" went to another listing; keep only what we had.
                    log.warning("Photo viewer navigated away from %s", start_url)
                    return list(photos.values())
            # ...then any photos the viewer didn't reach: click their strip thumbnail to open them
            # full size, or fall back to the thumbnail itself.
            for src in self.page.evaluate(GALLERY_JS):
                if len(photos) >= config.MAX_PHOTOS:
                    break
                key = photo_key(src)
                if key in photos:
                    continue
                full = src
                try:
                    self.page.locator(f'img[src="{src}"]').first.click(timeout=2000)
                    self.page.wait_for_timeout(random.uniform(400, 700))
                    opened = self.page.evaluate(MAIN_PHOTO_JS)
                    if opened and photo_key(opened) == key:
                        full = opened
                except Exception:
                    pass
                if self.page.url.split("?")[0] != start_url.split("?")[0]:
                    log.warning("Photo strip navigated away from %s", start_url)
                    break
                photos[key] = full
        except Exception as e:
            log.warning("Photo gallery read failed: %s", e)
        return list(photos.values())
