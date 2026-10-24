import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, db, settings
from .crawler import CrawlError, MarketplaceCrawler
from .jev import get_classifier, get_photo_checker, photo_factor

log = logging.getLogger(__name__)


def due_searches(conn, now_local: datetime | None = None) -> list[dict]:
    s = settings.load(conn)
    # Manual "run now" requests go through even when paused or outside active hours.
    scheduled_ok = not s["paused"] and settings.in_active_hours(s, now_local)
    rows = conn.execute("SELECT * FROM searches WHERE enabled=1 OR run_requested=1").fetchall()
    now = datetime.now(timezone.utc)
    due = []
    for r in rows:
        if r["run_requested"]:
            due.append(dict(r))
            continue
        last = datetime.fromisoformat(r["last_run_at"]) if r["last_run_at"] else None
        if scheduled_ok and r["enabled"] and (last is None or now - last >= timedelta(minutes=r["interval_minutes"])):
            due.append(dict(r))
    return due


def classify_pending(conn, search: dict, classify) -> int:
    rows = conn.execute(
        """SELECT l.* FROM listings l JOIN listing_searches ls ON ls.listing_id = l.id
           WHERE ls.search_id=? AND ls.classified_at IS NULL""",
        (search["id"],),
    ).fetchall()
    spent = 0.0
    for row in rows:
        result = classify(search, dict(row))
        spent += result.get("cost", 0)
        conn.execute(
            """UPDATE listing_searches SET text_score=:text_score, text_reason=:text_reason, ai_source=:ai_source,
               kind=:kind, deal=:deal, junk_prob=:junk_prob, ai_raw=:ai_raw, classified_at=:ts
               WHERE listing_id=:lid AND search_id=:sid""",
            {**result, "ts": db.now(), "lid": row["id"], "sid": search["id"]},
        )
        apply_scores(conn, row["id"], search["id"])
        conn.commit()
    add_cost(conn, spent)
    return len(rows)


def apply_scores(conn, listing_id: str, search_id: int) -> None:
    """Final score = text score x photo penalty (1.0 when the photos look fine or aren't checked yet)."""
    row = conn.execute("SELECT * FROM listing_searches WHERE listing_id=? AND search_id=?",
                       (listing_id, search_id)).fetchone()
    if row is None or row["text_score"] is None:
        return
    factor, labels = photo_factor(dict(row))
    conn.execute(
        "UPDATE listing_searches SET ai_score=?, ai_reason=? WHERE listing_id=? AND search_id=?",
        (round(row["text_score"] * factor, 1), " · ".join([row["text_reason"], *labels]), listing_id, search_id),
    )


def save_details(conn, listing_id: str, details: dict) -> None:
    conn.execute("UPDATE listings SET description=COALESCE(NULLIF(?, ''), description), photos=? WHERE id=?",
                 (details["description"], json.dumps(details["photos"]), listing_id))


def check_photos(conn, crawler, search: dict, check, min_score: float) -> int:
    """Send all photos of this search's best unchecked genuine listings to Jev."""
    rows = conn.execute(
        """SELECT l.* FROM listings l JOIN listing_searches ls ON ls.listing_id = l.id
           WHERE ls.search_id=? AND ls.photo_checked_at IS NULL
             AND ls.text_score >= ? AND COALESCE(ls.kind, 'genuine') = 'genuine'
           ORDER BY ls.text_score DESC LIMIT ?""",
        (search["id"], min_score, config.PHOTO_CHECK_LIMIT),
    ).fetchall()
    spent, checked = 0.0, 0
    for row in rows:
        listing = dict(row)
        if listing["photos"] is None:  # listing page not opened yet: get its description and gallery
            details = crawler.details(listing["url"])
            save_details(conn, listing["id"], details)
            listing["photos"] = json.dumps(details["photos"])
            listing["description"] = details["description"] or listing["description"]
        urls = json.loads(listing["photos"]) or ([listing["image_url"]] if listing["image_url"] else [])
        images = []
        for url in urls[: config.MAX_PHOTOS]:
            try:
                images.append(crawler.fetch_image(url))
            except Exception as e:
                log.warning("Photo download failed for %s: %s", listing["id"], e)
        if not images:
            # No usable photos (expired links): mark done with no penalty so it isn't retried every run.
            conn.execute("UPDATE listing_searches SET photo_checked_at=? WHERE listing_id=? AND search_id=?",
                         (db.now(), listing["id"], search["id"]))
            continue
        try:
            res = check(search, listing, images)
        except Exception as e:  # Jev hiccup: leave unchecked and retry next run
            log.warning("Jev photo check failed for %s: %s", listing["id"], e)
            continue
        spent += res.pop("cost", 0)
        conn.execute(
            """UPDATE listing_searches SET photo_worth=:photo_worth, photo_genuine=:photo_genuine,
               photo_condition=:photo_condition, photo_checked_at=:ts WHERE listing_id=:lid AND search_id=:sid""",
            {**res, "ts": db.now(), "lid": listing["id"], "sid": search["id"]},
        )
        apply_scores(conn, listing["id"], search["id"])
        conn.commit()
        checked += 1
    add_cost(conn, spent)
    return checked


def add_cost(conn, usd: float) -> None:
    if usd:
        db.set_meta(conn, "jev_cost_usd", db.get_meta(conn, "jev_cost_usd", 0) + usd)


def run_search(crawler, search: dict, classify, check_photo=None) -> str:
    items = crawler.search(search)
    with db.session() as conn:
        new_ids = [item["id"] for item in items if db.upsert_listing(conn, search["id"], item)]
        missing_desc = {r["id"] for r in conn.execute(
            f"SELECT id FROM listings WHERE description IS NULL AND id IN ({','.join('?' * len(new_ids))})", new_ids)}

    # Open the newest listings to read their descriptions and photo galleries; Jev judges much better with them.
    for lid in [i for i in new_ids if i in missing_desc][: config.DETAIL_FETCH_LIMIT]:
        details = crawler.details(f"https://www.facebook.com/marketplace/item/{lid}/")
        with db.session() as conn:
            save_details(conn, lid, details)

    with db.session() as conn:
        classified = classify_pending(conn, search, classify)
        summary = f"{len(items)} found, {len(new_ids)} new, {classified} classified"
        opts = settings.load(conn)
        if check_photo and opts["photo_check"]:
            checked = check_photos(conn, crawler, search, check_photo, opts["photo_min_score"])
            summary += f", {checked} photo sets checked"
    return summary


def run_due() -> None:
    with db.session() as conn:
        db.set_meta(conn, "worker_heartbeat", db.now())
        searches = due_searches(conn)
    if not searches:
        return

    classify, check_photo = get_classifier(), get_photo_checker()
    done = set()
    try:
        with MarketplaceCrawler() as crawler:
            for search in searches:
                try:
                    status, error = run_search(crawler, search, classify, check_photo), None
                except CrawlError as e:
                    status, error = "error", str(e)
                except Exception as e:
                    log.exception("Search %s failed", search["id"])
                    # Playwright errors carry a multi-line call log; the first line is the useful part.
                    status, error = "error", f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
                _record(search["id"], status, error)
                done.add(search["id"])
                log.info("Search %s (%s): %s %s", search["id"], search["name"], status, error or "")
    except Exception as e:  # browser failed to start or cookies unusable
        log.exception("Crawler setup failed")
        for search in searches:
            if search["id"] not in done:
                _record(search["id"], "error", str(e))


def _record(search_id: int, status: str, error: str | None) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE searches SET last_run_at=?, last_status=?, last_error=?, run_requested=0 WHERE id=?",
            (db.now(), status, error, search_id),
        )
