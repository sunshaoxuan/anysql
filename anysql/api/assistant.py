"""
AnySQL API - SQL 辅助对话。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from anysql.models.schemas import AnalysisStatus, GeneratedSQL, SQLAssistantRequest, SQLAssistantResponse, SQLLearnRequest
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.post("/sql", response_model=SQLAssistantResponse)
async def assist_sql(req: SQLAssistantRequest):
    """
    SQL 专用对话入口。

    先用向量检索找候选，再由 LLM 判断匹配度；高匹配返回已有 SQL。
    低匹配生成草稿，带当前 SQL 时按修正意见改写草稿。草稿必须人工确认后才入库。
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
            current_sql = req.current_sql
            if not current_sql and req.current_sql_id:
                record = SQLKnowledgeRepository(session).get(req.current_sql_id)
                current_sql = record.statement.raw_sql if record else None
        try:
            pipeline = state["pipeline"]
            if current_sql:
                record = await pipeline.revise_and_learn(req.product, req.message, current_sql)
                mode, matches, learned = "revised", [], False
            else:
                query_ja = await pipeline._normalize_query_to_japanese(req.message)
                with storage.database.session() as search_session:
                    product = ProductRepository(search_session).get_by_code(req.product)
                    vector = PGVectorRepository(search_session, state["llm"], config.llm.embed_model)
                    candidates = await vector.search(query_ja, req.product, req.top_k)
                    metadata_hits = await vector.search_metadata(query_ja, product.id, 8)
                matches = await pipeline._llm_match_candidates(req.product, f"{req.message}\n日文检索意图: {query_ja}", candidates)
                best = matches[0] if matches else None
                if best and best.llm_score >= req.match_threshold:
                    with storage.database.session() as record_session:
                        record = SQLKnowledgeRepository(record_session).get(best.sql_id)
                        if record:
                            record.statement.product = req.product
                    mode, learned = "matched", False
                else:
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
{req.message}

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
                    record = pipeline._draft_record(req.product, req.message, generated)
                    mode, learned = "generated", False
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                if mode != "matched":
                    SQLKnowledgeRepository(session).save_draft(product.id, req.message, record, req.session_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        text = "找到可直接复用的 SQL。" if mode == "matched" else "已根据修正意见改写 SQL。请确认正确后再采纳入库。" if mode == "revised" else "没有足够高匹配的 SQL，已根据 Metadata 和既有知识生成草稿。请确认正确后再采纳入库。"
        return SQLAssistantResponse(product=req.product, session_id=req.session_id, mode=mode, message=text, matches=matches, record=record, learned=learned)
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    current_sql = req.current_sql
    if not current_sql and req.current_sql_id:
        from anysql.harness.tasks import load_all_records

        pcfg = config.products[req.product]
        for record in load_all_records(pcfg.desc_dir):
            if record.statement.id == req.current_sql_id:
                current_sql = record.statement.raw_sql
                break

    try:
        mode, record, matches, learned = await state["pipeline"].assist_sql(
            product_id=req.product,
            message=req.message,
            current_sql=current_sql,
            top_k=req.top_k,
            match_threshold=req.match_threshold,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    if mode == "matched":
        text = "找到可直接复用的 SQL。"
    elif mode == "revised":
        text = "已根据修正意见改写 SQL。请确认正确后再采纳入库。"
    else:
        text = "没有足够高匹配的 SQL，已根据 Metadata 和既有知识生成草稿。请确认正确后再采纳入库。"

    return SQLAssistantResponse(
        product=req.product,
        session_id=req.session_id,
        mode=mode,
        message=text,
        matches=matches,
        record=record,
        learned=learned,
    )


@router.post("/learn")
async def learn_sql(req: SQLLearnRequest):
    """人工确认 SQL 正确后，纳入知识库。"""
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")
            config.products[req.product] = ProductRepository(session).to_product_config(product)
        generated = GeneratedSQL(
            sql=req.sql,
            summary=req.summary,
            business_meaning=req.business_meaning,
            usage_guide=req.usage_guide,
            parameters=req.parameters,
            tables=req.tables,
        )
        try:
            record = state["pipeline"]._draft_record(req.product, req.requirement, generated)
            record.statement.id = f"{req.product}_accepted_{uuid4().hex[:12]}"
            record.statement.source_file = "accepted"
            record.status = AnalysisStatus.SUCCESS
            record.analyzed_at = datetime.now()
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                SQLKnowledgeRepository(session).accept_generated(product.id, req.requirement, record)
                await PGVectorRepository(session, state["llm"], config.llm.embed_model).index_records([record], product.id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {"status": "learned", "learned": True, "accepted": True, "source": "metadata_generated", "record": record.model_dump(mode="json")}
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    generated = GeneratedSQL(
        sql=req.sql,
        summary=req.summary,
        business_meaning=req.business_meaning,
        usage_guide=req.usage_guide,
        parameters=req.parameters,
        tables=req.tables,
    )
    try:
        record = await state["pipeline"].learn_confirmed_sql(
            product_id=req.product,
            requirement=req.requirement,
            generated=generated,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"status": "learned", "record": record.model_dump(mode="json")}
