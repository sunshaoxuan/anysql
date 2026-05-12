"""
AnySQL API — 分析管理路由 (整顿版)
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from anysql.logger import logger
from anysql.models.schemas import AnalysisProgress
from anysql.storage.repositories import JobRepository, ProductRepository

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

def _get_pipeline():
    from anysql.main import app_state
    return app_state["pipeline"]

@router.get("/{product}/progress", response_model=AnalysisProgress)
async def get_analysis_progress(product: str):
    """获取解析实时进度 (日本語)"""
    pipeline = _get_pipeline()
    from anysql.main import app_state
    storage = app_state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_row = ProductRepository(session).get_by_code(product)
            if not product_row:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
            job = JobRepository(session).latest(product_row.id, "sql_analysis")
            if not job:
                return AnalysisProgress(product=product)
            return JobRepository.to_progress(job, product)
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    progress = pipeline.get_progress(product)
    return progress

@router.post("/{product}/start")
async def start_analysis(product: str, background_tasks: BackgroundTasks, force: bool = False):
    """启动解析流水线"""
    pipeline = _get_pipeline()
    from anysql.main import app_state
    storage = app_state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_row = ProductRepository(session).get_by_code(product)
            if not product_row:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
            job = JobRepository(session).create("sql_analysis", product_row.id, {"product_code": product, "force": force})
            job_id = job.id
        if storage.queue:
            storage.queue.enqueue("anysql.worker_tasks.sql_analysis", job_id, product, force)
        return {"message": f"解析ジョブを開始しました: {product}", "status": "queued", "job_id": job_id}
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    progress = pipeline.get_progress(product)
    if progress.status == "running":
        return {"message": f"解析プロセスは実行中です: {product}", "status": "running"}
    # 在后台运行解析，不阻塞前端
    background_tasks.add_task(pipeline.run, product, force)
    return {"message": f"解析プロセスを開始しました: {product}", "status": "started"}


@router.post("/{product}/metadata-index")
async def index_metadata(product: str):
    """手动重建产品 Metadata 向量索引。"""
    pipeline = _get_pipeline()
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    pcfg = pipeline.config.products[product]
    count = await pipeline.vector.index_metadata(product, pcfg.metadata_dir)
    return {"status": "indexed", "product": product, "count": count}


@router.post("/{product}/metadata-sync")
async def sync_metadata(product: str, background_tasks: BackgroundTasks):
    """手动启动数据库 Metadata 差异同步，并更新向量索引。"""
    pipeline = _get_pipeline()
    from anysql.main import app_state
    storage = app_state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_row = ProductRepository(session).get_by_code(product)
            if not product_row:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
            latest = JobRepository(session).latest(product_row.id, "metadata_delta_sync")
            if latest and latest.status == "running":
                return {"status": latest.status, "product": product, "job_id": latest.id}
            job = JobRepository(session).create("metadata_delta_sync", product_row.id, {"product_code": product, "reason": "manual"})
            job_id = job.id
        if storage.queue:
            storage.queue.enqueue("anysql.worker_tasks.metadata_delta_sync", job_id, product)
        return {"status": "queued", "product": product, "job_id": job_id}
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    status = pipeline.get_metadata_sync_status(product)
    if status.get("status") == "running":
        return status
    background_tasks.add_task(pipeline.sync_product_metadata, product)
    return {"status": "started", "product": product}


@router.get("/{product}/metadata-sync")
async def get_metadata_sync_status(product: str):
    """获取 Metadata 同步状态。"""
    pipeline = _get_pipeline()
    from anysql.main import app_state
    storage = app_state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product_row = ProductRepository(session).get_by_code(product)
            if not product_row:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
            job = JobRepository(session).latest(product_row.id, "metadata_delta_sync")
            if not job:
                return {"status": "idle", "product": product}
            return {
                "status": job.status,
                "product": product,
                "job_id": job.id,
                "total": job.total,
                "completed": job.completed,
                "failed": job.failed,
                "error": job.error,
                "result": job.result,
            }
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    return pipeline.get_metadata_sync_status(product)
