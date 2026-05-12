"""
AnySQL API — 产品管理路由
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException

from anysql.config import DatabaseConfig, ProductConfig, save_config
from anysql.logger import logger
from anysql.models.schemas import ProductInfo, ProductUpsertRequest
from anysql.storage.repositories import JobRepository, ProductRepository, SQLKnowledgeRepository

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
    storage = state.get("storage")

    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            products = []
            product_repo = ProductRepository(session)
            sql_repo = SQLKnowledgeRepository(session)
            for product in product_repo.list():
                sql_count, analyzed = sql_repo.count_for_product(product.id)
                products.append(product_repo.to_info(product, sql_count, analyzed))
            return products

    products = []
    for pid, pcfg in config.products.items():
        stats = vector.get_collection_stats(pid)
        from anysql.harness.tasks import get_analyzed_ids
        from anysql.core.sql_parser import scan_product_sqls

        analyzed = len(get_analyzed_ids(pcfg.desc_dir))
        sql_count = len(scan_product_sqls(pcfg.sql_dir, pid))

        products.append(ProductInfo(
            id=pid,
            physical_id=pcfg.physical_id or pid,
            code=pid,
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
            database_password=pcfg.database.password,
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
    storage = state.get("storage")

    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_repo = ProductRepository(session)
            product = product_repo.get_by_code(product_id)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")
            sql_count, analyzed = SQLKnowledgeRepository(session).count_for_product(product.id)
            return product_repo.to_info(product, sql_count, analyzed)

    if product_id not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")

    pcfg = config.products[product_id]
    vector = state["vector"]
    stats = vector.get_collection_stats(product_id)

    from anysql.harness.tasks import get_analyzed_ids
    from anysql.core.sql_parser import scan_product_sqls

    return ProductInfo(
        id=product_id,
        physical_id=pcfg.physical_id or product_id,
        code=product_id,
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
        database_password=pcfg.database.password,
        sql_count=len(scan_product_sqls(pcfg.sql_dir, product_id)),
        analyzed_count=len(get_analyzed_ids(pcfg.desc_dir)),
        db_configured=pcfg.database.is_configured,
    )


@router.post("", response_model=ProductInfo)
async def upsert_product(req: ProductUpsertRequest, background_tasks: BackgroundTasks):
    """新增或更新产品配置"""
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_repo = ProductRepository(session)
            job_repo = JobRepository(session)
            product, is_new = product_repo.upsert(req)
            sql_count, analyzed = SQLKnowledgeRepository(session).count_for_product(product.id)
            info = product_repo.to_info(product, sql_count, analyzed)
            job_id = None
            if is_new:
                job = job_repo.create("metadata_delta_sync", product.id, {"product_code": product.code, "reason": "initial"})
                job_id = job.id
        if job_id and storage.queue:
            storage.queue.enqueue("anysql.worker_tasks.metadata_delta_sync", job_id, req.code)
        return info
    original_id = None
    existing = None
    if req.physical_id:
        for pid, product in config.products.items():
            if (product.physical_id or pid) == req.physical_id:
                original_id = pid
                existing = product
                break

    if existing is None and req.code in config.products:
        original_id = req.code
        existing = config.products[req.code]

    is_new_product = existing is None
    physical_id = req.physical_id or (existing.physical_id if existing else "") or str(uuid4())
    if original_id and original_id != req.code:
        config.products.pop(original_id)
        pipeline = state.get("pipeline")
        if pipeline and original_id in pipeline._progress_map:
            pipeline._progress_map.pop(original_id, None)
    config.products[req.code] = ProductConfig(
        physical_id=physical_id,
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
            password=req.database_password,
        ),
    )
    for d in [req.sql_dir, req.desc_dir, req.metadata_dir]:
        from pathlib import Path
        Path(d).mkdir(parents=True, exist_ok=True)
    save_config(config)
    logger.info(f"产品配置已更新: {original_id or req.code} -> {req.code}")
    pipeline = state.get("pipeline")
    if is_new_product and pipeline:
        background_tasks.add_task(pipeline.sync_product_metadata, req.code)
    return await get_product(req.code)


@router.delete("/{product_id}")
async def delete_product(product_id: str):
    """删除产品配置，不删除磁盘上的产品数据。"""
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            if not ProductRepository(session).soft_delete(product_id):
                raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")
        return {"status": "deleted", "id": product_id}
    if product_id not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")
    config.products.pop(product_id)
    pipeline = state.get("pipeline")
    if pipeline:
        pipeline._progress_map.pop(product_id, None)
    save_config(config)
    logger.info(f"产品配置已删除: {product_id}")
    return {"status": "deleted", "id": product_id}


@router.get("/{product_id}/sqls")
async def list_product_sqls(product_id: str):
    """获取产品的所有 SQL 记录"""
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(product_id)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product_id}")
            return [r.model_dump(mode="json") for r in SQLKnowledgeRepository(session).list_records(product.id)]

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
