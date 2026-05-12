"""
AnySQL 向量引擎

基于 ChromaDB 的向量存储与语义检索。
使用 Ollama bge-m3 生成 Embedding。
"""

from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Optional

import chromadb

from anysql.core.llm_client import LLMClient
from anysql.logger import logger
from anysql.models.schemas import SQLRecord, SearchResult


class VectorEngine:
    """
    向量存储与检索引擎。

    每个产品一个 ChromaDB Collection。
    文档 = SQL原文 + 注释 + LLM分析摘要，
    Metadata = 产品/文件名/分类/复杂度等可过滤字段。
    """

    def __init__(
        self,
        persist_dir: str,
        collection_prefix: str,
        llm_client: LLMClient,
    ):
        self.persist_dir = persist_dir
        self.prefix = collection_prefix
        self.llm = llm_client

        self._client = chromadb.PersistentClient(path=persist_dir)
        logger.info(f"VectorEngine 初始化: persist_dir={persist_dir}")

    def _collection_name(self, product: str) -> str:
        return f"{self.prefix}_{product}"

    def _metadata_collection_name(self, product: str) -> str:
        return f"{self.prefix}_{product}_metadata"

    def _get_or_create_collection(self, product: str) -> chromadb.Collection:
        name = self._collection_name(product)
        collection = self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug(f"Collection '{name}': {collection.count()} 条记录")
        return collection

    def _get_or_create_metadata_collection(self, product: str) -> chromadb.Collection:
        name = self._metadata_collection_name(product)
        collection = self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug(f"Metadata Collection '{name}': {collection.count()} 条记录")
        return collection

    # ------------------------------------------------------------------
    # 文档构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_document(record: SQLRecord) -> str:
        """将 SQL 记录合成为单一文档用于 Embedding"""
        parts = []

        # 注释
        if record.statement.comment:
            parts.append(f"説明: {record.statement.comment}")

        # SQL 原文
        parts.append(f"SQL: {record.statement.raw_sql}")

        # 分析结果
        if record.analysis:
            parts.append(f"概要: {record.analysis.summary}")
            if record.analysis.business_context:
                parts.append(
                    "業務: " + ", ".join(record.analysis.business_context)
                )
            if record.analysis.keywords:
                parts.append(
                    "キーワード: " + ", ".join(record.analysis.keywords)
                )
            if record.analysis.usage_guide:
                parts.append(f"使い方: {record.analysis.usage_guide}")

        return "\n".join(parts)

    @staticmethod
    def _build_metadata(record: SQLRecord) -> dict:
        """构建 ChromaDB metadata"""
        meta: dict = {
            "sql_id": record.statement.id,
            "product": record.statement.product,
            "source_file": record.statement.source_file,
            "statement_type": record.statement.statement_type.value,
            "tables": ",".join(record.statement.tables),
            "line_number": record.statement.line_number,
            "comment": record.statement.comment[:500],
        }
        if record.analysis:
            meta["category"] = ",".join(record.analysis.category)
            meta["complexity"] = record.analysis.complexity.value
            meta["summary"] = record.analysis.summary[:200]
            meta["keywords"] = ",".join(record.analysis.keywords)
            meta["business_context"] = "\n".join(record.analysis.business_context)
        return meta

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------

    async def index_records(
        self,
        records: list[SQLRecord],
        product: str,
    ) -> int:
        """
        将 SQL 记录批量写入向量库。

        已存在的记录会被更新（upsert）。
        返回成功索引的数量。
        """
        if not records:
            return 0

        collection = self._get_or_create_collection(product)

        documents = [self._build_document(r) for r in records]
        ids = [r.statement.id for r in records]
        metadatas = [self._build_metadata(r) for r in records]

        logger.info(f"生成 Embedding: {len(documents)} 条文档")
        t0 = time.time()

        # 生成 Embedding
        embeddings = await self.llm.embed(documents)
        embed_time = time.time() - t0

        # 过滤掉 embedding 失败的记录
        valid_indices = [i for i, e in enumerate(embeddings) if e]
        if len(valid_indices) < len(records):
            logger.warning(
                f"Embedding 部分失败: {len(valid_indices)}/{len(records)}"
            )

        valid_docs = [documents[i] for i in valid_indices]
        valid_ids = [ids[i] for i in valid_indices]
        valid_metas = [metadatas[i] for i in valid_indices]
        valid_embeds = [embeddings[i] for i in valid_indices]

        # 写入 ChromaDB
        if valid_ids:
            collection.upsert(
                ids=valid_ids,
                documents=valid_docs,
                metadatas=valid_metas,
                embeddings=valid_embeds,
            )

        total_time = time.time() - t0
        logger.info(
            f"向量索引完成: {len(valid_ids)} 条, "
            f"embed={embed_time:.1f}s, total={total_time:.1f}s"
        )

        return len(valid_ids)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        product: Optional[str] = None,
        top_k: int = 10,
        where_filter: Optional[dict] = None,
    ) -> list[SearchResult]:
        """
        自然语言语义搜索。

        Args:
            query: 自然语言查询
            product: 限定产品（None=搜索所有产品）
            top_k: 返回数量
            where_filter: ChromaDB where filter

        Returns:
            按相关性排序的 SearchResult 列表
        """
        t0 = time.time()

        # 生成查询向量
        query_embedding = await self.llm.embed([query])
        if not query_embedding or not query_embedding[0]:
            logger.error("查询 embedding 生成失败")
            return []

        # 确定搜索范围
        if product:
            collections = [self._get_or_create_collection(product)]
        else:
            # 搜索所有 collection
            all_collections = self._client.list_collections()
            collections = [
                self._client.get_collection(c.name)
                for c in all_collections
                if c.name.startswith(self.prefix)
            ]

        all_results: list[SearchResult] = []

        for collection in collections:
            if collection.count() == 0:
                continue

            query_params: dict = {
                "query_embeddings": [query_embedding[0]],
                "n_results": min(top_k, collection.count()),
            }
            if where_filter:
                query_params["where"] = where_filter

            try:
                results = collection.query(**query_params)
            except Exception as e:
                logger.warning(f"Collection {collection.name} 查询失败: {e}")
                continue

            if not results or not results["ids"] or not results["ids"][0]:
                continue

            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 1.0
                doc = results["documents"][0][i] if results["documents"] else ""

                # ChromaDB cosine distance → similarity score
                score = max(0, 1 - distance)

                all_results.append(SearchResult(
                    sql_id=doc_id,
                    score=round(score, 4),
                    summary=meta.get("summary", ""),
                    raw_sql=self._extract_sql_from_doc(doc),
                    comment=meta.get("comment", ""),
                    source_file=meta.get("source_file", ""),
                    product=meta.get("product", ""),
                    category=meta.get("category", "").split(",") if meta.get("category") else [],
                    keywords=meta.get("keywords", "").split(",") if meta.get("keywords") else [],
                    business_context=meta.get("business_context", "").split("\n") if meta.get("business_context") else [],
                ))

        # 按分数排序
        all_results.sort(key=lambda r: r.score, reverse=True)
        all_results = all_results[:top_k]

        elapsed = (time.time() - t0) * 1000
        logger.info(
            f"搜索完成: query='{query[:30]}', "
            f"results={len(all_results)}, {elapsed:.0f}ms"
        )

        return all_results

    @staticmethod
    def _extract_sql_from_doc(doc: str) -> str:
        """从文档中提取 SQL 部分"""
        for line in doc.split("\n"):
            if line.startswith("SQL: "):
                return line[5:]
        return ""

    # ------------------------------------------------------------------
    # Metadata RAG
    # ------------------------------------------------------------------

    async def index_metadata(self, product: str, metadata_dir: str, limit: int | None = None) -> int:
        """将产品表元数据向量化，供 SQL 生成 RAG 使用。"""
        tables_dir = Path(metadata_dir) / "tables"
        if not tables_dir.exists():
            logger.warning(f"元数据目录不存在: {tables_dir}")
            return 0

        files = sorted(tables_dir.glob("*.json"))
        if limit:
            files = files[:limit]

        docs: list[str] = []
        ids: list[str] = []
        metadatas: list[dict] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"跳过无效元数据 {path}: {e}")
                continue
            table = path.stem.upper()
            docs.append(self._metadata_doc(table, data))
            ids.append(f"{product}_table_{table}")
            metadatas.append({"product": product, "table": table, "source_file": path.name})

        if not docs:
            return 0

        embeddings = await self.llm.embed(docs)
        valid = [i for i, emb in enumerate(embeddings) if emb]
        collection = self._get_or_create_metadata_collection(product)
        if valid:
            collection.upsert(
                ids=[ids[i] for i in valid],
                documents=[docs[i] for i in valid],
                metadatas=[metadatas[i] for i in valid],
                embeddings=[embeddings[i] for i in valid],
            )
        logger.info(f"元数据向量索引完成: {product} {len(valid)}/{len(docs)}")
        return len(valid)

    async def search_metadata(self, query: str, product: str, top_k: int = 8) -> list[dict]:
        """搜索产品表元数据。"""
        query_embedding = await self.llm.embed([query])
        if not query_embedding or not query_embedding[0]:
            return []
        collection = self._get_or_create_metadata_collection(product)
        if collection.count() == 0:
            return []
        results = collection.query(
            query_embeddings=[query_embedding[0]],
            n_results=min(top_k, collection.count()),
        )
        rows: list[dict] = []
        for i, doc_id in enumerate(results["ids"][0] if results and results.get("ids") else []):
            meta = results["metadatas"][0][i] if results.get("metadatas") else {}
            distance = results["distances"][0][i] if results.get("distances") else 1.0
            doc = results["documents"][0][i] if results.get("documents") else ""
            rows.append({
                "id": doc_id,
                "table": meta.get("table", ""),
                "score": round(max(0, 1 - distance), 4),
                "document": doc,
            })
        return rows

    @staticmethod
    def _metadata_doc(table: str, data: dict) -> str:
        parts = [f"テーブル: {table}"]
        for key in ("comment", "description", "remarks", "table_comment"):
            if data.get(key):
                parts.append(f"説明: {data[key]}")
        columns = data.get("columns") or data.get("COLUMN") or data.get("COLUMNS") or []
        if isinstance(columns, list):
            column_lines = []
            for col in columns[:80]:
                if isinstance(col, dict):
                    name = col.get("name") or col.get("column_name") or col.get("COLUMN_NAME") or ""
                    comment = col.get("comment") or col.get("comments") or col.get("COLUMN_COMMENT") or col.get("remarks") or ""
                    dtype = col.get("type") or col.get("data_type") or col.get("DATA_TYPE") or ""
                    column_lines.append(f"{name} {dtype} {comment}".strip())
                else:
                    column_lines.append(str(col))
            parts.append("カラム: " + " / ".join(column_lines))
        elif columns:
            parts.append(f"カラム: {str(columns)[:1500]}")
        else:
            parts.append("JSON: " + json.dumps(data, ensure_ascii=False)[:1500])
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def get_collection_stats(self, product: str) -> dict:
        """获取 collection 统计信息"""
        collection = self._get_or_create_collection(product)
        return {
            "product": product,
            "collection": collection.name,
            "count": collection.count(),
        }

    def clear_product(self, product: str) -> None:
        """清空产品的向量数据"""
        name = self._collection_name(product)
        try:
            self._client.delete_collection(name)
            logger.info(f"已清空 Collection: {name}")
        except Exception as e:
            logger.warning(f"清空 Collection 失败: {e}")
