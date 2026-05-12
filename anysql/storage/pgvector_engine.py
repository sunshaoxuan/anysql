"""pgvector-backed semantic search."""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from anysql.core.vector_engine import VectorEngine
from anysql.models.schemas import SQLRecord, SearchResult
from anysql.storage.models import MetadataEmbedding, MetadataTable, Product, SQLEmbedding


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

    async def index_records(self, records: list[SQLRecord], product_id: str) -> int:
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
        return count

    async def index_metadata(self, product_id: str) -> int:
        tables = list(self.session.scalars(select(MetadataTable).where(MetadataTable.product_id == product_id)))
        source_ids = {t.table_name for t in tables}
        existing = list(self.session.scalars(select(MetadataEmbedding).where(MetadataEmbedding.product_id == product_id)))
        stale = [row.id for row in existing if row.source_id not in source_ids]
        if stale:
            self.session.execute(delete(MetadataEmbedding).where(MetadataEmbedding.id.in_(stale)))

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
