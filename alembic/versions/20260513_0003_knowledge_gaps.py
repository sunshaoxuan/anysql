"""knowledge gap candidate workflow

Revision ID: 20260513_0003
Revises: 20260513_0002
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260513_0003"
down_revision = "20260513_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("knowledge_gaps"):
        op.create_table(
            "knowledge_gaps",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("agent_run_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("requirement_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("invalid_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("validation_result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("retrieval_snapshot", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("candidate_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "requirement_hash", name="uq_knowledge_gap_product_requirement"),
        )
        op.create_index("ix_knowledge_gaps_product_id", "knowledge_gaps", ["product_id"])
        op.create_index("ix_knowledge_gaps_agent_run_id", "knowledge_gaps", ["agent_run_id"])
        op.create_index("ix_knowledge_gaps_session_id", "knowledge_gaps", ["session_id"])
        op.create_index("ix_knowledge_gaps_requirement_hash", "knowledge_gaps", ["requirement_hash"])
        op.create_index("ix_knowledge_gaps_status", "knowledge_gaps", ["status"])

    if not inspector.has_table("knowledge_candidates"):
        op.create_table(
            "knowledge_candidates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("gap_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="auto_gap_analysis"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["gap_id"], ["knowledge_gaps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gap_id", "candidate_type", "content_hash", name="uq_knowledge_candidate_gap_type_hash"),
        )
        op.create_index("ix_knowledge_candidates_gap_id", "knowledge_candidates", ["gap_id"])
        op.create_index("ix_knowledge_candidates_product_id", "knowledge_candidates", ["product_id"])
        op.create_index("ix_knowledge_candidates_candidate_type", "knowledge_candidates", ["candidate_type"])
        op.create_index("ix_knowledge_candidates_status", "knowledge_candidates", ["status"])
        op.create_index("ix_knowledge_candidates_source", "knowledge_candidates", ["source"])
        op.create_index("ix_knowledge_candidates_content_hash", "knowledge_candidates", ["content_hash"])


def downgrade() -> None:
    op.drop_table("knowledge_candidates")
    op.drop_table("knowledge_gaps")
