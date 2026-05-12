"""initial schema

Revision ID: 20260512_0001
Revises:
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
from anysql.storage.models import Base

revision = "20260512_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    Base.metadata.create_all(bind)
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS alembic_version (
            version_num VARCHAR(32) NOT NULL,
            CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
