"""
AnySQL API — 搜索路由 (稳健增强版)
"""

from __future__ import annotations

import time
import urllib.parse
from fastapi import APIRouter, Query
from anysql.logger import logger
from anysql.models.schemas import SearchRequest, SearchResponse
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository

router = APIRouter(prefix="/api/search", tags=["search"])

def _get_app_state():
    from anysql.main import app_state
    return app_state

@router.post("", response_model=SearchResponse)
async def search_sql_post(req: SearchRequest):
    state = _get_app_state()
    storage = state.get("storage")
    vector = state["vector"]
    t0 = time.time()
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            results = await PGVectorRepository(session, state["llm"], state["config"].llm.embed_model).search(req.query, req.product, req.top_k)
    else:
        results = await vector.search(query=req.query, product=req.product, top_k=req.top_k)
    return SearchResponse(
        query=req.query,
        results=results,
        total=len(results),
        elapsed_ms=round((time.time() - t0) * 1000, 1)
    )

@router.get("")
async def search_sql_get(q: str = Query(...), product: str = None, k: int = 5):
    state = _get_app_state()
    storage = state.get("storage")
    vector = state["vector"]
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            results = await PGVectorRepository(session, state["llm"], state["config"].llm.embed_model).search(q, product, k)
    else:
        results = await vector.search(query=q, product=product, top_k=k)
    return results

@router.get("/sql/{sql_id}")
async def get_sql_detail(sql_id: str):
    """获取单条 SQL 的完整分析详情 (支持日文 ID 解码)"""
    # 强制进行 URL 解码
    decoded_id = urllib.parse.unquote(sql_id)
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")

    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            record = SQLKnowledgeRepository(session).get(decoded_id)
            if record:
                return record.model_dump(mode="json")
        return {"error": f"Record not found: {decoded_id}"}

    parts = decoded_id.split("_")
    if len(parts) < 2: return {"error": "Invalid SQL ID"}
    
    product_id = parts[0]
    if product_id not in config.products: return {"error": f"Product not found: {product_id}"}
    
    pcfg = config.products[product_id]
    from anysql.harness.tasks import load_all_records
    
    # 遍历所有记录查找
    records = load_all_records(pcfg.desc_dir)
    for r in records:
        if r.statement.id == decoded_id:
            return r.model_dump(mode="json")

    return {"error": f"Record not found: {decoded_id}"}
