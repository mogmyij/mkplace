"""Background worker: checks every minute for saved searches that are due and crawls them.

Runs independently of the web UI (separate container), so listings keep refreshing while
nobody has the site open.
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from . import db
from .pipeline import run_due


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    scheduler = BlockingScheduler()
    # max_instances=1 + coalesce: a slow crawl never overlaps the next tick.
    scheduler.add_job(run_due, "interval", minutes=1, jitter=20, max_instances=1, coalesce=True)
    run_due()
    scheduler.start()


if __name__ == "__main__":
    main()
