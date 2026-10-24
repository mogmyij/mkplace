import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "mkplace.db"))
FB_COOKIES_FILE = Path(os.environ.get("FB_COOKIES_FILE", DATA_DIR / "cookies.json"))

TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TYPESAFE_URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DETAIL_FETCH_LIMIT = int(os.environ.get("DETAIL_FETCH_LIMIT", "12"))
SCROLL_ROUNDS = int(os.environ.get("SCROLL_ROUNDS", "6"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
