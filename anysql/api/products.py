"""
AnySQL API — 产品管理路由
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.config import DatabaseConfig, ProductConfig, save_config
from anysql.logger import logger
from anysql.models.schemas import ProductInfo, ProductUpsertRequest

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
            rules=pcfg.rules,
            sql_dir=pcfg.sql_dir,
            desc_dir=pcfg.desc_dir,
            metadata_dir=pcfg.metadata_dir,
            database_type=pcfg.database.type,
            database_host=pcfg.database.host,
            database_port=pcfg.database.port,
            database_service_name=pcfg.database.service_name,
            database_username=pcfg.database.username,
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
        rules=pcfg.rules,
        sql_dir=pcfg.sql_dir,
        desc_dir=pcfg.desc_dir,
        metadata_dir=pcfg.metadata_dir,
        database_type=pcfg.database.type,
        database_host=pcfg.database.host,
        database_port=pcfg.database.port,
        database_service_name=pcfg.database.service_name,
        database_username=pcfg.database.username,
        sql_count=len(scan_product_sqls(pcfg.sql_dir, product_id)),
        analyzed_count=len(get_analyzed_ids(pcfg.desc_dir)),
        db_configured=pcfg.database.is_configured,
    )


@router.post("", response_model=ProductInfo)
async def upsert_product(req: ProductUpsertRequest):
    """新增或更新产品配置"""
    state = _get_app_state()
    config = state["config"]
    existing = config.products.get(req.id)
    password = req.database_password
    if not password and existing:
        password = existing.database.password
    config.products[req.id] = ProductConfig(
        name=req.name,
        description=req.description,
        rules=req.rules,
        sql_dir=req.sql_dir,
        desc_dir=req.desc_dir,
        metadata_dir=req.metadata_dir,
        database=DatabaseConfig(
            type=req.database_type,
            host=req.database_host,
            port=req.database_port,
            service_name=req.database_service_name,
            username=req.database_username,
            password=password,
        ),
    )
    for d in [req.sql_dir, req.desc_dir, req.metadata_dir]:
        from pathlib import Path
        Path(d).mkdir(parents=True, exist_ok=True)
    save_config(config)
    logger.info(f"产品配置已更新: {req.id}")
    return await get_product(req.id)


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
