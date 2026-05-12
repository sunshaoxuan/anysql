"""
AnySQL API - SQL 生成草稿。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.models.schemas import (
    GeneratedSQL,
    SQLGenerationRequest,
    SQLGenerationResponse,
)
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository

router = APIRouter(prefix="/api/generate", tags=["generation"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.post("/sql", response_model=SQLGenerationResponse)
async def generate_sql(req: SQLGenerationRequest):
    """
    根据产品 Metadata 和既有 SQL 知识生成 SQL 草稿。
    草稿需要通过 /api/assistant/learn 人工确认后才会入库。
    """
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")
            config.products[req.product] = ProductRepository(session).to_product_config(product)
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    pipeline = state["pipeline"]
    try:
        if storage and storage.is_database_mode:
            query_ja = await pipeline._normalize_query_to_japanese(req.requirement)
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                vector = PGVectorRepository(session, state["llm"], config.llm.embed_model)
                candidates = await vector.search(query_ja, req.product, req.top_k)
                metadata_hits = await vector.search_metadata(query_ja, product.id, 8)
            examples = [
                f"- {item.summary}\n  SQL: {item.raw_sql}\n  Context: {' / '.join(item.business_context)}"
                for item in candidates
            ]
            product_rules = config.products[req.product].rules.strip() or "No product-specific rules."
            metadata_context = "\n\n".join(
                f"- {m['table']} score={m['score']}\n{m['document']}"
                for m in metadata_hits
            ) or "No metadata vector hits."
            prompt = f"""你要为产品 {req.product} 生成一段满足业务需求的 SQL。

业务需求:
{req.requirement}

日文检索意图（元数据语言）:
{query_ja}

产品规则（必须遵守）:
{product_rules}

Metadata RAG 候选（优先参考）:
{metadata_context}

相似SQL知识:
{chr(10).join(examples) if examples else "No similar SQL knowledge found."}

要求:
1. 优先复用相似SQL和Metadata中的表、字段、日期条件和命名习惯。
2. 如果需要参数，用清晰占位符，例如 :employee_no 或 :target_date。
3. 输出 SQL 的用途、参数用法、业务含义、涉及表和假设。
4. 只返回 JSON，不要返回 Markdown。
"""
            generated = await state["agent"].execute_task(prompt, GeneratedSQL)
            record = pipeline._draft_record(req.product, req.requirement, generated)
        else:
            record = await pipeline.generate_and_learn(
                product_id=req.product,
                requirement=req.requirement,
                top_k=req.top_k,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    generated = GeneratedSQL(
        sql=record.statement.raw_sql,
        summary=record.analysis.summary if record.analysis else "",
        business_meaning=(
            " / ".join(record.analysis.business_context)
            if record.analysis else ""
        ),
        usage_guide=record.analysis.usage_guide if record.analysis else "",
        parameters=record.analysis.parameters if record.analysis else [],
        tables=record.statement.tables,
    )
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            SQLKnowledgeRepository(session).save_draft(product.id, req.requirement, record)
    return SQLGenerationResponse(
        product=req.product,
        requirement=req.requirement,
        generated=generated,
        record=record,
        learned=False,
    )
