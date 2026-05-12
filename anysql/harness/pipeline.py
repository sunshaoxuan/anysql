"""
AnySQL Harness 管线 (实时计数修正版)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re

from anysql.config import AppConfig
from anysql.core.agent_engine import AgentEngine
from anysql.core.llm_client import LLMClient
from anysql.core.sql_parser import _detect_type, _extract_tables, scan_product_sqls
from anysql.core.sql_cleaner import add_sql_header_comment, clean_generated_sql, strip_sql_comments
from anysql.core.vector_engine import VectorEngine
from anysql.core.metadata_collector import MetadataCollector
from anysql.logger import logger
from anysql.models.schemas import (
    AnalysisProgress,
    AnalysisStatus,
    SQLCandidateMatch,
    SQLRecord,
    SQLStatement,
    SQLAnalysis,
    GeneratedSQL,
    SearchResult,
)


class AnalysisPipeline:
    def __init__(self, config: AppConfig, llm: LLMClient, vector_engine: VectorEngine, agent: AgentEngine):
        self.config = config
        self.llm = llm
        self.vector = vector_engine
        self.agent = agent
        # 核心状态存储：确保全局唯一且持久
        self._progress_map: dict[str, AnalysisProgress] = {}
        self._metadata_sync_status: dict[str, dict] = {}

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

    async def _normalize_query_to_japanese(self, text: str) -> str:
        """把用户输入规范成日文检索意图，贴近元数据语言。"""
        prompt = f"""次のSQL検索/生成要求を、日本語の業務検索クエリに変換してください。
元の意味を保ち、テーブル名・項目名・業務用語の候補を日本語中心に補ってください。
JSONだけ返してください: {{"query_ja": "..."}}

入力:
{text}
"""
        try:
            data = await self.llm.chat_json(prompt, system_prompt="日本語SQL検索クエリ変換器。JSONのみ返す。", temperature=0.1)
            query_ja = str(data.get("query_ja", "")).strip()
            return query_ja or text
        except Exception as e:
            logger.warning(f"日文查询规范化失败，使用原文: {e}")
            return text

    @staticmethod
    def _draft_record(product_id: str, requirement: str, generated: GeneratedSQL) -> SQLRecord:
        executable_sql = clean_generated_sql(generated.sql)
        display_sql = add_sql_header_comment(executable_sql, generated.summary, requirement)
        tables = [t.upper() for t in generated.tables] or _extract_tables(executable_sql)
        stmt = SQLStatement(
            id=f"{product_id}_draft_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            product=product_id,
            source_file="draft.sql",
            raw_sql=display_sql,
            comment=f"Draft requirement: {requirement}",
            tables=tables,
            statement_type=_detect_type(strip_sql_comments(executable_sql)),
            line_number=0,
        )
        analysis = SQLAnalysis(
            summary=generated.summary or "SQL draft",
            business_context=[generated.business_meaning] if generated.business_meaning else [],
            tables_involved={t: "" for t in tables},
            usage_guide=generated.usage_guide,
            parameters=generated.parameters,
            keywords=tables,
        )
        return SQLRecord(
            statement=stmt,
            analysis=analysis,
            status=AnalysisStatus.PENDING,
            analyzed_at=datetime.now(),
        )

    @staticmethod
    def _record_to_generated(record: SQLRecord) -> GeneratedSQL:
        analysis = record.analysis
        return GeneratedSQL(
            sql=record.statement.raw_sql,
            summary=analysis.summary if analysis else record.statement.comment,
            business_meaning=" / ".join(analysis.business_context) if analysis else "",
            usage_guide=analysis.usage_guide if analysis else "",
            parameters=analysis.parameters if analysis else [],
            tables=record.statement.tables,
        )

    @staticmethod
    def _next_generated_id(product_id: str, desc_dir: Path) -> str:
        existing = []
        for path in desc_dir.glob(f"{product_id}_generated_*.json"):
            match = re.search(r"_(\d{4})\.json$", path.name)
            if match:
                existing.append(int(match.group(1)))
        return f"{product_id}_generated_{(max(existing, default=0) + 1):04d}"

    @staticmethod
    def _append_generated_sql(sql_dir: Path, stmt_id: str, requirement: str, sql: str) -> tuple[str, int]:
        sql_dir.mkdir(parents=True, exist_ok=True)
        path = sql_dir / "generated.sql"
        prefix = "\n\n" if path.exists() and path.stat().st_size > 0 else ""
        existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        line_number = len(existing_lines) + (3 if prefix else 2)
        block = (
            f"{prefix}-- Generated by AnySQL: {stmt_id}\n"
            f"-- Requirement: {requirement}\n"
            f"{sql.rstrip().rstrip(';')};\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
        return path.name, line_number

    async def _persist_generated_knowledge(
        self,
        product_id: str,
        requirement: str,
        generated: GeneratedSQL,
    ) -> SQLRecord:
        if product_id not in self.config.products:
            raise ValueError(f"未知产品: {product_id}")

        product_cfg = self.config.products[product_id]
        collector = MetadataCollector(product_cfg.database, product_cfg.metadata_dir)
        if not collector.load_index():
            await collector.collect_all()

        sql = generated.sql.strip()
        if not sql:
            raise ValueError("LLM 未生成 SQL")

        desc_dir = Path(product_cfg.desc_dir)
        desc_dir.mkdir(parents=True, exist_ok=True)
        stmt_id = self._next_generated_id(product_id, desc_dir)
        source_file, line_number = self._append_generated_sql(
            Path(product_cfg.sql_dir),
            stmt_id,
            requirement,
            sql,
        )

        tables = [t.upper() for t in generated.tables] or _extract_tables(sql)
        stmt = SQLStatement(
            id=stmt_id,
            product=product_id,
            source_file=source_file,
            raw_sql=sql.rstrip().rstrip(";") + ";",
            comment=f"Generated requirement: {requirement}",
            tables=tables,
            statement_type=_detect_type(sql),
            line_number=line_number,
        )

        metadata_context = self._build_metadata_context(stmt, collector)
        analysis_prompt = (
            f"Analyze this generated SQL and explain how to use it.\n"
            f"Requirement: {requirement}\n"
            f"SQL: {stmt.raw_sql}\n"
            f"Generated summary: {generated.summary}\n"
            f"Generated business meaning: {generated.business_meaning}\n"
            f"Generated usage guide: {generated.usage_guide}\n"
            f"Metadata: {metadata_context}"
        )
        analysis = await self.agent.execute_task(analysis_prompt, SQLAnalysis)
        if generated.parameters and not analysis.parameters:
            analysis.parameters = generated.parameters

        record = SQLRecord(
            statement=stmt,
            analysis=analysis,
            status=AnalysisStatus.SUCCESS,
            analyzed_at=datetime.now(),
            metadata_snapshot=self._get_snapshot(stmt, collector),
        )

        output_file = desc_dir / f"{record.statement.id}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

        await self.vector.index_records([record], product_id)

        progress = self.get_progress(product_id)
        progress.total = len(scan_product_sqls(product_cfg.sql_dir, product_id))
        progress.completed = len(list(desc_dir.glob("*.json")))

        logger.info(f"人工确认SQL已沉淀为知识: {record.statement.id}")
        return record

    async def generate_and_learn(self, product_id: str, requirement: str, top_k: int = 5) -> SQLRecord:
        if product_id not in self.config.products:
            raise ValueError(f"未知产品: {product_id}")

        product_cfg = self.config.products[product_id]
        collector = MetadataCollector(product_cfg.database, product_cfg.metadata_dir)
        if not collector.load_index():
            await collector.collect_all()

        query_ja = await self._normalize_query_to_japanese(requirement)
        similar = await self.vector.search(query_ja, product=product_id, top_k=top_k)
        metadata_hits = await self.vector.search_metadata(query_ja, product_id, top_k=8)
        if not metadata_hits:
            await self.vector.index_metadata(product_id, product_cfg.metadata_dir)
            metadata_hits = await self.vector.search_metadata(query_ja, product_id, top_k=8)
        examples = []
        for item in similar:
            examples.append(
                f"- {item.summary}\n"
                f"  SQL: {item.raw_sql}\n"
                f"  Context: {' / '.join(item.business_context)}"
            )

        index = collector.load_index() or {}
        table_names = index.get("table_names", [])
        table_hint = ", ".join(table_names[:200]) if isinstance(table_names, list) else ""
        product_rules = product_cfg.rules.strip() or "No product-specific rules."
        metadata_context = "\n\n".join(
            f"- {m['table']} score={m['score']}\n{m['document']}"
            for m in metadata_hits
        ) or "No metadata vector hits."

        prompt = f"""你要为产品 {product_id} 生成一段满足业务需求的 SQL。

业务需求:
{requirement}

日文检索意图（元数据语言）:
{query_ja}

产品规则（必须遵守，优先级高于一般推断）:
{product_rules}

可用表名候选（只展示前200个，必要时根据相似SQL推断）:
{table_hint or "No metadata index available."}

Metadata RAG 候选（优先参考）:
{metadata_context}

相似SQL知识:
{chr(10).join(examples) if examples else "No similar SQL knowledge found."}

要求:
1. 优先复用相似SQL中的表、字段、日期条件和命名习惯。
2. 如果需要参数，用清晰占位符，例如 :employee_no 或 :target_date。
3. 输出 SQL 的用途、参数用法、业务含义、涉及表和假设。
4. 只返回 JSON，不要返回 Markdown。
"""
        generated = await self.agent.execute_task(prompt, GeneratedSQL)
        return self._draft_record(product_id, requirement, generated)

    async def _llm_match_candidates(
        self,
        product_id: str,
        requirement: str,
        candidates: list[SearchResult],
    ) -> list[SQLCandidateMatch]:
        if not candidates:
            return []

        candidate_text = "\n\n".join(
            f"ID: {c.sql_id}\nVector score: {c.score}\nSummary: {c.summary}\nSQL: {c.raw_sql}"
            for c in candidates
        )
        product_rules = self.config.products[product_id].rules.strip() or "No product-specific rules."
        prompt = f"""请判断候选 SQL 是否满足用户需求，并给每个候选打 0 到 1 的匹配分。

用户需求:
{requirement}

产品规则（必须用于判断候选是否可用）:
{product_rules}

候选:
{candidate_text}

返回 JSON:
{{
  "matches": [
    {{"sql_id": "...", "llm_score": 0.0, "reason": "简短理由"}}
  ]
}}
"""
        data = await self.llm.chat_json(
            prompt=prompt,
            system_prompt="你是 SQL 匹配评审助手，只返回严格 JSON。",
            temperature=0.1,
        )
        llm_by_id = {
            item.get("sql_id"): item
            for item in data.get("matches", [])
            if isinstance(item, dict)
        }

        matches: list[SQLCandidateMatch] = []
        for candidate in candidates:
            item = llm_by_id.get(candidate.sql_id, {})
            try:
                llm_score = float(item.get("llm_score", candidate.score))
            except (TypeError, ValueError):
                llm_score = candidate.score
            matches.append(SQLCandidateMatch(
                sql_id=candidate.sql_id,
                vector_score=candidate.score,
                llm_score=max(0, min(1, llm_score)),
                reason=str(item.get("reason", "")),
            ))
        matches.sort(key=lambda m: m.llm_score, reverse=True)
        return matches

    async def revise_and_learn(
        self,
        product_id: str,
        requirement: str,
        current_sql: str,
    ) -> SQLRecord:
        product_rules = self.config.products[product_id].rules.strip() or "No product-specific rules."
        prompt = f"""请根据用户修正意见改写 SQL。

当前 SQL:
{current_sql}

用户修正意见:
{requirement}

产品规则（必须遵守，优先级高于一般推断）:
{product_rules}

要求:
1. 保留原 SQL 的产品命名习惯和表字段风格。
2. 输出改写后的 SQL、用途、参数用法、业务含义、涉及表和假设。
3. 只返回 JSON。
"""
        generated = await self.agent.execute_task(prompt, GeneratedSQL)
        return self._draft_record(product_id, requirement, generated)

    async def learn_confirmed_sql(
        self,
        product_id: str,
        requirement: str,
        generated: GeneratedSQL,
    ) -> SQLRecord:
        """人工确认后，将草稿 SQL 落盘并进入向量知识库。"""
        return await self._persist_generated_knowledge(product_id, requirement, generated)

    async def sync_product_metadata(self, product_id: str) -> dict:
        """从产品数据库差异采集 Metadata，并更新 Metadata 向量库。"""
        if product_id not in self.config.products:
            raise ValueError(f"未知产品: {product_id}")

        started_at = datetime.now()
        self._metadata_sync_status[product_id] = {
            "status": "running",
            "product": product_id,
            "started_at": started_at.isoformat(),
        }
        try:
            product_cfg = self.config.products[product_id]
            collector = MetadataCollector(product_cfg.database, product_cfg.metadata_dir)
            index = await collector.collect_all(use_cache=False)
            indexed_count = await self.vector.index_metadata(product_id, product_cfg.metadata_dir)
            result = {
                "status": "synced",
                "product": product_id,
                "indexed_count": indexed_count,
                "table_count": (index or {}).get("table_count", 0),
                "elapsed_seconds": round((datetime.now() - started_at).total_seconds(), 2),
            }
            self._metadata_sync_status[product_id] = result
            return result
        except Exception as e:
            logger.error(f"产品Metadata同步失败 {product_id}: {e}")
            result = {
                "status": "failed",
                "product": product_id,
                "error": str(e),
                "elapsed_seconds": round((datetime.now() - started_at).total_seconds(), 2),
            }
            self._metadata_sync_status[product_id] = result
            return result

    async def sync_all_metadata(self) -> list[dict]:
        """按产品顺序执行每日 Metadata 差异同步。"""
        results = []
        for product_id in list(self.config.products.keys()):
            results.append(await self.sync_product_metadata(product_id))
        return results

    def get_metadata_sync_status(self, product_id: str) -> dict:
        return self._metadata_sync_status.get(product_id, {
            "status": "idle",
            "product": product_id,
        })

    async def assist_sql(
        self,
        product_id: str,
        message: str,
        current_sql: str | None = None,
        top_k: int = 5,
        match_threshold: float = 0.78,
    ) -> tuple[str, SQLRecord, list[SQLCandidateMatch], bool]:
        if current_sql:
            record = await self.revise_and_learn(product_id, message, current_sql)
            return "revised", record, [], False

        query_ja = await self._normalize_query_to_japanese(message)
        candidates = await self.vector.search(query_ja, product=product_id, top_k=top_k)
        matches = await self._llm_match_candidates(product_id, f"{message}\n日文检索意图: {query_ja}", candidates)
        best = matches[0] if matches else None
        if best and best.llm_score >= match_threshold:
            from anysql.harness.tasks import load_all_records
            pcfg = self.config.products[product_id]
            records = load_all_records(pcfg.desc_dir)
            for record in records:
                if record.statement.id == best.sql_id:
                    return "matched", record, matches, False

        record = await self.generate_and_learn(product_id, message, top_k=top_k)
        return "generated", record, matches, False

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
            await self.vector.index_metadata(product_id, product_cfg.metadata_dir)
            
        except Exception as e:
            logger.error(f"Pipeline crashed: {e}")
            progress.status = AnalysisStatus.FAILED
            progress.errors.append(str(e))
        finally:
            if progress.started_at:
                progress.elapsed_seconds = (datetime.now() - progress.started_at).total_seconds()
