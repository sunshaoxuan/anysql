"""Add knowledge gap progress fields.

Revision ID: 20260513_0004
Revises: 20260513_0003
Create Date: 2026-05-13 14:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260513_0004"
down_revision = "20260513_0003"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("knowledge_gaps", "progress_stage"):
        op.add_column("knowledge_gaps", sa.Column("progress_stage", sa.String(length=64), nullable=True, server_default="queued"))
    if not _has_column("knowledge_gaps", "progress_percent"):
        op.add_column("knowledge_gaps", sa.Column("progress_percent", sa.Float(), nullable=True, server_default="0"))
    if not _has_column("knowledge_gaps", "progress_message"):
        op.add_column("knowledge_gaps", sa.Column("progress_message", sa.Text(), nullable=True, server_default=""))
    if not _has_column("knowledge_gaps", "progress_detail"):
        op.add_column("knowledge_gaps", sa.Column("progress_detail", sa.JSON(), nullable=True, server_default=sa.text("'{}'::json")))


def downgrade() -> None:
    if _has_column("knowledge_gaps", "progress_detail"):
        op.drop_column("knowledge_gaps", "progress_detail")
    if _has_column("knowledge_gaps", "progress_message"):
        op.drop_column("knowledge_gaps", "progress_message")
    if _has_column("knowledge_gaps", "progress_percent"):
        op.drop_column("knowledge_gaps", "progress_percent")
    if _has_column("knowledge_gaps", "progress_stage"):
        op.drop_column("knowledge_gaps", "progress_stage")
