import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    keywords TEXT NOT NULL,
    location TEXT NOT NULL,
    radius_km INTEGER NOT NULL DEFAULT 40,
    min_price REAL,
    max_price REAL,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    run_requested INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    price REAL,
    price_text TEXT,
    location TEXT,
    image_url TEXT,
    url TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_searches (
    listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    ai_score REAL,
    ai_reason TEXT,
    ai_source TEXT,
    kind TEXT,
    deal TEXT,
    junk_prob REAL,
    ai_raw TEXT,
    classified_at TEXT,
    PRIMARY KEY (listing_id, search_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_ls_search ON listing_searches(search_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with session() as conn:
        conn.executescript(SCHEMA)
        # Databases created before listing kinds existed: add the columns and re-score.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(listing_searches)")}
        if "junk_prob" not in cols:
            conn.execute("ALTER TABLE listing_searches ADD COLUMN kind TEXT")
            conn.execute("ALTER TABLE listing_searches ADD COLUMN junk_prob REAL")
            conn.execute("UPDATE listing_searches SET classified_at=NULL")


def set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def upsert_listing(conn, search_id: int, item: dict) -> bool:
    """Insert or refresh a listing and link it to a search. Returns True if new to this search."""
    ts = now()
    conn.execute(
        """
        INSERT INTO listings(id, title, price, price_text, location, image_url, url, first_seen_at, last_seen_at)
        VALUES(:id, :title, :price, :price_text, :location, :image_url, :url, :ts, :ts)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, price=excluded.price, price_text=excluded.price_text,
            location=excluded.location, image_url=COALESCE(excluded.image_url, listings.image_url),
            last_seen_at=excluded.last_seen_at
        """,
        {**item, "ts": ts},
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO listing_searches(listing_id, search_id, first_seen_at) VALUES(?, ?, ?)",
        (item["id"], search_id, ts),
    )
    return cur.rowcount == 1
