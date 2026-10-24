import logging
from datetime import datetime, timedelta, timezone

from . import config, db, settings
from .crawler import CrawlError, MarketplaceCrawler
from .jev import get_classifier, get_image_checker, image_factor

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
        apply_image(conn, row["id"])
        conn.commit()
    add_cost(conn, spent)
    return len(rows)


def apply_image(conn, listing_id: str) -> None:
    """Final score = text score x photo penalty (1.0 when the photo is fine or unchecked)."""
    listing = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    factor, flags = image_factor(dict(listing))
    for ls in conn.execute("SELECT search_id, text_score, text_reason FROM listing_searches "
                           "WHERE listing_id=? AND text_score IS NOT NULL", (listing_id,)).fetchall():
        conn.execute(
            "UPDATE listing_searches SET ai_score=?, ai_reason=? WHERE listing_id=? AND search_id=?",
            (round(ls["text_score"] * factor, 1), " · ".join([ls["text_reason"], *flags]), listing_id, ls["search_id"]),
        )


def check_images(conn, crawler, search: dict, check_image, min_score: float) -> int:
    """Run the photo check on this search's best genuine listings that haven't had one yet."""
    rows = conn.execute(
        """SELECT l.* FROM listings l JOIN listing_searches ls ON ls.listing_id = l.id
           WHERE ls.search_id=? AND l.image_checked_at IS NULL AND l.image_url IS NOT NULL
             AND ls.text_score >= ? AND COALESCE(ls.kind, 'genuine') = 'genuine'
           ORDER BY ls.text_score DESC LIMIT ?""",
        (search["id"], min_score, config.IMAGE_CHECK_LIMIT),
    ).fetchall()
    spent, checked = 0.0, 0
    for row in rows:
        try:
            image, mime = crawler.fetch_image(row["image_url"])
        except Exception as e:
            # Expired or missing photo: record it as checked (no penalty) so it isn't retried every run.
            log.warning("Photo download failed for %s: %s", row["id"], e)
            conn.execute("UPDATE listings SET image_checked_at=? WHERE id=?", (db.now(), row["id"]))
            continue
        try:
            res = check_image(dict(row), image, mime)
        except Exception as e:  # Jev hiccup: leave unchecked and retry next run
            log.warning("Jev photo check failed for %s: %s", row["id"], e)
            continue
        spent += res.pop("cost", 0)
        conn.execute(
            "UPDATE listings SET image_stock=:image_stock, image_damaged=:image_damaged, "
            "image_no_item=:image_no_item, image_checked_at=:ts WHERE id=:id",
            {**res, "ts": db.now(), "id": row["id"]},
        )
        apply_image(conn, row["id"])
        conn.commit()
        checked += 1
    add_cost(conn, spent)
    return checked


def add_cost(conn, usd: float) -> None:
    if usd:
        db.set_meta(conn, "jev_cost_usd", db.get_meta(conn, "jev_cost_usd", 0) + usd)


def run_search(crawler, search: dict, classify, check_image=None) -> str:
    items = crawler.search(search)
    with db.session() as conn:
        new_ids = [item["id"] for item in items if db.upsert_listing(conn, search["id"], item)]
        missing_desc = {r["id"] for r in conn.execute(
            f"SELECT id FROM listings WHERE description IS NULL AND id IN ({','.join('?' * len(new_ids))})", new_ids)}

    # Open the newest listings to read their descriptions; Jev judges much better with them.
    for lid in [i for i in new_ids if i in missing_desc][: config.DETAIL_FETCH_LIMIT]:
        desc = crawler.description(f"https://www.facebook.com/marketplace/item/{lid}/")
        with db.session() as conn:
            conn.execute("UPDATE listings SET description=? WHERE id=?", (desc, lid))

    with db.session() as conn:
        classified = classify_pending(conn, search, classify)
        summary = f"{len(items)} found, {len(new_ids)} new, {classified} classified"
        opts = settings.load(conn)
        if check_image and opts["image_check"]:
            photos = check_images(conn, crawler, search, check_image, opts["image_min_score"])
            summary += f", {photos} photos checked"
    return summary


def run_due() -> None:
    with db.session() as conn:
        db.set_meta(conn, "worker_heartbeat", db.now())
        searches = due_searches(conn)
    if not searches:
        return

    classify, check_image = get_classifier(), get_image_checker()
    done = set()
    try:
        with MarketplaceCrawler() as crawler:
            for search in searches:
                try:
                    status, error = run_search(crawler, search, classify, check_image), None
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
