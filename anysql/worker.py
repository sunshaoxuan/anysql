"""RQ worker entrypoint."""

from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from anysql.config import load_config


def main() -> None:
    config = load_config()
    if config.queue.backend != "redis" or not config.queue.redis_url:
        raise SystemExit("RQ worker requires queue.backend=redis and queue.redis_url")
    redis = Redis.from_url(config.queue.redis_url)
    worker = Worker([Queue("anysql", connection=redis)], connection=redis)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()

