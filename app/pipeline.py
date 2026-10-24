import logging
from datetime import datetime, timedelta, timezone

from . import config, db, settings
from .crawler import CrawlError, MarketplaceCrawler
from .jev import get_classifier

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
            """UPDATE listing_searches SET ai_score=:ai_score, ai_reason=:ai_reason, ai_source=:ai_source,
               kind=:kind, deal=:deal, junk_prob=:junk_prob, ai_raw=:ai_raw, classified_at=:ts
               WHERE listing_id=:lid AND search_id=:sid""",
            {**result, "ts": db.now(), "lid": row["id"], "sid": search["id"]},
        )
        conn.commit()
    if spent:
        db.set_meta(conn, "jev_cost_usd", db.get_meta(conn, "jev_cost_usd", 0) + spent)
    return len(rows)


def run_search(crawler, search: dict, classify) -> str:
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
    return f"{len(items)} found, {len(new_ids)} new, {classified} classified"


def run_due() -> None:
    with db.session() as conn:
        db.set_meta(conn, "worker_heartbeat", db.now())
        searches = due_searches(conn)
    if not searches:
        return

    classify = get_classifier()
    done = set()
    try:
        with MarketplaceCrawler() as crawler:
            for search in searches:
                try:
                    status, error = run_search(crawler, search, classify), None
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
