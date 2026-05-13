"""SQLAlchemy models for the shared AnySQL database."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


EMBEDDING_DIMENSION = 4096


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    physical_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, default=new_id)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[str] = mapped_column(Text, default="")
    sql_dir: Mapped[str] = mapped_column(Text, default="")
    desc_dir: Mapped[str] = mapped_column(Text, default="")
    metadata_dir: Mapped[str] = mapped_column(Text, default="")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    updated_by: Mapped[str] = mapped_column(String(128), default="system")

    db_connection: Mapped["ProductDBConnection"] = relationship(back_populates="product", uselist=False, cascade="all, delete-orphan")


class ProductDBConnection(Base, TimestampMixin):
    __tablename__ = "product_db_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True, nullable=False)
    db_type: Mapped[str] = mapped_column(String(64), default="oracle")
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=1521)
    service_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    password_cipher: Mapped[str | None] = mapped_column(Text)

    product: Mapped[Product] = relationship(back_populates="db_connection")


class SQLRecordRow(Base, TimestampMixin):
    __tablename__ = "sql_records"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(Text, default="")
    raw_sql: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="")
    tables: Mapped[list] = mapped_column(JSON, default=list)
    statement_type: Mapped[str] = mapped_column(String(32), default="OTHER")
    line_number: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    learned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), default="source")
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    accepted_by: Mapped[str | None] = mapped_column(String(128))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    analysis: Mapped["SQLAnalysisRow"] = relationship(back_populates="record", uselist=False, cascade="all, delete-orphan")


class SQLAnalysisRow(Base, TimestampMixin):
    __tablename__ = "sql_analyses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    sql_id: Mapped[str] = mapped_column(ForeignKey("sql_records.id", ondelete="CASCADE"), unique=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    business_context: Mapped[list] = mapped_column(JSON, default=list)
    tables_involved: Mapped[dict] = mapped_column(JSON, default=dict)
    usage_guide: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[list] = mapped_column(JSON, default=list)
    complexity: Mapped[str] = mapped_column(String(32), default="simple")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    parameters: Mapped[list] = mapped_column(JSON, default=list)

    record: Mapped[SQLRecordRow] = relationship(back_populates="analysis")


class MetadataTable(Base, TimestampMixin):
    __tablename__ = "metadata_tables"
    __table_args__ = (UniqueConstraint("product_id", "table_name", name="uq_metadata_table_product_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    sync_version: Mapped[int] = mapped_column(Integer, default=0)


class MetadataColumn(Base, TimestampMixin):
    __tablename__ = "metadata_columns"
    __table_args__ = (UniqueConstraint("metadata_table_id", "column_name", name="uq_metadata_column_table_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    metadata_table_id: Mapped[str] = mapped_column(ForeignKey("metadata_tables.id", ondelete="CASCADE"), nullable=False, index=True)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), default="")
    data_length: Mapped[int | None] = mapped_column(Integer)
    data_precision: Mapped[int | None] = mapped_column(Integer)
    data_scale: Mapped[int | None] = mapped_column(Integer)
    nullable: Mapped[str] = mapped_column(String(8), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    ordinal: Mapped[int] = mapped_column(Integer, default=0)


class MetadataTableProfile(Base, TimestampMixin):
    __tablename__ = "metadata_table_profiles"
    __table_args__ = (UniqueConstraint("product_id", "table_name", name="uq_metadata_table_profile_product_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    role: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="auto", index=True)
    updated_by: Mapped[str] = mapped_column(String(128), default="system")


class SQLDraft(Base, TimestampMixin):
    __tablename__ = "sql_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    requirement: Mapped[str] = mapped_column(Text, default="")
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    business_meaning: Mapped[str] = mapped_column(Text, default="")
    usage_guide: Mapped[str] = mapped_column(Text, default="")
    parameters: Mapped[list] = mapped_column(JSON, default=list)
    tables: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_by: Mapped[str] = mapped_column(String(128), default="system")


class KnowledgeAcceptance(Base, TimestampMixin):
    __tablename__ = "knowledge_acceptances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    sql_id: Mapped[str] = mapped_column(ForeignKey("sql_records.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement: Mapped[str] = mapped_column(Text, default="")
    accepted_by: Mapped[str] = mapped_column(String(128), default="system")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    current_item: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class SQLEmbedding(Base, TimestampMixin):
    __tablename__ = "sql_embeddings"
    __table_args__ = (UniqueConstraint("product_id", "source_id", "embedding_model", name="uq_sql_embedding_source_model"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), default="sql")
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))


class MetadataEmbedding(Base, TimestampMixin):
    __tablename__ = "metadata_embeddings"
    __table_args__ = (UniqueConstraint("product_id", "source_id", "embedding_model", name="uq_metadata_embedding_source_model"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), default="metadata_table")
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))


class RagNode(Base, TimestampMixin):
    __tablename__ = "rag_nodes"
    __table_args__ = (UniqueConstraint("product_id", "source_type", "source_id", "facet", name="uq_rag_node_source_facet"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    facet: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lang: Mapped[str] = mapped_column(String(16), default="ja", index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    source_version: Mapped[int] = mapped_column(Integer, default=0)


class RagEmbedding(Base, TimestampMixin):
    __tablename__ = "rag_embeddings"
    __table_args__ = (UniqueConstraint("node_id", "embedding_model", name="uq_rag_embedding_node_model"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("rag_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    facet: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))


class RagTerm(Base, TimestampMixin):
    __tablename__ = "rag_terms"
    __table_args__ = (UniqueConstraint("product_id", "term", "lang", name="uq_rag_term_product_lang"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    term: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    lang: Mapped[str] = mapped_column(String(16), default="ja", index=True)
    synonyms: Mapped[list] = mapped_column(JSON, default=list)
    domain: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    source: Mapped[str] = mapped_column(String(32), default="system")


class JoinEdge(Base, TimestampMixin):
    __tablename__ = "join_edges"
    __table_args__ = (UniqueConstraint("product_id", "from_table", "from_column", "to_table", "to_column", name="uq_join_edge_columns"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    from_table: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    from_column: Mapped[str] = mapped_column(String(255), nullable=False)
    to_table: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    to_column: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="auto", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(128), default="system")


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    intent_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_bundles: Mapped[list] = mapped_column(JSON, default=list)
    retrieval_scores: Mapped[list] = mapped_column(JSON, default=list)
    validation_result: Mapped[dict] = mapped_column(JSON, default=dict)
    context_budget: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_call_count: Mapped[int] = mapped_column(Integer, default=0)
    repair_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_reason: Mapped[str] = mapped_column(Text, default="")


class AgentStep(Base, TimestampMixin):
    __tablename__ = "agent_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="succeeded")
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class FeedbackEvent(Base, TimestampMixin):
    __tablename__ = "feedback_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    sql_id: Mapped[str | None] = mapped_column(ForeignKey("sql_records.id", ondelete="SET NULL"), index=True)
    requirement: Mapped[str] = mapped_column(Text, default="")
    feedback_type: Mapped[str] = mapped_column(String(32), default="accepted", index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), default="system")


class KnowledgeGap(Base, TimestampMixin):
    __tablename__ = "knowledge_gaps"
    __table_args__ = (UniqueConstraint("product_id", "requirement_hash", name="uq_knowledge_gap_product_requirement"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    invalid_reason: Mapped[str] = mapped_column(Text, default="")
    trigger_reasons: Mapped[list] = mapped_column(JSON, default=list)
    validation_result: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    retrieval_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    candidate_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reviewed_by: Mapped[str] = mapped_column(String(128), default="system")


class KnowledgeCandidate(Base, TimestampMixin):
    __tablename__ = "knowledge_candidates"
    __table_args__ = (
        UniqueConstraint("gap_id", "candidate_type", "content_hash", name="uq_knowledge_candidate_gap_type_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    gap_id: Mapped[str] = mapped_column(ForeignKey("knowledge_gaps.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    source: Mapped[str] = mapped_column(String(64), default="auto_gap_analysis", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(128), default="system")
