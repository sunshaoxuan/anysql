"""
AnySQL API — 产品管理路由
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.logger import logger
from anysql.models.schemas import ProductInfo

router = APIRouter(prefix="/api/products", tags=["products"])


def _get_app_state():
    """延迟导入避免循环依赖"""
    from anysql.main import app_state
    return app_state


@router.get("", response_model=list[ProductInfo])
async def list_products():
    """获取所有产品列表"""
    state = _get_app_state()
    config = state["config"]
    vector = state["vector"]

    products = []
    for pid, pcfg in config.products.items():
        stats = vector.get_collection_stats(pid)
        from anysql.harness.tasks import get_analyzed_ids
        from anysql.core.sql_parser import scan_product_sqls

        analyzed = len(get_analyzed_ids(pcfg.desc_dir))
        sql_count = len(scan_product_sqls(pcfg.sql_dir, pid))

        products.append(ProductInfo(
            id=pid,
            name=pcfg.name,
            description=pcfg.description,
            sql_count=sql_count,
            analyzed_count=analyzed,
            db_configured=pcfg.database.is_configured,
        ))

    logger.info(f"产品列表: {len(products)} 个产品")
    return products


@router.get("/{product_id}", response_model=ProductInfo)
async def get_product(product_id: str):
    """获取单个产品详情"""
    state = _get_app_state()
    config = state["config"]

    if product_id not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")

    pcfg = config.products[product_id]
    vector = state["vector"]
    stats = vector.get_collection_stats(product_id)

    from anysql.harness.tasks import get_analyzed_ids
    from anysql.core.sql_parser import scan_product_sqls

    return ProductInfo(
        id=product_id,
        name=pcfg.name,
        description=pcfg.description,
        sql_count=len(scan_product_sqls(pcfg.sql_dir, product_id)),
        analyzed_count=len(get_analyzed_ids(pcfg.desc_dir)),
        db_configured=pcfg.database.is_configured,
    )


@router.get("/{product_id}/sqls")
async def list_product_sqls(product_id: str):
    """获取产品的所有 SQL 记录"""
    state = _get_app_state()
    config = state["config"]

    if product_id not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")

    pcfg = config.products[product_id]

    from anysql.harness.tasks import load_all_records
    from anysql.core.sql_parser import scan_product_sqls

    # 已分析的记录
    records = load_all_records(pcfg.desc_dir)
    record_map = {r.statement.id: r for r in records}

    # 所有 SQL（含未分析的）
    statements = scan_product_sqls(pcfg.sql_dir, product_id)

    result = []
    for stmt in statements:
        if stmt.id in record_map:
            rec = record_map[stmt.id]
            result.append(rec.model_dump(mode="json"))
        else:
            result.append({
                "statement": stmt.model_dump(mode="json"),
                "analysis": None,
                "status": "pending",
            })

    return result
