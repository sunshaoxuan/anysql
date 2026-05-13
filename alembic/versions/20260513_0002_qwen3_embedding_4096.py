"""switch embeddings to qwen3 4096 dimensions

Revision ID: 20260513_0002
Revises: 20260512_0001
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision = "20260513_0002"
down_revision = "20260512_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DELETE FROM rag_embeddings")
    bind.exec_driver_sql("DELETE FROM metadata_embeddings")
    bind.exec_driver_sql("DELETE FROM sql_embeddings")
    bind.exec_driver_sql("ALTER TABLE rag_embeddings ALTER COLUMN embedding TYPE vector(4096)")
    bind.exec_driver_sql("ALTER TABLE metadata_embeddings ALTER COLUMN embedding TYPE vector(4096)")
    bind.exec_driver_sql("ALTER TABLE sql_embeddings ALTER COLUMN embedding TYPE vector(4096)")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DELETE FROM rag_embeddings")
    bind.exec_driver_sql("DELETE FROM metadata_embeddings")
    bind.exec_driver_sql("DELETE FROM sql_embeddings")
    bind.exec_driver_sql("ALTER TABLE rag_embeddings ALTER COLUMN embedding TYPE vector(1024)")
    bind.exec_driver_sql("ALTER TABLE metadata_embeddings ALTER COLUMN embedding TYPE vector(1024)")
    bind.exec_driver_sql("ALTER TABLE sql_embeddings ALTER COLUMN embedding TYPE vector(1024)")
