import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, settings, worker
from .crawler import CrawlError, load_cookies
from .query_parser import parse_query

STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
db.init()


@asynccontextmanager
async def lifespan(app):
    scheduler = None
    if config.RUN_WORKER:
        scheduler = BackgroundScheduler()
        worker.add_jobs(scheduler)
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="mkplace", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.APP_PASSWORD and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mkplace"'})
    return await call_next(request)


class SearchIn(BaseModel):
    name: str | None = None
    query: str = Field(min_length=2)
    keywords: str | None = None
    location: str = Field(min_length=1)
    radius_km: int = Field(40, ge=1, le=500)
    min_price: float | None = None
    max_price: float | None = None
    interval_minutes: int | None = Field(None, ge=5, le=10080)
    enabled: bool = True


class ParseIn(BaseModel):
    query: str


class SettingsIn(BaseModel):
    paused: bool = False
    active_start_hour: int | None = Field(None, ge=0, le=23)
    active_end_hour: int | None = Field(None, ge=0, le=23)
    default_interval_minutes: int = Field(30, ge=5, le=10080)


class ListingPatch(BaseModel):
    status: Literal["new", "seen", "starred", "hidden"]


def _search_values(body: SearchIn) -> dict:
    parsed = parse_query(body.query)
    if body.interval_minutes is None:
        with db.session() as conn:
            body.interval_minutes = settings.load(conn)["default_interval_minutes"]
    return {
        "name": body.name or (body.keywords or parsed["keywords"])[:60],
        "query": body.query,
        "keywords": body.keywords or parsed["keywords"],
        "location": body.location,
        "radius_km": body.radius_km,
        "min_price": body.min_price if body.min_price is not None else parsed["min_price"],
        "max_price": body.max_price if body.max_price is not None else parsed["max_price"],
        "interval_minutes": body.interval_minutes,
        "enabled": int(body.enabled),
    }


@app.post("/api/parse")
def parse(body: ParseIn):
    return parse_query(body.query)


@app.get("/api/searches")
def list_searches():
    with db.session() as conn:
        rows = conn.execute(
            """SELECT s.*, COUNT(ls.listing_id) AS listing_count,
                      SUM(CASE WHEN l.status='new' THEN 1 ELSE 0 END) AS new_count
               FROM searches s
               LEFT JOIN listing_searches ls ON ls.search_id = s.id
               LEFT JOIN listings l ON l.id = ls.listing_id
               GROUP BY s.id ORDER BY s.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/searches", status_code=201)
def create_search(body: SearchIn):
    values = {**_search_values(body), "created_at": db.now()}
    with db.session() as conn:
        cur = conn.execute(
            f"INSERT INTO searches({','.join(values)}, run_requested) VALUES({','.join(':' + k for k in values)}, 1)",
            values,
        )
        return dict(conn.execute("SELECT * FROM searches WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/searches/{search_id}")
def update_search(search_id: int, body: SearchIn):
    values = _search_values(body)
    with db.session() as conn:
        old = conn.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
        if not old:
            raise HTTPException(404)
        conn.execute(
            f"UPDATE searches SET {', '.join(f'{k}=:{k}' for k in values)} WHERE id=:id",
            {**values, "id": search_id},
        )
        # A changed request means old Jev scores answered a different question.
        if old["query"] != values["query"] or old["min_price"] != values["min_price"] or old["max_price"] != values["max_price"]:
            conn.execute("UPDATE listing_searches SET classified_at=NULL WHERE search_id=?", (search_id,))
            conn.execute("UPDATE searches SET run_requested=1 WHERE id=?", (search_id,))
        return dict(conn.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone())


@app.delete("/api/searches/{search_id}", status_code=204)
def delete_search(search_id: int):
    with db.session() as conn:
        conn.execute("DELETE FROM searches WHERE id=?", (search_id,))
        conn.execute("DELETE FROM listings WHERE id NOT IN (SELECT listing_id FROM listing_searches)")


@app.post("/api/searches/{search_id}/run", status_code=202)
def run_now(search_id: int):
    with db.session() as conn:
        conn.execute("UPDATE searches SET run_requested=1 WHERE id=?", (search_id,))
    return {"queued": True}


SORTS = {
    "score": "ai_score DESC NULLS LAST, first_seen_at DESC",
    "newest": "first_seen_at DESC",
    "price_asc": "price IS NULL, price ASC",
    "price_desc": "price DESC NULLS LAST",
}


@app.get("/api/listings")
def list_listings(
    search_id: int | None = None,
    q: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_score: float = 0,
    deal: str | None = None,  # comma separated
    view: Literal["active", "new", "starred", "hidden", "all"] = "active",
    kind: str | None = None,  # comma separated; unclassified listings count as genuine
    since_hours: int | None = None,
    sort: Literal["score", "newest", "price_asc", "price_desc"] = "score",
    limit: int = 60,
    offset: int = 0,
):
    where, params = ["1=1"], {"sid": search_id}
    if search_id:
        where.append("ls.search_id = :search_id")
        params["search_id"] = search_id
    if q:
        where.append("(l.title LIKE :q OR l.description LIKE :q OR l.location LIKE :q)")
        params["q"] = f"%{q}%"
    if min_price is not None:
        where.append("l.price >= :min_price")
        params["min_price"] = min_price
    if max_price is not None:
        where.append("l.price <= :max_price")
        params["max_price"] = max_price
    if since_hours:
        cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
        where.append("l.first_seen_at >= :cutoff")
        params["cutoff"] = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    if view == "active":
        where.append("l.status != 'hidden'")
    elif view != "all":
        where.append("l.status = :view")
        params["view"] = view

    having = ["COALESCE(MAX(ls.ai_score), 0) >= :min_score"]
    params["min_score"] = min_score
    for name, csv, expr in (("d", deal, "deal"), ("k", kind, "COALESCE(kind, 'genuine')")):
        values = [v for v in (csv or "").split(",") if v]
        if values:
            having.append(f"{expr} IN ({','.join(f':{name}{i}' for i in range(len(values)))})")
            params.update({f"{name}{i}": v for i, v in enumerate(values)})

    # One row per listing; when it matches several searches, report its best score.
    sql = f"""
        SELECT l.*, MAX(ls.ai_score) AS ai_score, MIN(ls.junk_prob) AS junk_prob,
               (SELECT ai_reason FROM listing_searches x WHERE x.listing_id=l.id AND (:sid IS NULL OR x.search_id=:sid) ORDER BY x.ai_score DESC LIMIT 1) AS ai_reason,
               (SELECT kind FROM listing_searches x WHERE x.listing_id=l.id AND (:sid IS NULL OR x.search_id=:sid) ORDER BY x.ai_score DESC LIMIT 1) AS kind,
               (SELECT deal FROM listing_searches x WHERE x.listing_id=l.id AND (:sid IS NULL OR x.search_id=:sid) ORDER BY x.ai_score DESC LIMIT 1) AS deal,
               (SELECT ai_source FROM listing_searches x WHERE x.listing_id=l.id AND (:sid IS NULL OR x.search_id=:sid) ORDER BY x.ai_score DESC LIMIT 1) AS ai_source,
               MIN(ls.first_seen_at) AS first_seen_at,
               GROUP_CONCAT(ls.search_id) AS search_ids
        FROM listings l JOIN listing_searches ls ON ls.listing_id = l.id
        WHERE {' AND '.join(where)}
        GROUP BY l.id
        HAVING {' AND '.join(having)}
    """
    with db.session() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows = conn.execute(f"{sql} ORDER BY {SORTS[sort]} LIMIT :limit OFFSET :offset",
                            {**params, "limit": min(limit, 200), "offset": offset}).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.patch("/api/listings/{listing_id}")
def patch_listing(listing_id: str, body: ListingPatch):
    with db.session() as conn:
        cur = conn.execute("UPDATE listings SET status=? WHERE id=?", (body.status, listing_id))
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    with db.session() as conn:
        return settings.load(conn)


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    with db.session() as conn:
        return settings.save(conn, body.model_dump())


@app.put("/api/cookies")
async def put_cookies(request: Request):
    """Replace the Facebook cookie file (paste of a Cookie-Editor JSON export)."""
    raw = await request.body()
    tmp = config.FB_COOKIES_FILE.with_suffix(".tmp")
    try:
        data = json.loads(raw)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        names = {c["name"] for c in load_cookies(tmp)}
    except (ValueError, KeyError, TypeError, CrawlError) as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"Not a cookie export: {e}")
    if not {"c_user", "xs"} <= names:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "Export has no c_user/xs cookies - make sure you're logged in to facebook.com when exporting.")
    tmp.replace(config.FB_COOKIES_FILE)
    return {"ok": True, "count": len(names)}


@app.get("/api/status")
def status():
    with db.session() as conn:
        heartbeat = db.get_meta(conn, "worker_heartbeat")
        current = settings.load(conn)
    alive = False
    if heartbeat:
        alive = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds() < 300
    return {
        "worker_heartbeat": heartbeat,
        "worker_alive": alive,
        "classifier": f"Jev ({config.JEV_MODEL})" if config.TYPESAFE_API_KEY else "Heuristic (no TYPESAFE_API_KEY)",
        "cookies_present": config.FB_COOKIES_FILE.exists(),
        "paused": current["paused"],
        "in_active_hours": settings.in_active_hours(current),
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
