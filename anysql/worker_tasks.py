"""RQ worker tasks for AnySQL."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from anysql.config import load_config
from anysql.core.agent_engine import AgentEngine
from anysql.core.llm_client import LLMClient
from anysql.core.metadata_collector import MetadataCollector
from anysql.core.sql_parser import scan_product_sqls
from anysql.harness.pipeline import AnalysisPipeline
from anysql.models.schemas import AnalysisStatus
from anysql.storage.database import Database
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import (
    JobRepository,
    MetadataRepository,
    ProductRepository,
    SQLKnowledgeRepository,
)


def _runtime():
    config = load_config()
    db = Database(config)
    llm = LLMClient(
        base_url=config.llm.base_url,
        chat_model=config.llm.chat_model,
        embed_model=config.llm.embed_model,
        timeout=config.llm.timeout,
        max_retries=config.llm.max_retries,
        temperature=config.llm.temperature,
    )
    return config, db, llm


def metadata_delta_sync(job_id: str, product_code: str) -> dict:
    return asyncio.run(_metadata_delta_sync(job_id, product_code))


async def _metadata_delta_sync(job_id: str, product_code: str) -> dict:
    config, db, llm = _runtime()
    try:
        with db.session() as session:
            jobs = JobRepository(session)
            jobs.mark_running(job_id)
            product = ProductRepository(session).get_by_code(product_code)
            if not product:
                raise ValueError(f"Product not found: {product_code}")
            db_config = ProductRepository(session).database_config(product)
        with tempfile.TemporaryDirectory() as tmp:
            collector = MetadataCollector(db_config, tmp)
            index = await collector.collect_all(use_cache=False)
            table_files = sorted((Path(tmp) / "tables").glob("*.json"))
            table_data = [json.loads(path.read_text(encoding="utf-8")) for path in table_files]
        with db.session() as session:
            jobs = JobRepository(session)
            metadata = MetadataRepository(session)
            total, changed = metadata.upsert_tables(product.id, table_data)
            indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_metadata(product.id)
            result = {"table_count": total or (index or {}).get("table_count", 0), "changed": changed, "indexed_count": indexed}
            jobs.mark_succeeded(job_id, result)
            return result
    except Exception as exc:
        with db.session() as session:
            JobRepository(session).mark_failed(job_id, str(exc))
        raise
    finally:
        await llm.close()


def sql_analysis(job_id: str, product_code: str, force: bool = False) -> dict:
    return asyncio.run(_sql_analysis(job_id, product_code, force))


async def _sql_analysis(job_id: str, product_code: str, force: bool = False) -> dict:
    config, db, llm = _runtime()
    try:
        agent = AgentEngine(llm)
        with db.session() as session:
            jobs = JobRepository(session)
            jobs.mark_running(job_id)
            product = ProductRepository(session).get_by_code(product_code)
            if not product:
                raise ValueError(f"Product not found: {product_code}")
            statements = scan_product_sqls(product.sql_dir, product.code)
            jobs.update_progress(job_id, total=len(statements))
        completed = 0
        failed = 0
        records = []
        for stmt in statements:
            with db.session() as session:
                JobRepository(session).update_progress(job_id, current_item=stmt.id)
            try:
                analysis = await agent.execute_task(f"Analyze SQL:\n{stmt.raw_sql}\nComment:\n{stmt.comment}", __import__("anysql.models.schemas", fromlist=["SQLAnalysis"]).SQLAnalysis)
                from anysql.models.schemas import SQLRecord
                record = SQLRecord(statement=stmt, analysis=analysis, status=AnalysisStatus.SUCCESS)
                completed += 1
            except Exception as exc:
                from anysql.models.schemas import SQLRecord
                record = SQLRecord(statement=stmt, status=AnalysisStatus.FAILED, error_message=str(exc))
                failed += 1
            records.append(record)
            with db.session() as session:
                product = ProductRepository(session).get_by_code(product_code)
                SQLKnowledgeRepository(session).upsert_record(product.id, record, learned=False, source_kind="source")
                JobRepository(session).update_progress(job_id, completed=completed, failed=failed)
        with db.session() as session:
            product = ProductRepository(session).get_by_code(product_code)
            ok_records = [r for r in records if r.status == AnalysisStatus.SUCCESS]
            indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_records(ok_records, product.id)
            result = {"completed": completed, "failed": failed, "indexed_count": indexed}
            JobRepository(session).mark_succeeded(job_id, result)
            return result
    except Exception as exc:
        with db.session() as session:
            JobRepository(session).mark_failed(job_id, str(exc))
        raise
    finally:
        await llm.close()


def embedding_rebuild(job_id: str, product_code: str) -> dict:
    return asyncio.run(_embedding_rebuild(job_id, product_code))


async def _embedding_rebuild(job_id: str, product_code: str) -> dict:
    config, db, llm = _runtime()
    try:
        with db.session() as session:
            jobs = JobRepository(session)
            jobs.mark_running(job_id)
            product = ProductRepository(session).get_by_code(product_code)
            if not product:
                raise ValueError(f"Product not found: {product_code}")
            records = SQLKnowledgeRepository(session).list_records(product.id)
            sql_indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_records(records, product.id)
            metadata_indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_metadata(product.id)
            result = {"sql_indexed": sql_indexed, "metadata_indexed": metadata_indexed}
            jobs.mark_succeeded(job_id, result)
            return result
    except Exception as exc:
        with db.session() as session:
            JobRepository(session).mark_failed(job_id, str(exc))
        raise
    finally:
        await llm.close()

