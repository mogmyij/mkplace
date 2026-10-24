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
    assert summary == "3 found, 3 new, 3 classified"
    assert len(crawler.details) == 3

    # Second run: nothing new, nothing re-classified, no detail pages re-opened.
    assert pipeline.run_search(crawler, due, pipeline.get_classifier()) == "3 found, 0 new, 0 classified"
    assert len(crawler.details) == 3

    items = client.get("/api/listings").json()["items"]
    ids = [i["id"] for i in items]
    assert ids[0] == "1"  # best match first
    assert "3" not in ids  # $1 listing hidden as a likely scam
    assert "3" in [i["id"] for i in client.get("/api/listings?hide_scams=false").json()["items"]]

    assert client.patch("/api/listings/2", json={"status": "hidden"}).json() == {"ok": True}
    assert [i["id"] for i in client.get("/api/listings").json()["items"]] == ["1"]
    assert [i["id"] for i in client.get("/api/listings?view=hidden").json()["items"]] == ["2"]
    assert client.get("/api/listings?sort=price_asc&view=all&hide_scams=false").json()["items"][0]["id"] == "3"
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
        assert conn.execute("SELECT COUNT(*) FROM listing_searches WHERE classified_at IS NULL").fetchone()[0] == 3
        assert len(pipeline.due_searches(conn)) == 1


def test_delete_search_removes_orphans(env):
    client, pipeline, db = env
    s = client.post("/api/searches", json={"query": "PS5", "location": "sydney"}).json()
    with db.session() as conn:
        [due] = pipeline.due_searches(conn)
    pipeline.run_search(FakeCrawler(ITEMS), due, pipeline.get_classifier())
    assert client.delete(f"/api/searches/{s['id']}").status_code == 204
    assert client.get("/api/listings?view=all&hide_scams=false").json()["total"] == 0


def test_password(env, monkeypatch):
    client, *_ = env
    from app import config
    monkeypatch.setattr(config, "APP_PASSWORD", "pw")
    assert client.get("/api/searches").status_code == 401
    assert client.get("/api/searches", auth=("admin", "pw")).status_code == 200
