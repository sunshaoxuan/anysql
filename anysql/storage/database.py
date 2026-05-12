"""Database engine/session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from anysql.config import AppConfig
from anysql.logger import logger
from anysql.storage.models import Base


class Database:
    def __init__(self, config: AppConfig):
        if not config.storage.database_url:
            raise ValueError("storage.database_url is required for PostgreSQL mode")
        self.url = config.storage.database_url
        self.engine = create_engine(self.url, pool_pre_ping=True, future=True)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )

    def init_schema(self) -> None:
        """Create extensions and tables for first-run/dev deployments."""
        with self.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        Base.metadata.create_all(self.engine)
        logger.info("PostgreSQL schema is ready")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
