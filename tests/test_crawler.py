import json

from app.crawler import load_cookies, parse_card, parse_price, search_url


def test_parse_card_basic():
    item = parse_card("/marketplace/item/123456789/?ref=search", "$350\nSony PS5 Disc\nSydney, NSW", "https://img")
    assert item == {
        "id": "123456789",
        "title": "Sony PS5 Disc",
        "price": 350.0,
        "price_text": "$350",
        "location": "Sydney, NSW",
        "image_url": "https://img",
        "url": "https://www.facebook.com/marketplace/item/123456789/",
    }


def test_parse_card_discount_and_currency_prefix():
    item = parse_card("/marketplace/item/9/", "A$1,200\nA$1,500\nMacBook Pro 14\nMelbourne, VIC", None)
    assert item["price"] == 1200
    assert item["title"] == "MacBook Pro 14"


def test_parse_card_free_and_vehicle_k():
    assert parse_card("/marketplace/item/1/", "Free\nCouch\nPerth", None)["price"] == 0
    assert parse_price("$12k") == 12000


def test_parse_card_rejects_non_items():
    assert parse_card("/marketplace/category/vehicles", "Vehicles", None) is None


def test_search_url():
    url = search_url({"keywords": "ps5 disc", "location": "sydney", "radius_km": 20, "min_price": None, "max_price": 350})
    assert url.startswith("https://www.facebook.com/marketplace/sydney/search/?")
    assert "query=ps5+disc" in url and "maxPrice=350" in url and "radius=20" in url and "minPrice" not in url


def test_load_cookie_editor_export(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"name": "c_user", "value": "1", "domain": ".facebook.com", "sameSite": "no_restriction",
                              "expirationDate": 1900000000.5}]))
    [c] = load_cookies(p)
    assert c["sameSite"] == "None" and c["expires"] == 1900000000.5


def test_shrink_resizes_to_jpeg():
    import io

    from PIL import Image

    from app.crawler import shrink
    buf = io.BytesIO()
    Image.new("RGBA", (3000, 1500), (255, 0, 0, 128)).save(buf, "PNG")
    out = Image.open(io.BytesIO(shrink(buf.getvalue(), 1024)))
    assert out.format == "JPEG" and out.size == (1024, 512)


def test_photo_key_ignores_size_variants():
    from app.crawler import photo_key
    a = "https://scontent.xx.fbcdn.net/v/t45.5328-4/123_456_n.jpg?stp=dst-jpg_s960x960&_nc=1"
    b = "https://scontent.xx.fbcdn.net/v/t45.5328-4/123_456_n.jpg?stp=dst-jpg_p180x540&_nc=2"
    assert photo_key(a) == photo_key(b) == "123_456_n.jpg"
