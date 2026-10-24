import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    from app import db, pipeline, web
    importlib.reload(web)
    db.init()
    return TestClient(web.app), pipeline, db


class FakeCrawler:
    def __init__(self, items):
        self.items = items
        self.details = []

    def search(self, search):
        return self.items

    def description(self, url):
        self.details.append(url)
        return "Barely used, comes with two controllers"


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
    assert len(crawler.details) == 4

    # Second run: nothing new, nothing re-classified, no detail pages re-opened.
    assert pipeline.run_search(crawler, due, pipeline.get_classifier()) == "4 found, 0 new, 0 classified"
    assert len(crawler.details) == 4

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
