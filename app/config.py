import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "mkplace.db"))
FB_COOKIES_FILE = Path(os.environ.get("FB_COOKIES_FILE", DATA_DIR / "cookies.json"))

# Jev can be reached through OpenRouter (preferred when its key is set) or TypeSafe directly.
# Both serve the same System One request/response format.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
if OPENROUTER_API_KEY:
    JEV_PROVIDER = "OpenRouter"
    JEV_API_KEY = OPENROUTER_API_KEY
    _default_url, _default_model = "https://openrouter.ai/api/v1/systemone", "~typesafe/jev-latest"
else:
    JEV_PROVIDER = "TypeSafe"
    JEV_API_KEY = TYPESAFE_API_KEY
    _default_url, _default_model = "https://api.typesafe.ai/v1/systemone", "jev-latest"
JEV_URL = os.environ.get("JEV_URL") or _default_url
JEV_MODEL = os.environ.get("JEV_MODEL") or _default_model  # blank in .env means default

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DETAIL_FETCH_LIMIT = int(os.environ.get("DETAIL_FETCH_LIMIT", "12"))
SCROLL_ROUNDS = int(os.environ.get("SCROLL_ROUNDS", "6"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Crawl scheduling. Interval and active hours are only defaults: change them in the UI's Settings.
DEFAULT_INTERVAL_MINUTES = int(os.environ.get("DEFAULT_INTERVAL_MINUTES", "30"))
# e.g. "7-23" to only crawl between 07:00 and 23:00 local time (TZ env var). Empty = around the clock.
ACTIVE_HOURS = os.environ.get("ACTIVE_HOURS", "")
WORKER_TICK_SECONDS = int(os.environ.get("WORKER_TICK_SECONDS", "60"))
# Run the scheduler inside the web process (single container). Set to 0 if running app.worker separately.
RUN_WORKER = os.environ.get("RUN_WORKER", "1") != "0"
