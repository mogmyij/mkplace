"""Background worker: checks every tick for saved searches that are due and crawls them.

By default the web process runs this scheduler itself (RUN_WORKER=1), so one container does
everything. `python -m app.worker` runs it standalone if you'd rather split the two.
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from . import config, db
from .pipeline import run_due


def add_jobs(scheduler):
    # max_instances=1 + coalesce: a slow crawl never overlaps the next tick.
    scheduler.add_job(run_due, "interval", seconds=config.WORKER_TICK_SECONDS, jitter=20,
                      max_instances=1, coalesce=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    scheduler = BlockingScheduler()
    add_jobs(scheduler)
    run_due()
    scheduler.start()


if __name__ == "__main__":
    main()
