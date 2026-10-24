"""Global crawl settings, stored in the DB so they can be edited from the UI."""
from datetime import datetime

from . import config, db


def _env_hours():
    if "-" not in config.ACTIVE_HOURS:
        return None, None
    start, end = config.ACTIVE_HOURS.split("-", 1)
    return int(start), int(end)


def defaults() -> dict:
    start, end = _env_hours()
    return {
        "paused": False,
        "active_start_hour": start,
        "active_end_hour": end,
        "default_interval_minutes": config.DEFAULT_INTERVAL_MINUTES,
    }


def load(conn) -> dict:
    return {**defaults(), **db.get_meta(conn, "settings", {})}


def save(conn, values: dict) -> dict:
    merged = {**load(conn), **values}
    db.set_meta(conn, "settings", merged)
    return merged


def in_active_hours(s: dict, now: datetime | None = None) -> bool:
    start, end = s.get("active_start_hour"), s.get("active_end_hour")
    if start is None or end is None or start == end:
        return True
    hour = (now or datetime.now()).hour
    # Windows can wrap midnight, e.g. 22-6.
    return start <= hour < end if start < end else hour >= start or hour < end
