"""Repositories for multi-dimensional RAG nodes and agent audit state."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from anysql.core.sql_cleaner import strip_sql_comments
from anysql.models.schemas import SQLRecord
from anysql.storage.models import (
    AgentRun,
    AgentStep,
    FeedbackEvent,
    JoinEdge,
    KnowledgeCandidate,
    KnowledgeGap,
    MetadataColumn,
    MetadataTable,
    MetadataTableProfile,
    Product,
    RagEmbedding,
    RagNode,
    RagTerm,
)


METADATA_NODE_TYPES = {
    "table_profile",
    "table_semantic",
    "column_semantic",
    "column_stat",
    "predicate_pattern",
    "business_term",
    "join_edge",
}


def stable_content_hash(content: object) -> str:
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RagRepository:
    def __init__(self, session: Session):
        self.session = session

    def stats(self, product_id: str) -> dict:
        node_rows = self.session.execute(
            select(RagNode.facet, func.count(RagNode.id)).where(RagNode.product_id == product_id).group_by(RagNode.facet)
        ).all()
        embedding_count = self.session.scalar(select(func.count(RagEmbedding.id)).where(RagEmbedding.product_id == product_id)) or 0
        latest = self.session.scalar(select(func.max(RagNode.updated_at)).where(RagNode.product_id == product_id))
        return {
            "nodes": {facet: count for facet, count in node_rows},
            "total_nodes": sum(count for _, count in node_rows),
            "embeddings": embedding_count,
            "updated_at": latest.isoformat() if latest else None,
        }

    def rebuild_metadata_nodes(self, product_id: str) -> int:
        stale = select(RagNode.id).where(RagNode.product_id == product_id, RagNode.source_type.in_(METADATA_NODE_TYPES))
        self.session.execute(delete(RagEmbedding).where(RagEmbedding.node_id.in_(stale)))
        self.session.execute(delete(RagNode).where(RagNode.product_id == product_id, RagNode.source_type.in_(METADATA_NODE_TYPES)))

        count = 0
        tables = list(self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id)))
        for table in tables:
            profile = self.session.scalar(select(MetadataTableProfile).where(
                MetadataTableProfile.product_id == product_id,
                MetadataTableProfile.table_name == table.table_name,
            ))
            columns = list(self.session.scalars(select(MetadataColumn).where(MetadataColumn.metadata_table_id == table.id)))
            count += self._upsert_node(
                product_id,
                "table_semantic",
                table.table_name,
                "table_semantic",
                _table_content(table, profile),
                {"table": table.table_name, "comment": table.comment, "domain": getattr(profile, "domain", "unknown"), "role": getattr(profile, "role", "unknown")},
                weight=1.0,
            )
            if profile:
                count += self._upsert_node(
                    product_id,
                    "table_profile",
                    table.table_name,
                    "table_profile",
                    f"Table {table.table_name}: domain={profile.domain}; role={profile.role}; confidence={profile.confidence}; reason={profile.reason}; source={profile.source}",
                    {"table": table.table_name, "domain": profile.domain, "role": profile.role, "source": profile.source},
                    weight=1.4 if profile.source == "manual" else 1.15,
                )
            for chunk_index, chunk in enumerate(_column_chunks(columns, 40)):
                count += self._upsert_node(
                    product_id,
                    "column_semantic",
                    f"{table.table_name}.columns.{chunk_index}",
                    "column_semantic",
                    _column_chunk_content(table, chunk, profile),
                    {
                        "table": table.table_name,
                        "columns": [column.column_name for column in chunk],
                        "chunk_index": chunk_index,
                        "domain": getattr(profile, "domain", "unknown"),
                        "role": getattr(profile, "role", "unknown"),
                    },
                    weight=1.25,
                )
            for column in columns:
                stat_content, stat_meta = _column_stat_content(table, column)
                predicate = _predicate_pattern(table, column)
                if predicate:
                    count += self._upsert_node(
                        product_id,
                        "predicate_pattern",
                        f"{table.table_name}.{column.column_name}.{predicate['kind']}",
                        "predicate_pattern",
                        predicate["content"],
                        predicate,
                        weight=1.25,
                    )
        count += self._seed_terms(product_id)
        self.session.flush()
        return count

    def upsert_accepted_sql_nodes(
        self,
        product_id: str,
        record: SQLRecord,
        requirement: str,
        run_id: str | None = None,
        accepted_by: str = "system",
    ) -> int:
        sql_id = record.statement.id
        raw_sql = record.statement.raw_sql
        tables = [table.upper() for table in record.statement.tables]
        analysis = record.analysis
        summary = analysis.summary if analysis else ""
        meaning = " / ".join(analysis.business_context) if analysis else ""
        params = analysis.parameters if analysis else []
        where_text = _where_fragment(raw_sql)
        count = 0
        count += self._upsert_node(
            product_id,
            "sql_intent",
            sql_id,
            "sql_intent",
            f"Requirement: {requirement}\nSummary: {summary}\nBusiness meaning: {meaning}\nParameters: {', '.join(params)}",
            {"sql_id": sql_id, "requirement": requirement, "summary": summary, "tables": tables, "run_id": run_id},
            weight=1.8,
        )
        count += self._upsert_node(
            product_id,
            "sql_structure",
            sql_id,
            "sql_structure",
            f"Accepted SQL structure\nTables: {', '.join(tables)}\nWhere: {where_text}\nSQL:\n{strip_sql_comments(raw_sql)}",
            {"sql_id": sql_id, "tables": tables, "where": where_text, "run_id": run_id},
            weight=1.5,
        )
        for predicate in _sql_predicates(raw_sql):
            count += self._upsert_node(
                product_id,
                "predicate_pattern",
                f"{sql_id}.{stable_content_hash(predicate)[:12]}",
                "predicate_pattern",
                f"Accepted predicate pattern for {requirement}: {predicate}",
                {"sql_id": sql_id, "predicate": predicate, "tables": tables, "run_id": run_id},
                weight=1.45,
            )
        count += self._upsert_node(
            product_id,
            "feedback",
            sql_id,
            "feedback_node",
            f"Positive feedback accepted SQL.\nRequirement: {requirement}\nTables: {', '.join(tables)}\nSummary: {summary}",
            {"sql_id": sql_id, "requirement": requirement, "tables": tables, "run_id": run_id, "accepted_by": accepted_by},
            weight=2.0,
        )
        self.session.add(FeedbackEvent(
            product_id=product_id,
            run_id=run_id,
            sql_id=sql_id,
            requirement=requirement,
            feedback_type="accepted",
            weight=2.0,
            payload={"tables": tables, "summary": summary},
            created_by=accepted_by,
        ))
        self.session.flush()
        return count

    async def index_nodes(self, product_id: str, llm, embedding_model: str, facets: set[str] | None = None, commit_each_batch: bool = False) -> int:
        query = select(RagNode).where(RagNode.product_id == product_id)
        if facets:
            query = query.where(RagNode.facet.in_(facets))
        nodes = list(self.session.scalars(query))
        pending: list[RagNode] = []
        for node in nodes:
            content_hash = stable_content_hash(node.content)
            node.content_hash = content_hash
            existing = self.session.scalar(select(RagEmbedding).where(
                RagEmbedding.node_id == node.id,
                RagEmbedding.embedding_model == embedding_model,
            ))
            if not existing or existing.content_hash != content_hash:
                pending.append(node)
        if not pending:
            self.session.flush()
            return 0
        indexed = 0
        for offset in range(0, len(pending), 32):
            batch = pending[offset:offset + 32]
            embeddings = await llm.embed([node.content for node in batch], model=embedding_model)
            for node, embedding in zip(batch, embeddings):
                if not embedding:
                    continue
                row = self.session.scalar(select(RagEmbedding).where(
                    RagEmbedding.node_id == node.id,
                    RagEmbedding.embedding_model == embedding_model,
                ))
                if not row:
                    row = RagEmbedding(node_id=node.id, product_id=product_id, facet=node.facet, embedding_model=embedding_model)
                    self.session.add(row)
                row.product_id = product_id
                row.facet = node.facet
                row.content_hash = node.content_hash
                row.embedding = embedding
                indexed += 1
            self.session.flush()
            if commit_each_batch:
                self.session.commit()
        return indexed

    async def hybrid_search(self, product_id: str, query: str, llm, embedding_model: str, top_k: int = 24) -> list[dict]:
        vector_results = await self._vector_search(product_id, query, llm, embedding_model, top_k=top_k)
        text_results = self._text_search(product_id, query, top_k=top_k)
        return _rrf_fuse([vector_results, text_results], top_k=top_k)

    async def _vector_search(self, product_id: str, query: str, llm, embedding_model: str, top_k: int) -> list[dict]:
        embedding = await llm.embed([query], model=embedding_model)
        if not embedding or not embedding[0]:
            return []
        rows = self.session.execute(text("""
            SELECT n.id, n.source_type, n.source_id, n.facet, n.content, n.meta, n.weight,
                   (1 - (e.embedding <=> CAST(:embedding AS vector))) AS score
            FROM rag_embeddings e
            JOIN rag_nodes n ON n.id = e.node_id
            WHERE e.product_id = :product_id AND e.embedding_model = :model
            ORDER BY e.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """), {"embedding": json.dumps(embedding[0]), "product_id": product_id, "model": embedding_model, "limit": top_k}).mappings().all()
        return [_row_to_evidence(row, "vector") for row in rows]

    def _text_search(self, product_id: str, query: str, top_k: int) -> list[dict]:
        terms = [term for term in set(_tokenize(query)) if len(term) >= 2]
        if not terms:
            return []
        clauses = []
        params: dict[str, object] = {"product_id": product_id, "limit": top_k}
        scores = []
        for idx, term in enumerate(terms[:12]):
            key = f"term_{idx}"
            raw = f"raw_{idx}"
            params[key] = f"%{term}%"
            params[raw] = term
            clauses.append(f"(n.content ILIKE :{key} OR n.source_id ILIKE :{key} OR similarity(n.content, :{raw}) > 0.12 OR similarity(n.source_id, :{raw}) > 0.18)")
            scores.append(f"GREATEST(similarity(n.content, :{raw}), similarity(n.source_id, :{raw}))")
        rows = self.session.execute(text(f"""
            SELECT n.id, n.source_type, n.source_id, n.facet, n.content, n.meta, n.weight,
                   GREATEST({', '.join(scores)}) AS score
            FROM rag_nodes n
            WHERE n.product_id = :product_id AND ({' OR '.join(clauses)})
            ORDER BY GREATEST({', '.join(scores)}) DESC, n.weight DESC
            LIMIT :limit
        """), params).mappings().all()
        return [_row_to_evidence(row, "text") for row in rows]

    def _seed_terms(self, product_id: str) -> int:
        terms = [
            ("异动", "zh", ["異動", "任免", "発令"], "transfer"),
            ("異動", "ja", ["异动", "任免", "発令"], "transfer"),
            ("基本信息", "zh", ["基本情報", "個人基本情報", "給与基本情報"], "employee"),
            ("氏名", "ja", ["姓名", "姓", "漢字氏名", "CNAMEKNJ"], "employee"),
            ("员工", "zh", ["社員", "職員", "職員番号", "社員番号"], "employee"),
        ]
        count = 0
        for term, lang, synonyms, domain in terms:
            row = self.session.scalar(select(RagTerm).where(RagTerm.product_id == product_id, RagTerm.term == term, RagTerm.lang == lang))
            if not row:
                row = RagTerm(product_id=product_id, term=term, lang=lang)
                self.session.add(row)
                count += 1
            row.synonyms = synonyms
            row.domain = domain
            row.source = "system"
            count += self._upsert_node(
                product_id,
                "business_term",
                f"{lang}:{term}",
                "business_term",
                f"Business term {term} ({lang}) means: {', '.join(synonyms)}. Domain: {domain}",
                {"term": term, "lang": lang, "synonyms": synonyms, "domain": domain},
                weight=1.25,
            )
        return count

    def _upsert_node(
        self,
        product_id: str,
        source_type: str,
        source_id: str,
        facet: str,
        content: str,
        meta: dict,
        weight: float = 1.0,
        lang: str = "ja",
    ) -> int:
        row = self.session.scalar(select(RagNode).where(
            RagNode.product_id == product_id,
            RagNode.source_type == source_type,
            RagNode.source_id == source_id,
            RagNode.facet == facet,
        ))
        content_hash = stable_content_hash(content)
        created = 0
        if not row:
            row = RagNode(product_id=product_id, source_type=source_type, source_id=source_id, facet=facet)
            self.session.add(row)
            created = 1
        row.lang = lang
        row.content = content
        row.meta = meta
        row.content_hash = content_hash
        row.weight = weight
        row.source_version = (row.source_version or 0) + 1
        return created

    def upsert_candidate_node(self, product_id: str, candidate: KnowledgeCandidate) -> int:
        payload = candidate.payload or {}
        candidate_type = candidate.candidate_type
        source_id = f"{candidate.id}:{candidate_type}"
        if candidate_type == "business_term":
            term = str(payload.get("term") or candidate.title)
            synonyms = payload.get("synonyms") or []
            content = f"Approved business term {term}: {', '.join(map(str, synonyms))}. Source gap: {candidate.gap_id}"
            meta = {"term": term, "synonyms": synonyms, "gap_id": candidate.gap_id, "candidate_id": candidate.id}
            return self._upsert_node(product_id, "knowledge_candidate", source_id, "business_term", content, meta, weight=1.55)
        if candidate_type == "intent":
            intent = str(payload.get("intent") or candidate.title)
            content = f"Approved intent candidate {intent}. Description: {payload.get('description') or ''}. Terms: {payload.get('terms') or []}"
            meta = {**payload, "gap_id": candidate.gap_id, "candidate_id": candidate.id}
            return self._upsert_node(product_id, "knowledge_candidate", source_id, "intent_candidate", content, meta, weight=1.6)
        if candidate_type == "table_field_evidence":
            table = str(payload.get("table") or "")
            fields = payload.get("fields") or []
            content = f"Approved table field evidence for {table}. Fields: {', '.join(map(str, fields))}. Reason: {candidate.reason}"
            meta = {**payload, "gap_id": candidate.gap_id, "candidate_id": candidate.id}
            return self._upsert_node(product_id, "knowledge_candidate", source_id, "table_field_evidence", content, meta, weight=1.65)
        if candidate_type == "predicate_pattern":
            content = f"Approved predicate pattern: {payload.get('pattern') or candidate.title}. Reason: {candidate.reason}"
            meta = {**payload, "gap_id": candidate.gap_id, "candidate_id": candidate.id}
            return self._upsert_node(product_id, "knowledge_candidate", source_id, "predicate_pattern", content, meta, weight=1.5)
        content = f"Approved candidate {candidate_type}: {json.dumps(payload, ensure_ascii=False)}"
        return self._upsert_node(product_id, "knowledge_candidate", source_id, candidate_type, content, {"payload": payload, "gap_id": candidate.gap_id}, weight=1.2)


class AgentRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, product_id: str, requirement: str, session_id: str | None = None) -> AgentRun:
        run = AgentRun(product_id=product_id, session_id=session_id, requirement=requirement, status="running")
        self.session.add(run)
        self.session.flush()
        return run

    def add_step(self, run_id: str, step_name: str, output: dict, input: dict | None = None, status: str = "succeeded", error: str = "") -> None:
        self.session.add(AgentStep(run_id=run_id, step_name=step_name, status=status, input=input or {}, output=output, error=error))
        self.session.flush()

    def complete(
        self,
        run_id: str,
        status: str,
        intent_plan: dict,
        evidence_bundles: list,
        retrieval_scores: list,
        validation_result: dict,
        context_budget: dict,
        llm_call_count: int,
        repair_count: int,
        invalid_reason: str = "",
    ) -> AgentRun:
        run = self.session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"Agent run not found: {run_id}")
        run.status = status
        run.intent_plan = intent_plan
        run.evidence_bundles = evidence_bundles
        run.retrieval_scores = retrieval_scores
        run.validation_result = validation_result
        run.context_budget = context_budget
        run.llm_call_count = llm_call_count
        run.repair_count = repair_count
        run.invalid_reason = invalid_reason
        self.session.flush()
        return run

    def get(self, run_id: str) -> dict | None:
        run = self.session.get(AgentRun, run_id)
        if not run:
            return None
        steps = list(self.session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.created_at)))
        return {
            "id": run.id,
            "product_id": run.product_id,
            "session_id": run.session_id,
            "requirement": run.requirement,
            "status": run.status,
            "intent_plan": run.intent_plan,
            "evidence_bundles": run.evidence_bundles,
            "retrieval_scores": run.retrieval_scores,
            "validation_result": run.validation_result,
            "context_budget": run.context_budget,
            "llm_call_count": run.llm_call_count,
            "repair_count": run.repair_count,
            "invalid_reason": run.invalid_reason,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "steps": [
                {
                    "step_name": step.step_name,
                    "status": step.status,
                    "input": step.input,
                    "output": step.output,
                    "error": step.error,
                    "created_at": step.created_at.isoformat() if step.created_at else None,
                }
                for step in steps
            ],
        }


class KnowledgeGapRepository:
    def __init__(self, session: Session):
        self.session = session

    def create_or_update(
        self,
        product_id: str,
        requirement: str,
        agent_run_id: str | None,
        session_id: str | None,
        invalid_reason: str,
        trigger_reasons: list[str],
        validation_result: dict,
        evidence_bundles: list,
        retrieval_scores: list,
    ) -> tuple[KnowledgeGap, bool]:
        requirement_hash = stable_content_hash(_normalize_requirement(requirement))
        gap = self.session.scalar(select(KnowledgeGap).where(
            KnowledgeGap.product_id == product_id,
            KnowledgeGap.requirement_hash == requirement_hash,
        ))
        created = False
        if not gap:
            gap = KnowledgeGap(product_id=product_id, requirement=requirement, requirement_hash=requirement_hash)
            self.session.add(gap)
            created = True
        gap.agent_run_id = agent_run_id
        gap.session_id = session_id
        gap.invalid_reason = invalid_reason
        gap.trigger_reasons = trigger_reasons
        gap.validation_result = validation_result
        gap.evidence_snapshot = evidence_bundles
        gap.retrieval_snapshot = retrieval_scores
        if gap.status not in {"running", "proposed", "approved", "rejected"}:
            gap.status = "queued"
        self.session.flush()
        return gap, created

    def get(self, gap_id: str) -> KnowledgeGap | None:
        return self.session.get(KnowledgeGap, gap_id)

    def list(self, product_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        query = select(KnowledgeGap).order_by(KnowledgeGap.updated_at.desc()).limit(limit)
        if product_id:
            query = query.where(KnowledgeGap.product_id == product_id)
        if status:
            query = query.where(KnowledgeGap.status == status)
        return [self.to_dict(row, include_candidates=False) for row in self.session.scalars(query)]

    def mark_running(self, gap_id: str) -> None:
        gap = self._require(gap_id)
        gap.status = "running"
        self.session.flush()

    def mark_failed(self, gap_id: str, error: str) -> None:
        gap = self._require(gap_id)
        gap.status = "failed"
        gap.candidate_summary = {"error": error}
        self.session.flush()

    def upsert_candidate(
        self,
        gap: KnowledgeGap,
        candidate_type: str,
        title: str,
        payload: dict,
        confidence: float,
        reason: str,
    ) -> KnowledgeCandidate:
        content_hash = stable_content_hash({"type": candidate_type, "title": title, "payload": payload})
        row = self.session.scalar(select(KnowledgeCandidate).where(
            KnowledgeCandidate.gap_id == gap.id,
            KnowledgeCandidate.candidate_type == candidate_type,
            KnowledgeCandidate.content_hash == content_hash,
        ))
        if not row:
            row = KnowledgeCandidate(gap_id=gap.id, product_id=gap.product_id, candidate_type=candidate_type, content_hash=content_hash)
            self.session.add(row)
        row.title = title
        row.payload = payload
        row.confidence = confidence
        row.reason = reason
        row.status = "proposed" if row.status not in {"approved", "rejected"} else row.status
        self.session.flush()
        return row

    def mark_proposed(self, gap_id: str, summary: dict, confidence: float) -> KnowledgeGap:
        gap = self._require(gap_id)
        gap.status = "proposed"
        gap.candidate_summary = summary
        gap.confidence = confidence
        self.session.flush()
        return gap

    def approve(self, gap_id: str, reviewed_by: str = "system") -> KnowledgeGap:
        gap = self._require(gap_id)
        gap.status = "approved"
        gap.reviewed_by = reviewed_by
        for candidate in self.session.scalars(select(KnowledgeCandidate).where(KnowledgeCandidate.gap_id == gap_id)):
            if candidate.status == "proposed":
                candidate.status = "approved"
                candidate.reviewed_by = reviewed_by
        self.session.flush()
        return gap

    def reject(self, gap_id: str, reviewed_by: str = "system") -> KnowledgeGap:
        gap = self._require(gap_id)
        gap.status = "rejected"
        gap.reviewed_by = reviewed_by
        for candidate in self.session.scalars(select(KnowledgeCandidate).where(KnowledgeCandidate.gap_id == gap_id)):
            if candidate.status == "proposed":
                candidate.status = "rejected"
                candidate.reviewed_by = reviewed_by
        self.session.flush()
        return gap

    def candidates(self, gap_id: str, status: str | None = None) -> list[KnowledgeCandidate]:
        query = select(KnowledgeCandidate).where(KnowledgeCandidate.gap_id == gap_id).order_by(KnowledgeCandidate.confidence.desc())
        if status:
            query = query.where(KnowledgeCandidate.status == status)
        return list(self.session.scalars(query))

    def to_dict(self, gap: KnowledgeGap, include_candidates: bool = True) -> dict:
        result = {
            "id": gap.id,
            "product_id": gap.product_id,
            "agent_run_id": gap.agent_run_id,
            "session_id": gap.session_id,
            "requirement": gap.requirement,
            "status": gap.status,
            "invalid_reason": gap.invalid_reason,
            "trigger_reasons": gap.trigger_reasons,
            "validation_result": gap.validation_result,
            "candidate_summary": gap.candidate_summary,
            "confidence": gap.confidence,
            "created_at": gap.created_at.isoformat() if gap.created_at else None,
            "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
        }
        if include_candidates:
            result["candidates"] = [self.candidate_to_dict(row) for row in self.candidates(gap.id)]
        return result

    @staticmethod
    def candidate_to_dict(candidate: KnowledgeCandidate) -> dict:
        return {
            "id": candidate.id,
            "gap_id": candidate.gap_id,
            "product_id": candidate.product_id,
            "candidate_type": candidate.candidate_type,
            "status": candidate.status,
            "source": candidate.source,
            "title": candidate.title,
            "payload": candidate.payload,
            "confidence": candidate.confidence,
            "reason": candidate.reason,
            "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
            "updated_at": candidate.updated_at.isoformat() if candidate.updated_at else None,
        }

    def _require(self, gap_id: str) -> KnowledgeGap:
        gap = self.session.get(KnowledgeGap, gap_id)
        if not gap:
            raise ValueError(f"Knowledge gap not found: {gap_id}")
        return gap


def _table_content(table: MetadataTable, profile: MetadataTableProfile | None) -> str:
    role = f"domain={profile.domain}; role={profile.role}; reason={profile.reason}" if profile else "domain=unknown; role=unknown"
    return f"Table: {table.table_name}\nComment: {table.comment}\nProfile: {role}\nRaw metadata: {json.dumps(table.raw or {}, ensure_ascii=False)[:2500]}"


def _column_content(table: MetadataTable, column: MetadataColumn, profile: MetadataTableProfile | None) -> str:
    role = f"{profile.domain}/{profile.role}" if profile else "unknown/unknown"
    return (
        f"Column: {table.table_name}.{column.column_name}\n"
        f"Comment: {column.comment}\n"
        f"Type: {column.data_type}({column.data_length or ''}) nullable={column.nullable}\n"
        f"Table comment: {table.comment}\n"
        f"Table role: {role}"
    )


def _column_chunks(columns: list[MetadataColumn], chunk_size: int) -> list[list[MetadataColumn]]:
    return [columns[offset:offset + chunk_size] for offset in range(0, len(columns), chunk_size)]


def _column_chunk_content(table: MetadataTable, columns: list[MetadataColumn], profile: MetadataTableProfile | None) -> str:
    role = f"{profile.domain}/{profile.role}" if profile else "unknown/unknown"
    lines = [
        f"Table: {table.table_name}",
        f"Table comment: {table.comment}",
        f"Table role: {role}",
        "Columns:",
    ]
    for column in columns:
        stat_content, stat_meta = _column_stat_content(table, column)
        traits = ",".join(stat_meta.get("traits", []))
        lines.append(
            f"- {column.column_name}: {column.comment}; type={column.data_type}({column.data_length or ''}); "
            f"nullable={column.nullable}; traits={traits}"
        )
    return "\n".join(lines)


def _column_stat_content(table: MetadataTable, column: MetadataColumn) -> tuple[str, dict]:
    name = column.column_name.upper()
    comment = column.comment or ""
    traits = []
    if any(token in name for token in ("NAME", "CNAME", "氏名")) or "氏名" in comment:
        traits.append("name")
    if any(token in name for token in ("SHAIN", "EMPLOYEE", "職員", "社員")) or any(token in comment for token in ("職員番号", "社員番号")):
        traits.append("employee_no")
    if "DTE" in name or any(token in comment for token in ("年月日", "日付")):
        traits.append("date")
    if name.endswith("_CDE") or "コード" in comment:
        traits.append("code")
    if name.endswith("_NME") or "名称" in comment:
        traits.append("label")
    traits = traits or ["unknown"]
    meta = {"table": table.table_name, "column": column.column_name, "traits": traits, "data_type": column.data_type}
    return f"Column statistic traits for {table.table_name}.{column.column_name}: {', '.join(traits)}. Comment: {comment}", meta


def _predicate_pattern(table: MetadataTable, column: MetadataColumn) -> dict | None:
    name = column.column_name.upper()
    comment = column.comment or ""
    if "氏名" in comment or "NAME" in name or name in {"CNAMEKNJ", "CNAMEKNA"}:
        return {"kind": "name_like", "table": table.table_name, "column": column.column_name, "content": f"Name search predicate: {table.table_name}.{column.column_name} LIKE :name_pattern || '%'"}
    if "職員番号" in comment or "社員番号" in comment or "SHAIN" in name:
        return {"kind": "employee_no", "table": table.table_name, "column": column.column_name, "content": f"Employee number predicate: {table.table_name}.{column.column_name} = :employee_no"}
    if "年月日" in comment or name.endswith("_DTE"):
        return {"kind": "date", "table": table.table_name, "column": column.column_name, "content": f"Date predicate: {table.table_name}.{column.column_name} BETWEEN :date_from AND :date_to"}
    return None


def _where_fragment(sql: str) -> str:
    text_value = strip_sql_comments(sql)
    match = re.search(r"\bWHERE\b(.+?)(\bGROUP\s+BY\b|\bORDER\s+BY\b|$)", text_value, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


def _sql_predicates(sql: str) -> list[str]:
    where = _where_fragment(sql)
    if not where:
        return []
    return [part.strip() for part in re.split(r"\bAND\b|\bOR\b", where, flags=re.IGNORECASE) if part.strip()]


def _row_to_evidence(row, channel: str) -> dict:
    meta = row["meta"] or {}
    table = str(meta.get("table") or "").upper()
    column = str(meta.get("column") or "").upper()
    return {
        "node_id": row["id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "facet": row["facet"],
        "table": table,
        "column": column,
        "score": round(max(0.0, float(row["score"] or 0)) * float(row["weight"] or 1.0), 4),
        "weight": float(row["weight"] or 1.0),
        "evidence": str(row["content"] or "")[:2400],
        "reasons": [channel],
        "meta": meta,
    }


def _rrf_fuse(result_sets: list[list[dict]], top_k: int = 24, k: int = 60) -> list[dict]:
    by_id: dict[str, dict] = {}
    scores: defaultdict[str, float] = defaultdict(float)
    reasons: defaultdict[str, list[str]] = defaultdict(list)
    for result_set in result_sets:
        for rank, item in enumerate(result_set, start=1):
            node_id = str(item.get("node_id") or item.get("source_id"))
            by_id.setdefault(node_id, item)
            scores[node_id] += (1 / (k + rank)) * float(item.get("weight") or 1.0) + float(item.get("score") or 0) / 100
            for reason in item.get("reasons") or []:
                if reason not in reasons[node_id]:
                    reasons[node_id].append(reason)
    fused = []
    for node_id, item in by_id.items():
        fused.append({**item, "score": round(scores[node_id], 4), "reasons": reasons[node_id] or item.get("reasons", [])})
    return sorted(fused, key=lambda row: (-float(row.get("score") or 0), str(row.get("source_id") or "")))[:top_k]


def _tokenize(query: str) -> list[str]:
    synonyms = {
        "员工": ["社員", "職員", "職員番号", "社員番号"],
        "姓名": ["氏名", "漢字氏名", "カナ氏名", "CNAMEKNJ", "CNAMEKNA"],
        "名字": ["氏名", "漢字氏名", "CNAMEKNJ"],
        "姓": ["氏名", "漢字氏名", "CNAMEKNJ"],
        "基本信息": ["基本情報", "個人基本情報", "給与基本情報"],
        "非常勤": ["非常勤職員", "非職", "DJND3001"],
        "入职": ["入社", "任用", "任用年月日", "NINYO_DTE", "DNINYO_DTE"],
        "入社": ["任用", "任用年月日", "NINYO_DTE", "DNINYO_DTE"],
        "任用": ["任用年月日", "NINYO_DTE", "DNINYO_DTE"],
        "採用": ["採用年月日", "SAIYO", "NINYO_DTE"],
        "异动": ["異動", "任免", "発令"],
        "異動": ["異動", "任免", "発令"],
        "日志": ["ログ", "チェック", "エラー", "メッセージ"],
    }
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*|[\u3040-\u30ff\u3400-\u9fff]{1,}", query or "")
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        for key, values in synonyms.items():
            if key in token:
                expanded.extend(values)
    return expanded


def _normalize_requirement(requirement: str) -> str:
    return re.sub(r"\s+", "", (requirement or "").strip().lower())
