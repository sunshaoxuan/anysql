"""RQ worker tasks for AnySQL."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from sqlalchemy import or_, select

from anysql.config import load_config
from anysql.core.agent_engine import AgentEngine
from anysql.core.llm_client import LLMClient
from anysql.core.metadata_collector import MetadataCollector
from anysql.core.sql_parser import scan_product_sqls
from anysql.harness.pipeline import AnalysisPipeline
from anysql.models.schemas import AnalysisStatus
from anysql.storage.database import Database
from anysql.storage.models import MetadataColumn, MetadataTable, MetadataTableProfile, SQLRecordRow
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.rag_repository import KnowledgeGapRepository, RagRepository
from anysql.storage.repositories import (
    JobRepository,
    MetadataRepository,
    ProductRepository,
    SQLKnowledgeRepository,
    TableProfileRepository,
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
            profiled = await TableProfileRepository(session).rebuild_auto_with_llm(product.id, llm)
            indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_metadata(product.id, commit_each_batch=True)
            rag = RagRepository(session)
            rag_nodes = rag.rebuild_metadata_nodes(product.id)
            session.commit()
            rag_indexed = await rag.index_nodes(product.id, llm, config.llm.embed_model, commit_each_batch=True)
            result = {
                "table_count": total or (index or {}).get("table_count", 0),
                "changed": changed,
                "profiled_count": profiled,
                "indexed_count": indexed,
                "rag_node_count": rag_nodes,
                "rag_indexed_count": rag_indexed,
            }
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
            indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_records(ok_records, product.id, commit_each_batch=True)
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
            sql_indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_records(records, product.id, commit_each_batch=True)
            metadata_indexed = await PGVectorRepository(session, llm, config.llm.embed_model).index_metadata(product.id, commit_each_batch=True)
            rag = RagRepository(session)
            rag_nodes = rag.rebuild_metadata_nodes(product.id)
            session.commit()
            accepted_rows = list(session.scalars(select(SQLRecordRow).where(
                SQLRecordRow.product_id == product.id,
                SQLRecordRow.learned.is_(True),
            )))
            for row in accepted_rows:
                record = SQLKnowledgeRepository.to_schema(row)
                rag.upsert_accepted_sql_nodes(product.id, record, row.comment or (record.analysis.summary if record.analysis else ""))
            rag_indexed = await rag.index_nodes(product.id, llm, config.llm.embed_model, commit_each_batch=True)
            result = {"sql_indexed": sql_indexed, "metadata_indexed": metadata_indexed, "rag_node_count": rag_nodes, "rag_indexed_count": rag_indexed}
            jobs.mark_succeeded(job_id, result)
            return result
    except Exception as exc:
        with db.session() as session:
            JobRepository(session).mark_failed(job_id, str(exc))
        raise
    finally:
        await llm.close()


def knowledge_gap_analysis(gap_id: str) -> dict:
    return asyncio.run(_knowledge_gap_analysis(gap_id))


async def _knowledge_gap_analysis(gap_id: str) -> dict:
    config, db, llm = _runtime()
    try:
        with db.session() as session:
            repo = KnowledgeGapRepository(session)
            repo.mark_running(gap_id)
            gap = repo.get(gap_id)
            if not gap:
                raise ValueError(f"Knowledge gap not found: {gap_id}")
            cleared = repo.clear_proposed_candidates(gap_id)
            repo.update_progress(
                gap_id,
                "term_mining",
                15,
                "Extracting business terms from the failed request.",
                {"requirement": gap.requirement, "triggers": gap.trigger_reasons, "cleared_candidates": cleared},
                status="running",
            )
            session.commit()
            terms = _mine_gap_terms(gap.requirement, gap.validation_result, gap.evidence_snapshot)
            repo.update_progress(
                gap_id,
                "term_candidates",
                30,
                "Expanded business terms into metadata language.",
                {"terms": [{"term": term, "synonyms": synonyms, "domain": domain} for term, synonyms, domain in terms]},
                status="running",
            )
            session.commit()
            for term, synonyms, domain in terms:
                repo.upsert_candidate(
                    gap,
                    "business_term",
                    term,
                    {"term": term, "synonyms": synonyms, "domain": domain, "lang": "mixed"},
                    0.72,
                    "extracted from failed requirement and expanded to metadata language",
                )
            repo.update_progress(
                gap_id,
                "evidence_search",
                45,
                "Searching metadata for table and field evidence.",
                {"term_count": len(terms)},
                status="running",
            )
            session.commit()
            field_candidates = _explore_gap_fields(session, gap.product_id, terms)
            review_pack = _build_review_pack(gap.requirement, terms, field_candidates)
            repo.update_progress(
                gap_id,
                "evidence_scoring",
                65,
                "Scoring candidate tables and fields.",
                {
                    "candidate_tables": [item["table"] for item in field_candidates[:10]],
                    "candidate_count": len(field_candidates),
                    "review_pack": review_pack,
                },
                status="running",
            )
            session.commit()
            for item in field_candidates:
                repo.upsert_candidate(
                    gap,
                    "table_field_evidence",
                    f"{item['table']} / {', '.join(item['fields'][:6])}",
                    item,
                    float(item.get("confidence") or 0.0),
                    item.get("reason") or "metadata field evidence",
                )
            repo.update_progress(
                gap_id,
                "candidate_building",
                80,
                "Building review candidates for intent and predicate patterns.",
                {"field_candidate_count": len(field_candidates)},
                status="running",
            )
            session.commit()
            if terms:
                intent_name = _propose_intent_name(gap.requirement, terms)
                repo.upsert_candidate(
                    gap,
                    "intent",
                    intent_name,
                    {
                        "intent": intent_name,
                        "description": "Auto-proposed intent from knowledge gap; requires review before use.",
                        "terms": [term for term, _, _ in terms],
                        "review_pack": review_pack,
                    },
                    0.62 if field_candidates else 0.38,
                    "derived from gap terms and candidate metadata evidence",
                )
            for item in field_candidates[:5]:
                if item.get("fields"):
                    repo.upsert_candidate(
                        gap,
                        "predicate_pattern",
                        f"{item['table']} dependent/support predicate",
                        {
                            "table": item["table"],
                            "fields": item["fields"],
                            "review_pack": review_pack,
                            "pattern": "use reviewed dependent/support fields; aggregate only when rows represent child/dependent details",
                        },
                        min(0.7, float(item.get("confidence") or 0.0)),
                        "candidate predicate pattern from gap analysis",
                    )
            repo.update_progress(
                gap_id,
                "review_packaging",
                92,
                "Packaging proposed knowledge for human review.",
                {"candidate_count": len(field_candidates) + len(terms)},
                status="running",
            )
            session.commit()
            confidence = max([float(item.get("confidence") or 0.0) for item in field_candidates] + ([0.5] if terms else [0.0]))
            summary = {
                "terms": [term for term, _, _ in terms],
                "candidate_tables": [item["table"] for item in field_candidates[:10]],
                "candidate_count": len(field_candidates) + len(terms),
                "needs_review": True,
                "review_pack": review_pack,
            }
            repo.mark_proposed(gap_id, summary, confidence)
            return summary
    except Exception as exc:
        with db.session() as session:
            KnowledgeGapRepository(session).mark_failed(gap_id, str(exc))
        raise
    finally:
        await llm.close()


def _mine_gap_terms(requirement: str, validation_result: dict, evidence_snapshot: list) -> list[tuple[str, list[str], str]]:
    text = f"{requirement} {json.dumps(validation_result, ensure_ascii=False)} {json.dumps(evidence_snapshot, ensure_ascii=False)[:2000]}"
    terms: list[tuple[str, list[str], str]] = []
    if any(word in text for word in ("多子女", "子女", "子供", "扶养", "扶養", "家族", "親族", "児童")):
        terms.extend([
            ("多子女", ["複数子女", "複数子供", "子供人数", "児童人数", "扶養親族"], "employee"),
            ("扶养中", ["扶養中", "扶養", "扶養親族", "家族", "親族"], "employee"),
            ("子女个数", ["子供数", "児童数", "人数"], "employee"),
        ])
    seen = set()
    unique = []
    for term, synonyms, domain in terms:
        if term in seen:
            continue
        seen.add(term)
        unique.append((term, synonyms, domain))
    return unique


def _explore_gap_fields(session, product_id: str, terms: list[tuple[str, list[str], str]]) -> list[dict]:
    if not terms:
        return []
    ordered_keywords: list[str] = []
    for term, synonyms, _ in terms:
        ordered_keywords.append(term)
        ordered_keywords.extend(synonyms)
    ordered_keywords.extend(["家族", "親族", "扶養", "続柄", "子供", "児童", "FUYOU", "FUYO", "FUY", "KAZOKU", "FAMILY", "CHILD", "KODOMO", "JIDO", "ZOKUGARA"])
    weak_keywords = {"対象", "明細", "COUNT"}
    keywords = []
    seen_keywords = set()
    for keyword in ordered_keywords:
        if keyword in weak_keywords or keyword in seen_keywords:
            continue
        seen_keywords.add(keyword)
        keywords.append(keyword)
    filters = []
    for keyword in keywords[:48]:
        like = f"%{keyword}%"
        filters.extend([
            MetadataTable.table_name.ilike(like),
            MetadataTable.comment.ilike(like),
            MetadataColumn.column_name.ilike(like),
            MetadataColumn.comment.ilike(like),
        ])
    rows = list(session.execute(
        select(MetadataTable, MetadataColumn, MetadataTableProfile)
        .join(MetadataColumn, MetadataColumn.metadata_table_id == MetadataTable.id)
        .outerjoin(MetadataTableProfile, (MetadataTableProfile.product_id == MetadataTable.product_id) & (MetadataTableProfile.table_name == MetadataTable.table_name))
        .where(MetadataTable.product_id == product_id)
        .where(or_(*filters))
        .limit(5000)
    ).all())
    by_table: dict[str, dict] = {}
    dangerous = {"log", "work", "if_staging", "backup"}
    for table, column, profile in rows:
        role = getattr(profile, "role", "unknown") if profile else "unknown"
        if role in dangerous:
            continue
        table_text = f"{table.table_name} {table.comment or ''}".upper()
        column_text = f"{column.column_name} {column.comment or ''}".upper()
        haystack = f"{table_text} {column_text}"
        hits = [kw for kw in keywords if str(kw).upper() in haystack]
        strong_hits = [hit for hit in hits if hit not in weak_keywords]
        if not strong_hits:
            continue
        column_name = str(column.column_name or "").upper()
        field_strength = _gap_field_strength(table_text, column_text, column_name, strong_hits)
        if field_strength <= 0:
            continue
        item = by_table.setdefault(table.table_name, {
            "table": table.table_name,
            "comment": table.comment,
            "domain": getattr(profile, "domain", "unknown") if profile else "unknown",
            "role": role,
            "fields": [],
            "field_comments": {},
            "matched_terms": [],
            "strong_field_count": 0,
            "weak_field_count": 0,
            "penalty": _gap_table_penalty(table_text),
            "confidence": 0.0,
            "reason": "metadata terms matched",
        })
        if field_strength >= 2:
            item["strong_field_count"] += 1
        else:
            item["weak_field_count"] += 1
        item["fields"].append(column.column_name)
        item["field_comments"][column.column_name] = column.comment or ""
        item["matched_terms"].extend(strong_hits)
    candidates = []
    for item in by_table.values():
        fields = list(dict.fromkeys(item["fields"]))
        matched_terms = list(dict.fromkeys(item["matched_terms"]))
        strong_count = int(item.get("strong_field_count") or 0)
        if strong_count == 0:
            continue
        confidence = (
            0.28
            + min(0.32, strong_count * 0.08)
            + min(0.18, len(matched_terms) * 0.025)
            + min(0.12, len(fields) * 0.012)
            - float(item.get("penalty") or 0.0)
        )
        confidence = max(0.15, min(0.93, confidence))
        if confidence < 0.48:
            continue
        item["fields"] = fields
        item["matched_terms"] = matched_terms
        item["confidence"] = round(confidence, 4)
        item["reason"] = f"{strong_count} strong dependent/support field markers"
        candidates.append(item)
    return sorted(candidates, key=lambda row: (-float(row["confidence"]), row["table"]))[:20]


def _propose_intent_name(requirement: str, terms: list[tuple[str, list[str], str]]) -> str:
    if any(term in {"多子女", "扶养中", "子女个数"} for term, _, _ in terms):
        return "employee_dependent_children"
    return "auto_proposed_intent"


def _build_review_pack(requirement: str, terms: list[tuple[str, list[str], str]], candidates: list[dict]) -> dict:
    proposed_intent = _propose_intent_name(requirement, terms) if terms else "auto_proposed_intent"
    scored = []
    blocked = []
    for item in candidates:
        row = dict(item)
        table_text = f"{row.get('table') or ''} {row.get('comment') or ''}".upper()
        blocked_reason = _review_block_reason(table_text, row)
        business_score = _review_business_score(table_text, row)
        row["business_score"] = round(business_score, 4)
        if blocked_reason:
            row["blocked_reason"] = blocked_reason
            blocked.append(row)
        else:
            scored.append(row)
    scored.sort(key=lambda row: (-float(row.get("business_score") or 0.0), -float(row.get("confidence") or 0.0), str(row.get("table") or "")))
    blocked.sort(key=lambda row: (-float(row.get("confidence") or 0.0), str(row.get("table") or "")))
    primary = scored[0] if scored and float(scored[0].get("business_score") or 0.0) >= 0.52 else None
    alternatives = scored[1:6] if primary else scored[:6]
    recommended_fields = []
    source_tables = ([primary] if primary else []) + alternatives[:3]
    for item in source_tables:
        for field in (item.get("fields") or [])[:10]:
            label = {"table": item.get("table"), "column": field, "comment": (item.get("field_comments") or {}).get(field, "")}
            if label not in recommended_fields:
                recommended_fields.append(label)
    missing = []
    if not primary:
        missing.append("primary_table")
    if len(recommended_fields) < 2:
        missing.append("dependent_child_business_fields")
    if proposed_intent == "auto_proposed_intent":
        missing.append("intent")
    return {
        "proposed_intent": proposed_intent,
        "intent_confidence": 0.76 if proposed_intent == "employee_dependent_children" and primary else 0.45,
        "primary_table": _pack_table(primary) if primary else None,
        "alternative_tables": [_pack_table(row) for row in alternatives],
        "blocked_tables": [_pack_table(row, blocked=True) for row in blocked[:12]],
        "recommended_fields": recommended_fields[:24],
        "predicate_hints": _predicate_hints(proposed_intent, primary),
        "aggregation_hints": _aggregation_hints(proposed_intent, primary),
        "missing_evidence": missing,
        "needs_manual_input": bool(missing),
        "review_recommendation": _review_recommendation(primary, missing),
    }


def _review_business_score(table_text: str, item: dict) -> float:
    score = float(item.get("confidence") or 0.0)
    role = str(item.get("role") or "")
    domain = str(item.get("domain") or "")
    if domain == "employee":
        score += 0.12
    if role in {"history_fact", "master", "business_fact"}:
        score += 0.1
    if any(marker in table_text for marker in ("家族情報", "扶養家族", "扶養親族", "家族", "親族")):
        score += 0.22
    if any(marker in table_text for marker in ("履歴", "歴")):
        score += 0.08
    if any(marker in table_text for marker in ("給与", "手当", "期末勤勉", "年末調整", "年調", "保険", "控除", "申告書", "XML", "APP", "KARI_", "入力", "取込")):
        score -= 0.32
    return max(0.0, min(1.0, score))


def _review_block_reason(table_text: str, item: dict) -> str:
    role = str(item.get("role") or "")
    domain = str(item.get("domain") or "")
    if role in {"log", "work", "if_staging", "backup"}:
        return f"role={role}"
    if domain == "payroll":
        return "payroll_source_requires_explicit_review"
    table_name = str(item.get("table") or "").upper()
    if table_name.startswith(("WK", "VWK")) or "_WK" in table_name:
        return "work_like_candidate"
    if any(marker in table_text for marker in ("給与", "手当", "期末勤勉", "年末調整", "年調", "保険", "控除", "申告書", "XML", "APP", "KARI_", "入力", "取込", "退職手当")):
        return "low_priority_or_non_primary_business_source"
    return ""


def _pack_table(item: dict | None, blocked: bool = False) -> dict | None:
    if not item:
        return None
    result = {
        "table": item.get("table"),
        "comment": item.get("comment") or "",
        "domain": item.get("domain") or "unknown",
        "role": item.get("role") or "unknown",
        "confidence": item.get("confidence") or 0.0,
        "business_score": item.get("business_score", item.get("confidence") or 0.0),
        "reason": item.get("reason") or "",
        "fields": (item.get("fields") or [])[:12],
    }
    if blocked:
        result["blocked_reason"] = item.get("blocked_reason") or ""
    return result


def _predicate_hints(intent: str, primary: dict | None) -> list[str]:
    if intent == "employee_dependent_children":
        hints = ["optional surname filter should use a name field only when the user provides a surname"]
        if primary:
            hints.append(f"use fields from {primary.get('table')} only after review approval")
        return hints
    return []


def _aggregation_hints(intent: str, primary: dict | None) -> list[str]:
    if intent == "employee_dependent_children":
        return ["group by employee number/name when counting children or dependents", "count only rows/fields that evidence child or dependent relationship"]
    return []


def _review_recommendation(primary: dict | None, missing: list[str]) -> str:
    if missing:
        return "Needs manual review: choose a primary table/fields before this knowledge can guide SQL generation."
    return f"Approve if {primary.get('table')} is the official family/dependent evidence source for this product."


def _gap_field_strength(table_text: str, column_text: str, column_name: str, hits: list[str]) -> int:
    """Score whether a metadata field is real dependent/support evidence, not just an employee key."""
    if column_name in {"CCOMPKB", "CQTAIKEIKB", "NGAITONEN", "CSHAINNO", "CMNCLIENT", "CMNCOMP", "CMNUSER", "DMNDATE", "NSEQ"}:
        return 0
    text = f"{table_text} {column_text}"
    score = 0
    if any(marker in column_text for marker in ("扶養", "扶养", "親族", "家族", "児童", "子供", "子女", "CHILD", "KODOMO", "JIDO", "FUYO", "FUYOU", "KAZOKU", "ZOKUGARA")):
        score += 2
    if any(marker in table_text for marker in ("扶養親族", "扶養控除", "児童", "家族", "親族")):
        score += 1
    if any(marker in column_text for marker in ("人数", "人員", "数", "COUNT", "NINZU")):
        score += 1
    if hits:
        score += 1
    return score


def _gap_table_penalty(table_text: str) -> float:
    penalty = 0.0
    if any(marker in table_text for marker in ("採用者給与", "給与", "手当", "期末勤勉", "年調", "保険", "控除", "退職手当", "XML", "申告書ﾃﾞｰﾀ", "申告書データ", "APP", "取込", "入力")):
        penalty += 0.18
    if any(marker in table_text for marker in ("KARI_", "仮", "一時", "WK", "WORK")):
        penalty += 0.12
    return penalty
