import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "JEV_API_KEY", "")
    from app import db, pipeline, web
    importlib.reload(web)
    db.init()
    return TestClient(web.app), pipeline, db


class FakeCrawler:
    def __init__(self, items):
        self.items = items
        self.opened = []

    def search(self, search):
        return self.items

    def details(self, url):
        self.opened.append(url)
        lid = url.rstrip("/").rsplit("/", 1)[-1]
        return {"description": "Barely used, comes with two controllers",
                "photos": [f"https://img/{lid}-a.jpg", f"https://img/{lid}-b.jpg"]}


ITEMS = [
    {"id": "1", "title": "PS5 disc edition", "price": 300.0, "price_text": "$300", "location": "Sydney",
     "image_url": None, "url": "https://www.facebook.com/marketplace/item/1/"},
    {"id": "2", "title": "Xbox controller", "price": 40.0, "price_text": "$40", "location": "Sydney",
     "image_url": None, "url": "https://www.facebook.com/marketplace/item/2/"},
    {"id": "3", "title": "PS5 bundle", "price": 1.0, "price_text": "$1", "location": "Sydney",
     "image_url": None, "url": "https://www.facebook.com/marketplace/item/3/"},
    {"id": "4", "title": "WANTED PS5", "price": 300.0, "price_text": "$300", "location": "Sydney",
     "image_url": None, "url": "https://www.facebook.com/marketplace/item/4/"},
]


def test_end_to_end(env):
    client, pipeline, db = env
    r = client.post("/api/searches", json={"query": "looking for a PS5 under $350", "location": "sydney"})
    assert r.status_code == 201
    s = r.json()
    assert (s["keywords"], s["max_price"], s["run_requested"]) == ("PS5", 350, 1)

    with db.session() as conn:
        [due] = pipeline.due_searches(conn)
    crawler = FakeCrawler(ITEMS)
    summary = pipeline.run_search(crawler, due, pipeline.get_classifier())
    assert summary == "4 found, 4 new, 4 classified"
    assert len(crawler.opened) == 4

    # Second run: nothing new, nothing re-classified, no detail pages re-opened.
    assert pipeline.run_search(crawler, due, pipeline.get_classifier()) == "4 found, 0 new, 0 classified"
    assert len(crawler.opened) == 4

    genuine = client.get("/api/listings?kind=genuine").json()["items"]
    assert [i["id"] for i in genuine][0] == "1"  # best match first
    assert {i["id"] for i in genuine} == {"1", "2"}  # $1 bait and the wanted post filtered out
    junk = client.get("/api/listings?kind=wanted,bait_price").json()["items"]
    assert {(i["id"], i["kind"]) for i in junk} == {("3", "bait_price"), ("4", "wanted")}
    assert client.get("/api/listings").json()["total"] == 4

    assert client.patch("/api/listings/2", json={"status": "hidden"}).json() == {"ok": True}
    assert [i["id"] for i in client.get("/api/listings?kind=genuine").json()["items"]] == ["1"]
    assert [i["id"] for i in client.get("/api/listings?view=hidden").json()["items"]] == ["2"]
    assert client.get("/api/listings?sort=price_asc&view=all").json()["items"][0]["id"] == "3"
    assert client.get("/api/listings?min_score=95").json()["total"] == 0


def test_editing_query_requeues_classification(env):
    client, pipeline, db = env
    s = client.post("/api/searches", json={"query": "PS5 under $350", "location": "sydney"}).json()
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)
    pipeline.run_search(FakeCrawler(ITEMS), due, pipeline.get_classifier())
    pipeline._record(s["id"], "ok", None)
    with db.session() as conn:
        assert pipeline.due_searches(conn) == []

    client.put(f"/api/searches/{s['id']}", json={"query": "PS5 under $250", "location": "sydney"})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing_searches WHERE classified_at IS NULL").fetchone()[0] == len(ITEMS)
        assert len(pipeline.due_searches(conn)) == 1


def test_delete_search_removes_orphans(env):
    client, pipeline, db = env
    s = client.post("/api/searches", json={"query": "PS5", "location": "sydney"}).json()
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)
    pipeline.run_search(FakeCrawler(ITEMS), due, pipeline.get_classifier())
    assert client.delete(f"/api/searches/{s['id']}").status_code == 204
    assert client.get("/api/listings?view=all").json()["total"] == 0


def test_password(env, monkeypatch):
    client, *_ = env
    from app import config
    monkeypatch.setattr(config, "APP_PASSWORD", "pw")
    assert client.get("/api/searches").status_code == 401
    assert client.get("/api/searches", auth=("admin", "pw")).status_code == 200


def test_settings_pause_and_active_hours(env):
    from datetime import datetime
    client, pipeline, db = env
    assert client.put("/api/settings", json={"paused": False, "active_start_hour": 22, "active_end_hour": 6,
                                              "default_interval_minutes": 45}).status_code == 200
    s = client.post("/api/searches", json={"query": "PS5", "location": "sydney"}).json()
    assert s["interval_minutes"] == 45
    pipeline._record(s["id"], "ok", None)  # clears run_requested; last run long enough ago below
    with db.session() as conn:
        conn.execute("UPDATE searches SET last_run_at='2000-01-01T00:00:00+00:00'")
        assert len(pipeline.due_searches(conn, datetime(2026, 1, 1, 23))) == 1  # inside 22-6 window
        assert pipeline.due_searches(conn, datetime(2026, 1, 1, 12)) == []    # outside window

    client.put("/api/settings", json={"paused": True, "default_interval_minutes": 45})
    with db.session() as conn:
        assert pipeline.due_searches(conn, datetime(2026, 1, 1, 12)) == []
    client.post(f"/api/searches/{s['id']}/run")  # manual runs ignore pause
    with db.session() as conn:
        assert len(pipeline.due_searches(conn, datetime(2026, 1, 1, 12))) == 1


def test_custom_interval(env):
    client, *_ = env
    r = client.post("/api/searches", json={"query": "PS5", "location": "sydney", "interval_minutes": 7})
    assert r.json()["interval_minutes"] == 7
    assert client.post("/api/searches", json={"query": "PS5", "location": "x", "interval_minutes": 2}).status_code == 422


def test_cookie_upload(env, tmp_path, monkeypatch):
    client, *_ = env
    from app import config
    monkeypatch.setattr(config, "FB_COOKIES_FILE", tmp_path / "cookies.json")
    bad = client.put("/api/cookies", content='[{"name": "datr", "value": "x"}]')
    assert bad.status_code == 400 and not (tmp_path / "cookies.json").exists()
    assert client.put("/api/cookies", content="not json").status_code == 400
    good = client.put("/api/cookies", content='[{"name": "c_user", "value": "1"}, {"name": "xs", "value": "2"}]')
    assert good.json() == {"ok": True, "count": 2}
    assert (tmp_path / "cookies.json").exists()


def test_jev_cost_is_accumulated(env):
    client, pipeline, db = env
    client.post("/api/searches", json={"query": "PS5", "location": "sydney"})
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)

    def classify(search, listing):
        return {**pipeline.get_classifier()(search, listing), "cost": 0.001}

    pipeline.run_search(FakeCrawler(ITEMS), due, classify)
    assert client.get("/api/status").json()["jev_cost_usd"] == pytest.approx(0.001 * len(ITEMS))


class PhotoCrawler(FakeCrawler):
    def __init__(self, items, broken=()):
        super().__init__(items)
        self.broken, self.fetched = set(broken), []

    def fetch_image(self, url):
        self.fetched.append(url)
        if any(b in url for b in self.broken):
            raise RuntimeError("HTTP 403")
        return url.encode(), "image/jpeg"


def test_photo_check_sends_all_photos_and_lowers_scores(env, monkeypatch):
    client, pipeline, db = env
    from app import config
    monkeypatch.setattr(config, "DETAIL_FETCH_LIMIT", 1)  # listing 5's page is only opened by the photo step
    items = [{**ITEMS[0], "image_url": "https://img/1-thumb.jpg"},
             {**ITEMS[1], "id": "5", "title": "PS5 slim", "image_url": "https://img/5-thumb.jpg",
              "url": "https://www.facebook.com/marketplace/item/5/"},
             {**ITEMS[1], "id": "6", "title": "PS5 pro", "image_url": "https://img/6-thumb.jpg",
              "url": "https://www.facebook.com/marketplace/item/6/"},
             {**ITEMS[3], "image_url": "https://img/4.jpg"}]  # wanted post: never photo-checked
    client.post("/api/searches", json={"query": "PS5 under $350", "location": "sydney"})
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)

    sent = {}

    def check(search, listing, images):
        sent[listing["id"]] = [img for img, _ in images]
        ok = listing["id"] != "1"
        return {"photo_worth": 0.9 if ok else 0.1, "photo_genuine": 0.95 if ok else 0.2,
                "photo_condition": 2.2, "cost": 0.0005}

    crawler = PhotoCrawler(items, broken=["6-"])
    summary = pipeline.run_search(crawler, due, pipeline.get_classifier(), check)
    assert summary.endswith("2 photo sets checked")
    # Every gallery photo goes to Jev in one request, including for a listing whose page the photo
    # step had to open itself. Listing 6's photos all failed to download: no request, no penalty.
    assert sent == {"1": [b"https://img/1-a.jpg", b"https://img/1-b.jpg"],
                    "5": [b"https://img/5-a.jpg", b"https://img/5-b.jpg"]}

    with db.session() as conn:
        rows = {r["listing_id"]: dict(r) for r in conn.execute("SELECT * FROM listing_searches")}
    bad, good = rows["1"], rows["5"]
    assert bad["ai_score"] == pytest.approx(bad["text_score"] * (1 - 0.6 * 0.8), abs=0.1)  # worst of the two "no"s
    assert bad["ai_reason"].endswith("· Photos not convincing · Not the seller's own photos · Condition: Good")
    assert good["ai_score"] == good["text_score"]
    assert good["ai_reason"].endswith("· Condition: Good")
    assert rows["6"]["photo_checked_at"] and rows["6"]["ai_score"] == rows["6"]["text_score"]
    assert rows["4"]["photo_checked_at"] is None

    ids = lambda q: {i["id"] for i in client.get("/api/listings?view=all" + q).json()["items"]}
    assert "1" in ids("") and "1" not in ids("&hide_photo_issues=true")
    assert ids("&min_condition=2") == {"1", "5"} and ids("&min_condition=3") == set()
    assert client.get("/api/listings?view=all").json()["items"][0]["photo_count"] == 2

    # Second run: nothing left to check. Turning the setting off skips the step.
    assert pipeline.run_search(crawler, due, pipeline.get_classifier(), check).endswith("0 photo sets checked")
    client.put("/api/settings", json={"photo_check": False})
    assert "photo" not in pipeline.run_search(crawler, due, pipeline.get_classifier(), check)
    assert client.get("/api/status").json()["jev_cost_usd"] == pytest.approx(0.001)


def test_editing_request_rechecks_photos(env):
    client, pipeline, db = env
    s = client.post("/api/searches", json={"query": "PS5 under $350", "location": "sydney"}).json()
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)
    check = lambda search, listing, images: {"photo_worth": 0.9, "photo_genuine": 0.9, "photo_condition": 3, "cost": 0}
    pipeline.run_search(PhotoCrawler(ITEMS), due, pipeline.get_classifier(), check)
    client.put(f"/api/searches/{s['id']}", json={"query": "PS5 for parts", "location": "sydney"})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing_searches WHERE photo_checked_at IS NOT NULL").fetchone()[0] == 0
