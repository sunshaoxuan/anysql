"""
AnySQL 主程序 — 最终稳健版
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from anysql import __version__
from anysql.config import load_config
from anysql.core.agent_engine import AgentEngine
from anysql.core.llm_client import LLMClient
from anysql.core.vector_engine import VectorEngine
from anysql.harness.pipeline import AnalysisPipeline
from anysql.logger import logger

# 全局应用状态
app_state = {}
BASE_DIR = Path(__file__).resolve().parent


async def _daily_metadata_sync_loop(pipeline: AnalysisPipeline):
    """每日执行一次产品 Metadata 差异同步。"""
    try:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            logger.info("每日 Metadata 差异同步开始")
            await pipeline.sync_all_metadata()
    except asyncio.CancelledError:
        logger.info("每日 Metadata 差异同步任务已停止")
        raise

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=== AnySQL 启动中... ===")
    config = load_config()
    app_state["config"] = config
    
    llm = LLMClient(
        base_url=config.llm.base_url,
        chat_model=config.llm.chat_model,
        embed_model=config.llm.embed_model,
        timeout=config.llm.timeout,
        max_retries=config.llm.max_retries,
        temperature=config.llm.temperature,
    )
    vector = VectorEngine(config.vector_db.persist_dir, config.vector_db.collection_prefix, llm)
    agent = AgentEngine(llm)
    pipeline = AnalysisPipeline(config, llm, vector, agent)
    
    app_state.update({
        "llm": llm,
        "vector": vector,
        "agent": agent,
        "pipeline": pipeline,
        "metadata_sync_task": asyncio.create_task(_daily_metadata_sync_loop(pipeline)),
    })
    
    logger.info("=== AnySQL 已就绪 ===")
    yield
    sync_task = app_state.get("metadata_sync_task")
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
    await llm.close()
    logger.info("=== AnySQL 正在关闭... ===")

app = FastAPI(title="AnySQL", version=__version__, lifespan=lifespan)

# 挂载静态资源和模板
app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

# 包含 API 路由
from anysql.api.analysis import router as analysis_router
from anysql.api.assistant import router as assistant_router
from anysql.api.generation import router as generation_router
from anysql.api.products import router as products_router
from anysql.api.search import router as search_router
app.include_router(analysis_router)
app.include_router(assistant_router)
app.include_router(generation_router)
app.include_router(products_router)
app.include_router(search_router)

# 页面路由
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    config = app_state.get("config")
    return templates.TemplateResponse("index.html", {"request": request, "products": config.products if config else {}})

@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    config = app_state.get("config")
    return templates.TemplateResponse("products.html", {"request": request, "products": config.products if config else {}})

@app.get("/analysis", response_class=HTMLResponse)
async def analysis_page(request: Request):
    config = app_state.get("config")
    return templates.TemplateResponse("analysis.html", {"request": request, "products": config.products if config else {}})

@app.get("/sql/{sql_id}", response_class=HTMLResponse)
async def sql_detail_page(request: Request, sql_id: str):
    """SQL 详情页 (支持中文/日文 ID)"""
    from anysql.harness.tasks import load_all_records
    decoded_id = urllib.parse.unquote(sql_id)
    config = app_state.get("config")
    
    record = None
    if config:
        for pid, pcfg in config.products.items():
            records = load_all_records(pcfg.desc_dir)
            for r in records:
                if r.statement.id == decoded_id:
                    record = r
                    break
            if record: break
    
    if not record:
        return HTMLResponse("<h1>Record Not Found</h1>", status_code=404)
        
    return templates.TemplateResponse("sql_detail.html", {"request": request, "record": record})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
