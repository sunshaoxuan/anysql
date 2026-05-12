"""Application storage bootstrap."""

from __future__ import annotations

from redis import Redis
from rq import Queue

from anysql.config import AppConfig
from anysql.storage.database import Database


class StorageContext:
    def __init__(self, config: AppConfig):
        self.config = config
        self.database: Database | None = None
        self.queue: Queue | None = None
        self.redis: Redis | None = None
        if config.storage.backend == "postgresql":
            self.database = Database(config)
            self.database.init_schema()
        if config.queue.backend == "redis" and config.queue.redis_url:
            self.redis = Redis.from_url(config.queue.redis_url)
            self.queue = Queue("anysql", connection=self.redis)

    @property
    def is_database_mode(self) -> bool:
        return self.database is not None

    def enqueue(self, func_name: str, *args, **kwargs) -> str | None:
        if not self.queue:
            return None
        job = self.queue.enqueue(func_name, *args, **kwargs)
        return job.id

