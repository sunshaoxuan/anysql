"""
AnySQL API — 分析管理路由 (整顿版)
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from anysql.logger import logger
from anysql.models.schemas import AnalysisProgress

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

def _get_pipeline():
    from anysql.main import app_state
    return app_state["pipeline"]

@router.get("/{product}/progress", response_model=AnalysisProgress)
async def get_analysis_progress(product: str):
    """获取解析实时进度 (日本語)"""
    pipeline = _get_pipeline()
    if product not in pipeline.config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
    progress = pipeline.get_progress(product)
    return progress

@router.post("/{product}/start")
async def start_analysis(product: str, background_tasks: BackgroundTasks, force: bool = False):
    """启动解析流水线"""
    pipeline = _get_pipeline()
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
