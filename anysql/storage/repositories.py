"""Repository layer for PostgreSQL-backed AnySQL data."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from uuid import uuid4
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from anysql.config import DatabaseConfig, ProductConfig
from anysql.models.schemas import (
    AnalysisProgress,
    AnalysisStatus,
    Complexity,
    ProductInfo,
    ProductUpsertRequest,
    SQLAnalysis,
    SQLRecord,
    SQLStatement,
    StatementType,
)
from anysql.core.table_profiles import infer_table_profile
from anysql.core.table_profile_classifier import classify_table_profile
from anysql.storage.models import (
    Job,
    KnowledgeAcceptance,
    MetadataColumn,
    MetadataTableProfile,
    MetadataTable,
    Product,
    ProductDBConnection,
    SQLAnalysisRow,
    SQLDraft,
    SQLRecordRow,
)


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        return value


def stable_hash(data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProductRepository:
    def __init__(self, session: Session):
        self.session = session

    def list(self, include_deleted: bool = False) -> list[Product]:
        stmt = select(Product).order_by(Product.code)
        if not include_deleted:
            stmt = stmt.where(Product.deleted.is_(False))
        return list(self.session.scalars(stmt))

    def get_by_code(self, code: str, include_deleted: bool = False) -> Product | None:
        stmt = select(Product).where(Product.code == code)
        if not include_deleted:
            stmt = stmt.where(Product.deleted.is_(False))
        return self.session.scalar(stmt)

    def get_by_physical_id(self, physical_id: str) -> Product | None:
        return self.session.scalar(select(Product).where(Product.physical_id == physical_id))

    def upsert(self, req: ProductUpsertRequest) -> tuple[Product, bool]:
        existing = self.get_by_physical_id(req.physical_id) if req.physical_id else None
        if existing is None:
            existing = self.get_by_code(req.code, include_deleted=True)
        is_new = existing is None
        product = existing or Product(code=req.code, physical_id=req.physical_id or str(uuid4()))
        product.code = req.code
        product.name = req.name
        product.description = req.description
        product.rules = req.rules
        product.sql_dir = req.sql_dir
        product.desc_dir = req.desc_dir
        product.metadata_dir = req.metadata_dir
        product.deleted = False
        if is_new:
            self.session.add(product)
            self.session.flush()

        conn = product.db_connection or ProductDBConnection(product_id=product.id)
        conn.db_type = req.database_type
        conn.host = req.database_host
        conn.port = req.database_port
        conn.service_name = req.database_service_name
        conn.username = req.database_username
        conn.password_cipher = encrypt_secret(req.database_password)
        if not product.db_connection:
            self.session.add(conn)
        self.session.flush()
        return product, is_new

    def soft_delete(self, code: str) -> bool:
        product = self.get_by_code(code)
        if not product:
            return False
        product.deleted = True
        self.session.flush()
        return True

    def to_info(self, product: Product, sql_count: int = 0, analyzed_count: int = 0) -> ProductInfo:
        conn = product.db_connection
        return ProductInfo(
            id=product.code,
            physical_id=product.physical_id,
            code=product.code,
            name=product.name,
            description=product.description or "",
            rules=product.rules or "",
            sql_dir=product.sql_dir or "",
            desc_dir=product.desc_dir or "",
            metadata_dir=product.metadata_dir or "",
            database_type=conn.db_type if conn else "oracle",
            database_host=conn.host if conn else None,
            database_port=conn.port if conn else 1521,
            database_service_name=conn.service_name if conn else None,
            database_username=conn.username if conn else None,
            database_password=decrypt_secret(conn.password_cipher) if conn else None,
            sql_count=sql_count,
            analyzed_count=analyzed_count,
            db_configured=bool(conn and conn.host and conn.service_name and conn.username),
        )

    def database_config(self, product: Product) -> DatabaseConfig:
        conn = product.db_connection
        if not conn:
            return DatabaseConfig()
        return DatabaseConfig(
            type=conn.db_type,
            host=conn.host,
            port=conn.port,
            service_name=conn.service_name,
            username=conn.username,
            password=decrypt_secret(conn.password_cipher),
        )

    def to_product_config(self, product: Product) -> ProductConfig:
        return ProductConfig(
            physical_id=product.physical_id,
            name=product.name,
            description=product.description or "",
            rules=product.rules or "",
            sql_dir=product.sql_dir or "",
            desc_dir=product.desc_dir or "",
            metadata_dir=product.metadata_dir or "",
            database=self.database_config(product),
        )


class SQLKnowledgeRepository:
    def __init__(self, session: Session):
        self.session = session

    def count_for_product(self, product_id: str) -> tuple[int, int]:
        rows = list(self.session.scalars(select(SQLRecordRow).where(SQLRecordRow.product_id == product_id)))
        return len(rows), sum(1 for r in rows if r.status == AnalysisStatus.SUCCESS.value)

    def list_records(self, product_id: str) -> list[SQLRecord]:
        rows = list(self.session.scalars(select(SQLRecordRow).where(SQLRecordRow.product_id == product_id).order_by(SQLRecordRow.id)))
        return [self.to_schema(row) for row in rows]

    def get(self, sql_id: str) -> SQLRecord | None:
        row = self.session.get(SQLRecordRow, sql_id)
        return self.to_schema(row) if row else None

    def upsert_record(self, product_id: str, record: SQLRecord, learned: bool = False, source_kind: str = "source") -> SQLRecordRow:
        row = self.session.get(SQLRecordRow, record.statement.id)
        if not row:
            row = SQLRecordRow(id=record.statement.id, product_id=product_id)
            self.session.add(row)
        stmt = record.statement
        row.source_file = stmt.source_file
        row.raw_sql = stmt.raw_sql
        row.comment = stmt.comment
        row.tables = stmt.tables
        row.statement_type = stmt.statement_type.value
        row.line_number = stmt.line_number
        row.status = record.status.value
        row.learned = learned
        row.source_kind = source_kind
        row.analyzed_at = record.analyzed_at
        row.error_message = record.error_message
        row.metadata_snapshot = record.metadata_snapshot
        if record.analysis:
            analysis = row.analysis or SQLAnalysisRow(sql_id=row.id)
            analysis.summary = record.analysis.summary
            analysis.business_context = record.analysis.business_context
            analysis.tables_involved = record.analysis.tables_involved
            analysis.usage_guide = record.analysis.usage_guide
            analysis.category = record.analysis.category
            analysis.complexity = record.analysis.complexity.value
            analysis.keywords = record.analysis.keywords
            analysis.parameters = record.analysis.parameters
            if not row.analysis:
                self.session.add(analysis)
        self.session.flush()
        return row

    def accept_generated(self, product_id: str, requirement: str, record: SQLRecord, accepted_by: str = "system") -> SQLRecordRow:
        row = self.upsert_record(product_id, record, learned=True, source_kind="accepted")
        row.accepted_by = accepted_by
        self.session.add(KnowledgeAcceptance(product_id=product_id, sql_id=row.id, requirement=requirement, accepted_by=accepted_by))
        self.session.flush()
        return row

    def save_draft(self, product_id: str, requirement: str, record: SQLRecord, session_id: str | None = None) -> SQLDraft:
        analysis = record.analysis
        draft = SQLDraft(
            product_id=product_id,
            session_id=session_id,
            requirement=requirement,
            sql=record.statement.raw_sql,
            summary=analysis.summary if analysis else "",
            business_meaning=" / ".join(analysis.business_context) if analysis else "",
            usage_guide=analysis.usage_guide if analysis else "",
            parameters=analysis.parameters if analysis else [],
            tables=record.statement.tables,
        )
        self.session.add(draft)
        self.session.flush()
        return draft

    @staticmethod
    def to_schema(row: SQLRecordRow) -> SQLRecord:
        stmt = SQLStatement(
            id=row.id,
            product=row.product_id,
            source_file=row.source_file,
            raw_sql=row.raw_sql,
            comment=row.comment or "",
            tables=row.tables or [],
            statement_type=StatementType(row.statement_type) if row.statement_type in StatementType._value2member_map_ else StatementType.OTHER,
            line_number=row.line_number,
        )
        analysis = None
        if row.analysis:
            analysis = SQLAnalysis(
                summary=row.analysis.summary,
                business_context=row.analysis.business_context or [],
                tables_involved=row.analysis.tables_involved or {},
                usage_guide=row.analysis.usage_guide or "",
                category=row.analysis.category or [],
                complexity=Complexity(row.analysis.complexity) if row.analysis.complexity in Complexity._value2member_map_ else Complexity.SIMPLE,
                keywords=row.analysis.keywords or [],
                parameters=row.analysis.parameters or [],
            )
        return SQLRecord(
            statement=stmt,
            analysis=analysis,
            status=AnalysisStatus(row.status) if row.status in AnalysisStatus._value2member_map_ else AnalysisStatus.PENDING,
            analyzed_at=row.analyzed_at,
            error_message=row.error_message,
            metadata_snapshot=row.metadata_snapshot or {},
        )


class MetadataRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_tables(self, product_id: str, table_data: Iterable[dict]) -> tuple[int, int]:
        changed = 0
        total = 0
        seen: set[str] = set()
        for data in table_data:
            total += 1
            table_name = str(data.get("name") or data.get("table_name") or "").upper()
            if not table_name:
                continue
            seen.add(table_name)
            content_hash = stable_hash(data)
            row = self.session.scalar(
                select(MetadataTable).where(MetadataTable.product_id == product_id, MetadataTable.table_name == table_name)
            )
            if not row:
                row = MetadataTable(product_id=product_id, table_name=table_name)
                self.session.add(row)
            if row.content_hash != content_hash:
                changed += 1
                row.comment = data.get("comment") or data.get("description") or ""
                row.raw = data
                row.content_hash = content_hash
                row.sync_version = (row.sync_version or 0) + 1
                self.session.flush()
                self.session.query(MetadataColumn).filter(MetadataColumn.metadata_table_id == row.id).delete()
                for idx, col in enumerate(data.get("columns") or []):
                    self.session.add(MetadataColumn(
                        metadata_table_id=row.id,
                        column_name=str(col.get("name") or col.get("column_name") or "").upper(),
                        data_type=col.get("data_type") or col.get("type") or "",
                        data_length=col.get("data_length"),
                        data_precision=col.get("data_precision"),
                        data_scale=col.get("data_scale"),
                        nullable=col.get("nullable") or "",
                        comment=col.get("comment") or col.get("comments") or "",
                        ordinal=idx,
                    ))
        stale = list(self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id, ~MetadataTable.table_name.in_(seen)))) if seen else []
        for row in stale:
            self.session.delete(row)
            changed += 1
        self.session.flush()
        return total, changed

    def list_tables(self, product_id: str) -> list[MetadataTable]:
        return list(self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id).order_by(MetadataTable.table_name)))

    def get_table(self, product_id: str, table_name: str) -> dict | None:
        row = self.session.scalar(select(MetadataTable).where(MetadataTable.product_id == product_id, MetadataTable.table_name == table_name.upper()))
        return row.raw if row else None


class TableProfileRepository:
    def __init__(self, session: Session):
        self.session = session

    def rebuild_auto(self, product_id: str) -> int:
        count = 0
        tables = self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id)).all()
        for table in tables:
            existing = self.get(product_id, table.table_name)
            if existing and existing.source == "manual":
                continue
            columns = [
                {"column_name": col.column_name, "comment": col.comment}
                for col in self.session.scalars(select(MetadataColumn).where(MetadataColumn.metadata_table_id == table.id))
            ]
            inferred = infer_table_profile(table.table_name, table.comment, columns)
            if not existing:
                existing = MetadataTableProfile(product_id=product_id, table_name=table.table_name)
                self.session.add(existing)
            existing.domain = inferred.domain
            existing.role = inferred.role
            existing.confidence = inferred.confidence
            existing.reason = inferred.reason
            existing.source = "auto"
            existing.updated_by = "system"
            count += 1
        self.session.flush()
        return count

    async def rebuild_auto_with_llm(self, product_id: str, llm, max_llm_reviews: int = 20) -> int:
        count = 0
        tables = self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id)).all()
        tables = sorted(tables, key=_profile_review_priority, reverse=True)
        llm_reviews = 0
        for table in tables:
            existing = self.get(product_id, table.table_name)
            if existing and existing.source == "manual":
                continue
            columns = [
                {"column_name": col.column_name, "comment": col.comment}
                for col in self.session.scalars(select(MetadataColumn).where(MetadataColumn.metadata_table_id == table.id).order_by(MetadataColumn.ordinal))
            ]
            if llm_reviews < max_llm_reviews:
                inferred = await classify_table_profile(table.table_name, table.comment, columns, llm)
                if inferred.source == "auto_llm":
                    llm_reviews += 1
            else:
                inferred = infer_table_profile(table.table_name, table.comment, columns)
            if not existing:
                existing = MetadataTableProfile(product_id=product_id, table_name=table.table_name)
                self.session.add(existing)
            existing.domain = inferred.domain
            existing.role = inferred.role
            existing.confidence = inferred.confidence
            existing.reason = inferred.reason
            existing.source = inferred.source
            existing.updated_by = "system"
            count += 1
        self.session.flush()
        return count

    async def classify_one_with_llm(self, product_id: str, table_name: str, llm) -> dict:
        table_name = table_name.upper()
        table = self.session.scalar(select(MetadataTable).where(MetadataTable.product_id == product_id, MetadataTable.table_name == table_name))
        if not table:
            raise ValueError(f"Table not found: {table_name}")
        existing = self.get(product_id, table_name)
        if existing and existing.source == "manual":
            return self.to_dict(table, existing)
        columns = [
            {"column_name": col.column_name, "comment": col.comment}
            for col in self.session.scalars(select(MetadataColumn).where(MetadataColumn.metadata_table_id == table.id).order_by(MetadataColumn.ordinal))
        ]
        inferred = await classify_table_profile(table.table_name, table.comment, columns, llm)
        if inferred.source != "auto_llm":
            inferred = infer_table_profile(table.table_name, table.comment, columns)
        if not existing:
            existing = MetadataTableProfile(product_id=product_id, table_name=table_name)
            self.session.add(existing)
        existing.domain = inferred.domain
        existing.role = inferred.role
        existing.confidence = inferred.confidence
        existing.reason = inferred.reason
        existing.source = inferred.source
        existing.updated_by = "system"
        self.session.flush()
        return self.to_dict(table, existing)

    def get(self, product_id: str, table_name: str) -> MetadataTableProfile | None:
        return self.session.scalar(
            select(MetadataTableProfile).where(
                MetadataTableProfile.product_id == product_id,
                MetadataTableProfile.table_name == table_name.upper(),
            )
        )

    def list(self, product_id: str, q: str = "", limit: int = 200) -> list[dict]:
        stmt = (
            select(MetadataTable, MetadataTableProfile)
            .join(
                MetadataTableProfile,
                (MetadataTableProfile.product_id == MetadataTable.product_id)
                & (MetadataTableProfile.table_name == MetadataTable.table_name),
                isouter=True,
            )
            .where(MetadataTable.product_id == product_id)
            .order_by(MetadataTable.table_name)
            .limit(limit)
        )
        if q:
            like = f"%{q.upper()}%"
            stmt = stmt.where((MetadataTable.table_name.ilike(like)) | (MetadataTable.comment.ilike(f"%{q}%")))
        rows = self.session.execute(stmt).all()
        result = []
        for table, profile in rows:
            result.append(self.to_dict(table, profile))
        return result

    def update_manual(self, product_id: str, table_name: str, domain: str, role: str, updated_by: str = "system") -> dict:
        table_name = table_name.upper()
        table = self.session.scalar(select(MetadataTable).where(MetadataTable.product_id == product_id, MetadataTable.table_name == table_name))
        if not table:
            raise ValueError(f"Table not found: {table_name}")
        profile = self.get(product_id, table_name)
        if not profile:
            profile = MetadataTableProfile(product_id=product_id, table_name=table_name)
            self.session.add(profile)
        profile.domain = domain or "unknown"
        profile.role = role or "unknown"
        profile.confidence = 1.0
        profile.reason = "manual override"
        profile.source = "manual"
        profile.updated_by = updated_by
        self.session.flush()
        return self.to_dict(table, profile)

    @staticmethod
    def to_dict(table: MetadataTable, profile: MetadataTableProfile | None) -> dict:
        return {
            "table_name": table.table_name,
            "comment": table.comment,
            "domain": profile.domain if profile else "unknown",
            "role": profile.role if profile else "unknown",
            "confidence": float(profile.confidence or 0) if profile else 0,
            "reason": profile.reason if profile else "",
            "source": profile.source if profile else "auto",
        }


def _profile_review_priority(table: MetadataTable) -> int:
    text = f"{table.table_name} {table.comment}"
    score = 0
    if "基本情報" in text:
        score += 8
    if "非常勤職員" in text:
        score += 8
    if table.table_name.upper().startswith("DJND"):
        score += 5
    if any(word in text for word in ("任免", "発令", "異動")):
        score += 3
    return score


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job_type: str, product_id: str | None = None, payload: dict | None = None) -> Job:
        job = Job(job_type=job_type, product_id=product_id, payload=payload or {}, status="queued")
        self.session.add(job)
        self.session.flush()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.session.get(Job, job_id)

    def latest(self, product_id: str, job_type: str) -> Job | None:
        return self.session.scalar(
            select(Job).where(Job.product_id == product_id, Job.job_type == job_type).order_by(Job.created_at.desc())
        )

    def mark_running(self, job_id: str) -> None:
        self.session.execute(update(Job).where(Job.id == job_id).values(status="running", started_at=datetime.now()))

    def update_progress(self, job_id: str, **values) -> None:
        self.session.execute(update(Job).where(Job.id == job_id).values(**values))

    def mark_succeeded(self, job_id: str, result: dict | None = None) -> None:
        self.session.execute(update(Job).where(Job.id == job_id).values(status="succeeded", result=result or {}, finished_at=datetime.now()))

    def mark_failed(self, job_id: str, error: str) -> None:
        self.session.execute(update(Job).where(Job.id == job_id).values(status="failed", error=error, finished_at=datetime.now()))

    @staticmethod
    def to_progress(job: Job, product_code: str = "") -> AnalysisProgress:
        return AnalysisProgress(
            product=product_code,
            total=job.total,
            completed=job.completed,
            failed=job.failed,
            current_sql=job.current_item,
            status=AnalysisStatus.RUNNING if job.status == "running" else AnalysisStatus.COMPLETED if job.status == "succeeded" else AnalysisStatus.FAILED if job.status == "failed" else AnalysisStatus.PENDING,
            started_at=job.started_at,
            elapsed_seconds=((job.finished_at or datetime.now()) - job.started_at).total_seconds() if job.started_at else 0,
            errors=[job.error] if job.error else [],
        )
