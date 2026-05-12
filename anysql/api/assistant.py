"""
AnySQL API - SQL 辅助对话。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.models.schemas import GeneratedSQL, SQLAssistantRequest, SQLAssistantResponse, SQLLearnRequest

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
