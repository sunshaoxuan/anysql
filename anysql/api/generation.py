"""
AnySQL API - SQL 生成与自动知识沉淀。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.models.schemas import (
    GeneratedSQL,
    SQLGenerationRequest,
    SQLGenerationResponse,
)

router = APIRouter(prefix="/api/generate", tags=["generation"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.post("/sql", response_model=SQLGenerationResponse)
async def generate_sql(req: SQLGenerationRequest):
    """
    根据产品 Metadata 和既有 SQL 知识生成 SQL，并自动加入知识库。
    """
    state = _get_app_state()
    config = state["config"]
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    pipeline = state["pipeline"]
    try:
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
    return SQLGenerationResponse(
        product=req.product,
        requirement=req.requirement,
        generated=generated,
        record=record,
        learned=True,
    )
