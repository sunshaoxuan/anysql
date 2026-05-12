"""Daily metadata sync scheduler."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from redis import Redis
from rq import Queue

from anysql.config import load_config
from anysql.storage.database import Database
from anysql.storage.repositories import JobRepository, ProductRepository


def next_daily_run(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    candidate = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate


def enqueue_daily_syncs() -> None:
    config = load_config()
    if config.queue.backend != "redis" or not config.queue.redis_url:
        raise SystemExit("Scheduler requires Redis queue configuration")
    redis = Redis.from_url(config.queue.redis_url)
    queue = Queue("anysql", connection=redis)
    db = Database(config)
    with db.session() as session:
        for product in ProductRepository(session).list():
            job = JobRepository(session).create("metadata_delta_sync", product.id, {"product_code": product.code, "reason": "daily"})
            queue.enqueue("anysql.worker_tasks.metadata_delta_sync", job.id, product.code)


def main() -> None:
    config = load_config()
    if config.queue.backend != "redis" or not config.queue.redis_url:
        raise SystemExit("Scheduler requires Redis queue configuration")
    next_run = next_daily_run()
    while True:
        now = datetime.now()
        if now >= next_run:
            enqueue_daily_syncs()
            next_run = next_daily_run(now + timedelta(minutes=1))
        time.sleep(60)


if __name__ == "__main__":
    main()
