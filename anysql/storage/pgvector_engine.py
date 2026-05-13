"""pgvector-backed semantic search."""

from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from anysql.core.vector_engine import VectorEngine
from anysql.core.table_profiles import is_table_allowed, policy_for_intent, resolve_intent, table_policy_score
from anysql.models.schemas import SQLRecord, SearchResult
from anysql.storage.models import MetadataColumn, MetadataEmbedding, MetadataTable, MetadataTableProfile, Product, SQLEmbedding


class PGVectorRepository:
    def __init__(self, session: Session, llm, embedding_model: str):
        self.session = session
        self.llm = llm
        self.embedding_model = embedding_model

    @staticmethod
    def _hash(doc: str) -> str:
        return hashlib.sha256(doc.encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata_doc(table: MetadataTable) -> str:
        return VectorEngine._metadata_doc(table.table_name, table.raw or {})

    async def index_records(self, records: list[SQLRecord], product_id: str, commit_each_batch: bool = False) -> int:
        if not records:
            return 0
        count = 0
        batch_size = 64
        for offset in range(0, len(records), batch_size):
            batch = records[offset:offset + batch_size]
            docs = [VectorEngine._build_document(r) for r in batch]
            embeddings = await self.llm.embed(docs)
            for record, doc, emb in zip(batch, docs, embeddings):
                if not emb:
                    continue
                content_hash = self._hash(doc)
                row = self.session.scalar(
                    select(SQLEmbedding).where(
                        SQLEmbedding.product_id == product_id,
                        SQLEmbedding.source_id == record.statement.id,
                        SQLEmbedding.embedding_model == self.embedding_model,
                    )
                )
                if row and row.content_hash == content_hash:
                    continue
                if not row:
                    row = SQLEmbedding(
                        product_id=product_id,
                        source_id=record.statement.id,
                        embedding_model=self.embedding_model,
                    )
                    self.session.add(row)
                row.source_type = "sql"
                row.content_hash = content_hash
                row.document = doc
                row.meta = VectorEngine._build_metadata(record)
                row.embedding = emb
                count += 1
            self.session.flush()
            if commit_each_batch:
                self.session.commit()
        return count

    async def index_metadata(self, product_id: str, commit_each_batch: bool = False) -> int:
        tables = list(self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id)))
        source_ids = {t.table_name for t in tables}
        existing = list(self.session.scalars(select(MetadataEmbedding).where(MetadataEmbedding.product_id == product_id)))
        stale = [row.id for row in existing if row.source_id not in source_ids]
        if stale:
            self.session.execute(delete(MetadataEmbedding).where(MetadataEmbedding.id.in_(stale)))
            if commit_each_batch:
                self.session.commit()

        docs: list[tuple[MetadataTable, str, str]] = []
        for table in tables:
            doc = self._metadata_doc(table)
            content_hash = self._hash(doc)
            row = self.session.scalar(
                select(MetadataEmbedding).where(
                    MetadataEmbedding.product_id == product_id,
                    MetadataEmbedding.source_id == table.table_name,
                    MetadataEmbedding.embedding_model == self.embedding_model,
                )
            )
            if row and row.content_hash == content_hash:
                continue
            docs.append((table, doc, content_hash))
        if not docs:
            self.session.flush()
            return 0
        count = 0
        batch_size = 64
        for offset in range(0, len(docs), batch_size):
            batch = docs[offset:offset + batch_size]
            embeddings = await self.llm.embed([doc for _, doc, _ in batch])
            for (table, doc, content_hash), emb in zip(batch, embeddings):
                if not emb:
                    continue
                row = self.session.scalar(
                    select(MetadataEmbedding).where(
                        MetadataEmbedding.product_id == product_id,
                        MetadataEmbedding.source_id == table.table_name,
                        MetadataEmbedding.embedding_model == self.embedding_model,
                    )
                )
                if not row:
                    row = MetadataEmbedding(product_id=product_id, source_id=table.table_name, embedding_model=self.embedding_model)
                    self.session.add(row)
                row.source_type = "metadata_table"
                row.content_hash = content_hash
                row.document = doc
                row.meta = {"table": table.table_name, "product_id": product_id}
                row.embedding = emb
                count += 1
            self.session.flush()
            if commit_each_batch:
                self.session.commit()
        return count

    async def search(self, query: str, product_code: str | None = None, top_k: int = 10) -> list[SearchResult]:
        t0 = time.time()
        query_embedding = await self.llm.embed([query])
        if not query_embedding or not query_embedding[0]:
            return []
        product_filter = ""
        params = {"embedding": json.dumps(query_embedding[0]), "limit": top_k, "model": self.embedding_model}
        if product_code:
            product = self.session.scalar(select(Product).where(Product.code == product_code, Product.deleted.is_(False)))
            if not product:
                return []
            product_filter = "AND e.product_id = :product_id"
            params["product_id"] = product.id
        rows = self.session.execute(text(f"""
            SELECT e.source_id, e.document, e.meta, p.code, (1 - (e.embedding <=> CAST(:embedding AS vector))) AS score
            FROM sql_embeddings e
            JOIN products p ON p.id = e.product_id
            WHERE e.embedding_model = :model {product_filter}
            ORDER BY e.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """), params).mappings().all()
        results: list[SearchResult] = []
        for row in rows:
            meta = row["meta"] or {}
            doc = row["document"] or ""
            results.append(SearchResult(
                sql_id=row["source_id"],
                score=round(max(0, float(row["score"] or 0)), 4),
                summary=meta.get("summary", ""),
                raw_sql=VectorEngine._extract_sql_from_doc(doc),
                comment=meta.get("comment", ""),
                source_file=meta.get("source_file", ""),
                product=row["code"],
                category=meta.get("category", "").split(",") if isinstance(meta.get("category"), str) and meta.get("category") else [],
                keywords=meta.get("keywords", "").split(",") if isinstance(meta.get("keywords"), str) and meta.get("keywords") else [],
                business_context=meta.get("business_context", "").split("\n") if isinstance(meta.get("business_context"), str) and meta.get("business_context") else [],
            ))
        return results

    async def search_metadata(self, query: str, product_id: str, top_k: int = 8) -> list[dict]:
        query_embedding = await self.llm.embed([query])
        if not query_embedding or not query_embedding[0]:
            return []
        rows = self.session.execute(text("""
            SELECT source_id, document, meta, (1 - (embedding <=> CAST(:embedding AS vector))) AS score
            FROM metadata_embeddings
            WHERE product_id = :product_id AND embedding_model = :model
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """), {"embedding": json.dumps(query_embedding[0]), "product_id": product_id, "model": self.embedding_model, "limit": top_k}).mappings().all()
        return [
            {
                "id": row["source_id"],
                "table": (row["meta"] or {}).get("table", row["source_id"]),
                "score": round(max(0, float(row["score"] or 0)), 4),
                "document": row["document"],
            }
            for row in rows
        ]

    def search_metadata_text(self, query: str, product_id: str, top_k: int = 8) -> list[dict]:
        terms = [term for term in set(_tokenize_query(query)) if len(term) >= 2]
        if not terms:
            return []
        conditions = []
        fuzzy_scores = []
        params: dict[str, object] = {"product_id": product_id, "limit": top_k, "query": query}
        for idx, term in enumerate(terms[:12]):
            key = f"term_{idx}"
            params[key] = f"%{term}%"
            raw_key = f"raw_{idx}"
            params[raw_key] = term
            conditions.append(
                f"(mt.table_name ILIKE :{key} OR mt.comment ILIKE :{key} "
                f"OR mc.column_name ILIKE :{key} OR mc.comment ILIKE :{key} "
                f"OR similarity(mt.table_name, :{raw_key}) > 0.18 "
                f"OR similarity(COALESCE(mt.comment, ''), :{raw_key}) > 0.18 "
                f"OR similarity(mc.column_name, :{raw_key}) > 0.18 "
                f"OR similarity(COALESCE(mc.comment, ''), :{raw_key}) > 0.18)"
            )
            fuzzy_scores.append(
                f"GREATEST(similarity(mt.table_name, :{raw_key}), "
                f"similarity(COALESCE(mt.comment, ''), :{raw_key}), "
                f"similarity(mc.column_name, :{raw_key}), "
                f"similarity(COALESCE(mc.comment, ''), :{raw_key}))"
            )
        fuzzy_score_sql = f"GREATEST({', '.join(fuzzy_scores)})" if fuzzy_scores else "0"
        rows = self.session.execute(text(f"""
            SELECT mt.id, mt.table_name, mt.comment,
                   COUNT(*) AS hit_count,
                   MAX({fuzzy_score_sql}) AS fuzzy_score,
                   STRING_AGG(DISTINCT mc.column_name || ':' || COALESCE(mc.comment, ''), E'\n') AS matched_columns
            FROM metadata_tables mt
            LEFT JOIN metadata_columns mc ON mc.metadata_table_id = mt.id
            WHERE mt.product_id = :product_id AND ({' OR '.join(conditions)})
            GROUP BY mt.id, mt.table_name, mt.comment
            ORDER BY MAX({fuzzy_score_sql}) DESC, hit_count DESC, mt.table_name
            LIMIT :limit
        """), params).mappings().all()
        results: list[dict] = []
        for row in rows:
            table = self.session.get(MetadataTable, row["id"])
            document = self._metadata_doc(table) if table else str(row["table_name"] or "")
            if row["matched_columns"]:
                document = f"{document}\nMatched columns:\n{row['matched_columns']}"
            results.append({
                "id": row["table_name"],
                "table": row["table_name"],
                "score": round(min(1.0, max(float(row["fuzzy_score"] or 0), 0.55 + float(row["hit_count"] or 0) / 40)), 4),
                "document": document,
                "source": "fuzzy_text",
            })
        return results

    def search_metadata_by_intent(self, requirement: str, product_id: str, top_k: int = 8) -> list[dict]:
        intent = resolve_intent(requirement)
        policy = policy_for_intent(intent)
        if not policy:
            return []
        rows = self.session.execute(text("""
            SELECT mt.id, mt.table_name, mt.comment,
                   STRING_AGG(
                       DISTINCT CASE
                           WHEN mc.column_name ~* '(C?NAME|KANJI|KANA|MEI|SHIMEI|SIMEI|SHAIN|EMPLOYEE|CEMPLOYEE|HTRE|IDO|NNMN)'
                                OR COALESCE(mc.comment, '') LIKE '%氏名%'
                                OR COALESCE(mc.comment, '') LIKE '%職員番号%'
                                OR COALESCE(mc.comment, '') LIKE '%社員番号%'
                                OR COALESCE(mc.comment, '') LIKE '%異動%'
                                OR COALESCE(mc.comment, '') LIKE '%任免%'
                                OR COALESCE(mc.comment, '') LIKE '%発令%'
                           THEN mc.column_name || ':' || COALESCE(mc.comment, '')
                           ELSE NULL
                       END,
                       E'\n'
                   ) AS matched_columns
            FROM metadata_tables mt
            JOIN metadata_columns mc ON mc.metadata_table_id = mt.id
            JOIN metadata_table_profiles mp ON mp.product_id = mt.product_id AND mp.table_name = mt.table_name
            WHERE mt.product_id = :product_id
              AND mp.role = ANY(:allowed_roles)
              AND NOT (mp.role = ANY(:blocked_roles))
              AND (:domain = 'unknown' OR mp.domain = :domain)
            GROUP BY mt.id, mt.table_name, mt.comment
        """), {
            "product_id": product_id,
            "allowed_roles": list(policy.allowed_roles),
            "blocked_roles": list(policy.blocked_roles),
            "domain": policy.domain,
        }).mappings().all()
        scored: list[dict] = []
        for row in rows:
            table_name = str(row["table_name"] or "")
            comment = str(row["comment"] or "")
            profile = self._profile(product_id, table_name)
            score = 0.6 + table_policy_score(table_name, profile, policy)
            if score <= 0:
                continue
            table = self.session.get(MetadataTable, row["id"])
            document = self._metadata_doc(table) if table else f"Table: {table_name}\nComment: {comment}"
            if row["matched_columns"]:
                document = f"{document}\nIntent matched columns:\n{row['matched_columns']}"
            if profile:
                document = f"{document}\nTable profile: domain={profile.domain}, role={profile.role}, source={profile.source}, reason={profile.reason}"
            scored.append({
                "id": table_name,
                "table": table_name,
                "score": round(score, 4),
                "document": document,
                "profile": self._profile_dict(profile),
                "source": "intent_rerank",
            })
        return sorted(scored, key=lambda item: (-float(item["score"]), str(item["table"])))[:top_k]

    def apply_profile_policy(self, hits: list[dict], requirement: str, product_id: str) -> list[dict]:
        intent = resolve_intent(requirement)
        policy = policy_for_intent(intent)
        adjusted: list[dict] = []
        for item in hits:
            table_name = str(item.get("table") or item.get("id") or "").upper()
            profile = self._profile(product_id, table_name)
            if policy and profile and not is_table_allowed(profile, policy):
                continue
            score = float(item.get("score") or 0) + table_policy_score(table_name, profile, policy)
            new_item = {**item, "score": round(max(0, min(1, score)), 4), "profile": self._profile_dict(profile)}
            if profile and "Table profile:" not in str(new_item.get("document", "")):
                new_item["document"] = f"{new_item.get('document', '')}\nTable profile: domain={profile.domain}, role={profile.role}, source={profile.source}, reason={profile.reason}"
            adjusted.append(new_item)
        return sorted(adjusted, key=lambda row: (-float(row.get("score") or 0), str(row.get("table") or "")))

    def blocked_profiles(self, hits: list[dict], requirement: str, product_id: str) -> list[dict]:
        policy = policy_for_intent(resolve_intent(requirement))
        if not policy:
            return []
        blocked = []
        seen: set[str] = set()
        for item in hits:
            table_name = str(item.get("table") or item.get("id") or "").upper()
            if table_name in seen:
                continue
            seen.add(table_name)
            profile = self._profile(product_id, table_name)
            if profile and not is_table_allowed(profile, policy):
                blocked.append(self._profile_dict(profile))
        return blocked

    def _profile(self, product_id: str, table_name: str) -> MetadataTableProfile | None:
        return self.session.scalar(select(MetadataTableProfile).where(
            MetadataTableProfile.product_id == product_id,
            MetadataTableProfile.table_name == table_name.upper(),
        ))

    @staticmethod
    def _profile_dict(profile: MetadataTableProfile | None) -> dict | None:
        if not profile:
            return None
        return {
            "table_name": profile.table_name,
            "domain": profile.domain,
            "role": profile.role,
            "confidence": float(profile.confidence or 0),
            "reason": profile.reason,
            "source": profile.source,
        }


def _tokenize_query(query: str) -> list[str]:
    synonyms = {
        "员工": ["社員", "職員", "職員番号", "社員番号"],
        "姓名": ["氏名", "漢字氏名", "カナ氏名", "CNAMEKNJ", "CNAMEKNA"],
        "名字": ["氏名", "漢字氏名", "CNAMEKNJ"],
        "姓": ["氏名", "漢字氏名", "CNAMEKNJ"],
        "基本信息": ["基本情報", "給与基本情報", "BTKIHON"],
        "基本資料": ["基本情報", "給与基本情報", "BTKIHON"],
        "异动": ["異動", "任免", "発令"],
        "異動": ["異動", "任免", "発令"],
    }
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*|[\u3040-\u30ff\u3400-\u9fff]{1,}", query or "")
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        for key, values in synonyms.items():
            if key in token:
                expanded.extend(values)
    return expanded


def _metadata_intent(requirement: str) -> str:
    text_value = requirement or ""
    asks_basic = any(word in text_value for word in ("基本情况", "基本信息", "基本資料", "基本情報"))
    asks_employee = any(word in text_value for word in ("员工", "职员", "職員", "社員", "人的", "人の"))
    asks_name = any(word in text_value for word in ("姓", "姓名", "名字", "氏名"))
    if asks_basic and asks_employee and asks_name:
        return "employee_basic_name"
    return ""


def _employee_basic_table_score(table_name: str, comment: str) -> float:
    table = table_name.upper()
    text_value = f"{table_name} {comment}"
    score = 0.55
    if "個人基本情報DB" in text_value:
        score += 0.42
    elif "個人基本情報" in text_value:
        score += 0.36
    elif "基本情報" in text_value:
        score += 0.24
    if "DB" in comment:
        score += 0.04
    if table in {"DJND0110", "MAST_EMPLOYEES", "MAST_EMPLOYEES2"}:
        score += 0.05
    penalties = {
        "IF": 0.24,
        "DT_": 0.22,
        "_IDO": 0.2,
        "_KAK": 0.2,
        "ワーク": 0.24,
        "WORK": 0.24,
        "WK": 0.18,
        "WPT": 0.18,
        "入力": 0.16,
        "連携": 0.16,
        "履歴": 0.16,
        "ログ": 0.16,
        "バックアップ": 0.16,
        "非常勤": 0.16,
        "翌月": 0.14,
        "給与": 0.12,
        "財形": 0.18,
        "賞与": 0.12,
        "年末": 0.12,
        "_R": 0.12,
        "DEL": 0.12,
    }
    for marker, penalty in penalties.items():
        if marker in text_value.upper() if marker.isascii() else marker in text_value:
            score -= penalty
    return score
