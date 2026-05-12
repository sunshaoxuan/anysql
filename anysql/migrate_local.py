"""Import legacy local config/products data into PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from anysql.config import load_config
from anysql.core.llm_client import LLMClient
from anysql.models.schemas import AnalysisStatus, ProductUpsertRequest, SQLRecord
from anysql.storage.database import Database
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import MetadataRepository, ProductRepository, SQLKnowledgeRepository, TableProfileRepository


async def migrate(config_path: str | None = None, rebuild_embeddings: bool = True) -> dict:
    config = load_config(config_path)
    db = Database(config)
    db.init_schema()
    report = {
        "products": 0,
        "sql_records": 0,
        "metadata_tables": 0,
        "skipped_generated": 0,
        "sql_embeddings": 0,
        "metadata_embeddings": 0,
        "errors": [],
    }
    llm = LLMClient(
        base_url=config.llm.base_url,
        chat_model=config.llm.chat_model,
        embed_model=config.llm.embed_model,
        timeout=config.llm.timeout,
        max_retries=config.llm.max_retries,
        temperature=config.llm.temperature,
    )
    try:
        imported_products: list[tuple[str, str]] = []
        for code, pcfg in config.products.items():
            with db.session() as session:
                product_repo = ProductRepository(session)
                product, _ = product_repo.upsert(ProductUpsertRequest(
                    physical_id=pcfg.physical_id or None,
                    code=code,
                    name=pcfg.name,
                    description=pcfg.description,
                    rules=pcfg.rules,
                    sql_dir=pcfg.sql_dir,
                    desc_dir=pcfg.desc_dir,
                    metadata_dir=pcfg.metadata_dir,
                    database_type=pcfg.database.type,
                    database_host=pcfg.database.host,
                    database_port=pcfg.database.port,
                    database_service_name=pcfg.database.service_name,
                    database_username=pcfg.database.username,
                    database_password=pcfg.database.password,
                ))
                report["products"] += 1
                desc_dir = Path(pcfg.desc_dir)
                records: list[SQLRecord] = []
                for path in sorted(desc_dir.glob("*.json")) if desc_dir.exists() else []:
                    if "_generated_" in path.name:
                        report["skipped_generated"] += 1
                        continue
                    try:
                        record = SQLRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
                        if record.status != AnalysisStatus.SUCCESS:
                            report["skipped_generated"] += 1
                            continue
                        SQLKnowledgeRepository(session).upsert_record(product.id, record, learned=False, source_kind="source")
                        records.append(record)
                        report["sql_records"] += 1
                    except Exception as exc:
                        report["errors"].append(f"{path}: {exc}")

                table_dir = Path(pcfg.metadata_dir) / "tables"
                table_data = []
                for path in sorted(table_dir.glob("*.json")) if table_dir.exists() else []:
                    try:
                        table_data.append(json.loads(path.read_text(encoding="utf-8")))
                    except Exception as exc:
                        report["errors"].append(f"{path}: {exc}")
                total, _ = MetadataRepository(session).upsert_tables(product.id, table_data)
                TableProfileRepository(session).rebuild_auto(product.id)
                report["metadata_tables"] += total
                imported_products.append((code, product.id))
        if rebuild_embeddings:
            for code, product_id in imported_products:
                with db.session() as session:
                    records = SQLKnowledgeRepository(session).list_records(product_id)
                    vector = PGVectorRepository(session, llm, config.llm.embed_model)
                    report["sql_embeddings"] += await vector.index_records(records, product_id)
                with db.session() as session:
                    vector = PGVectorRepository(session, llm, config.llm.embed_model)
                    report["metadata_embeddings"] += await vector.index_metadata(product_id)
        return report
    finally:
        await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-embeddings", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(migrate(args.config, rebuild_embeddings=not args.no_embeddings))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
