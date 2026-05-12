"""
AnySQL Harness 管线 (实时计数修正版)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from anysql.config import AppConfig
from anysql.core.agent_engine import AgentEngine
from anysql.core.llm_client import LLMClient
from anysql.core.sql_parser import scan_product_sqls
from anysql.core.vector_engine import VectorEngine
from anysql.core.metadata_collector import MetadataCollector
from anysql.logger import logger
from anysql.models.schemas import (
    AnalysisProgress,
    AnalysisStatus,
    SQLRecord,
    SQLStatement,
    SQLAnalysis,
)


class AnalysisPipeline:
    def __init__(self, config: AppConfig, llm: LLMClient, vector_engine: VectorEngine, agent: AgentEngine):
        self.config = config
        self.llm = llm
        self.vector = vector_engine
        self.agent = agent
        # 核心状态存储：确保全局唯一且持久
        self._progress_map: dict[str, AnalysisProgress] = {}

    def get_progress(self, product: str) -> AnalysisProgress:
        """核心修复：如果内存中已存在进度对象，必须返回同一个对象，而不是新建"""
        if product not in self._progress_map:
            pcfg = self.config.products.get(product)
            progress = AnalysisProgress(product=product)
            if pcfg:
                sqls = scan_product_sqls(pcfg.sql_dir, product)
                progress.total = len(sqls)
                # 尝试从磁盘恢复历史完成数
                desc_path = Path(pcfg.desc_dir)
                if desc_path.exists():
                    progress.completed = len(list(desc_path.glob("*.json")))
            self._progress_map[product] = progress
        
        return self._progress_map[product]

    def _build_metadata_context(self, stmt: SQLStatement, collector: MetadataCollector) -> str:
        tables = self._get_snapshot(stmt, collector)
        if not tables:
            return "No table metadata found."

        sections: list[str] = []
        for table_name, meta in tables.items():
            columns = meta.get("columns") or meta.get("COLUMN") or []
            if isinstance(columns, list):
                col_names = []
                for col in columns[:20]:
                    if isinstance(col, dict):
                        col_names.append(str(col.get("name") or col.get("column_name") or col.get("COLUMN_NAME") or col))
                    else:
                        col_names.append(str(col))
                col_text = ", ".join(col_names)
            else:
                col_text = str(columns)[:1000]
            sections.append(f"Table {table_name}: {col_text or json.dumps(meta, ensure_ascii=False)[:1000]}")
        return "\n".join(sections)

    def _get_snapshot(self, stmt: SQLStatement, collector: MetadataCollector) -> dict[str, dict]:
        snapshot: dict[str, dict] = {}
        for table in stmt.tables:
            table_name = table.split(".")[-1].upper()
            meta = collector.get_table_metadata(table_name)
            if meta:
                snapshot[table_name] = meta
        return snapshot

    async def run(self, product_id: str, force: bool = False):
        if product_id not in self.config.products:
            raise ValueError(f"未知产品: {product_id}")

        product_cfg = self.config.products[product_id]
        # 获取唯一的进度对象
        progress = self.get_progress(product_id)
        
        # 启动前重置本次任务的计数
        progress.status = AnalysisStatus.RUNNING
        progress.completed = 0
        progress.failed = 0
        progress.errors = []
        progress.started_at = datetime.now()
        progress.current_sql = None
        
        try:
            collector = MetadataCollector(product_cfg.database, product_cfg.metadata_dir)
            if not collector.load_index(): await collector.collect_all()
            
            statements = scan_product_sqls(product_cfg.sql_dir, product_id)
            progress.total = len(statements)
            desc_dir = Path(product_cfg.desc_dir)
            desc_dir.mkdir(parents=True, exist_ok=True)

            for i, stmt in enumerate(statements, 1):
                progress.current_sql = stmt.id
                output_file = desc_dir / f"{stmt.id}.json"
                
                if not force and output_file.exists():
                    progress.completed += 1
                    continue

                logger.info(f"[{i}/{len(statements)}] Processing: {stmt.id}")
                db_context = self._build_metadata_context(stmt, collector)
                
                try:
                    prompt = f"Analyze SQL: {stmt.raw_sql}\nMetadata: {db_context}"
                    analysis = await self.agent.execute_task(prompt, SQLAnalysis)
                    
                    record = SQLRecord(
                        statement=stmt, 
                        analysis=analysis, 
                        status=AnalysisStatus.SUCCESS,
                        analyzed_at=datetime.now(),
                        metadata_snapshot=self._get_snapshot(stmt, collector),
                    )
                    
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
                    
                    # 实实在在的加 1
                    progress.completed += 1
                except Exception as e:
                    logger.error(f"Error {stmt.id}: {e}")
                    record = SQLRecord(
                        statement=stmt,
                        status=AnalysisStatus.FAILED,
                        analyzed_at=datetime.now(),
                        error_message=str(e),
                    )
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
                    progress.failed += 1
                    progress.errors.append(f"{stmt.id}: {e}")

            progress.status = AnalysisStatus.COMPLETED
            progress.current_sql = None
            # 索引更新
            from anysql.harness.tasks import load_all_records
            all_records = load_all_records(product_cfg.desc_dir)
            await self.vector.index_records([r for r in all_records if r.status == AnalysisStatus.SUCCESS], product_id)
            
        except Exception as e:
            logger.error(f"Pipeline crashed: {e}")
            progress.status = AnalysisStatus.FAILED
            progress.errors.append(str(e))
        finally:
            if progress.started_at:
                progress.elapsed_seconds = (datetime.now() - progress.started_at).total_seconds()
