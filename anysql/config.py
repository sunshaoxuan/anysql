"""
AnySQL 配置管理模块

从 config.yaml 加载全局配置，使用 Pydantic 进行校验。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from anysql.logger import logger, setup_file_logging


# ---------------------------------------------------------------------------
# Pydantic 配置模型
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    """LLM 服务配置"""
    base_url: str = "http://ccnode.briconbric.com:22545"
    chat_model: str = "qwen3:14b"
    embed_model: str = "bge-m3"
    timeout: int = 120
    max_retries: int = 3
    temperature: float = 0.3


class VectorDBConfig(BaseModel):
    """向量数据库配置"""
    backend: str = "chroma"
    persist_dir: str = "./data/chroma_db"
    collection_prefix: str = "anysql"
    host: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None


class StorageConfig(BaseModel):
    """AnySQL 自身的系统数据存储配置"""
    backend: str = "postgresql"
    database_url: Optional[str] = None


class QueueConfig(BaseModel):
    """后台任务队列配置"""
    backend: str = "redis"
    redis_url: Optional[str] = None


class ServerConfig(BaseModel):
    """Web 服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8000


class DatabaseConfig(BaseModel):
    """数据库连接配置"""
    type: str = "oracle"
    host: Optional[str] = None
    port: int = 1521
    service_name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return all([self.host, self.service_name, self.username])


class ProductConfig(BaseModel):
    """产品定义"""
    physical_id: str = ""
    name: str
    description: str = ""
    rules: str = ""
    sql_dir: str
    desc_dir: str
    metadata_dir: str
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = "INFO"
    dir: str = "./data/logs"
    rotation: str = "10 MB"
    retention: str = "30 days"
    format: str = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} "
        "| {name}:{function}:{line} | {message}"
    )


class AppConfig(BaseModel):
    """应用全局配置"""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    products: dict[str, ProductConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

_config: Optional[AppConfig] = None
_config_path: Optional[Path] = None


def _find_config_file() -> Path:
    """从多个位置查找 config.yaml"""
    env_config = os.environ.get("ANYSQL_CONFIG")
    candidates = [
        Path(env_config) if env_config else None,
        Path(__file__).parent.parent / "config.yaml",
        Path("config.yaml"),
    ]
    for p in candidates:
        if p and p.exists():
            return p.resolve()
    raise FileNotFoundError(
        f"找不到 config.yaml，已搜索: {[str(c) for c in candidates]}"
    )


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """加载并验证配置文件"""
    global _config, _config_path

    if config_path:
        path = Path(config_path)
    else:
        path = _find_config_file()

    logger.info(f"加载配置文件: {path}")
    _config_path = path

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    _config = AppConfig(**raw)
    if os.environ.get("ANYSQL_STORAGE_BACKEND"):
        _config.storage.backend = os.environ["ANYSQL_STORAGE_BACKEND"]
    if os.environ.get("ANYSQL_DATABASE_URL"):
        _config.storage.database_url = os.environ["ANYSQL_DATABASE_URL"]
    if os.environ.get("ANYSQL_QUEUE_BACKEND"):
        _config.queue.backend = os.environ["ANYSQL_QUEUE_BACKEND"]
    if os.environ.get("ANYSQL_REDIS_URL"):
        _config.queue.redis_url = os.environ["ANYSQL_REDIS_URL"]
    if os.environ.get("ANYSQL_LLM_BASE_URL"):
        _config.llm.base_url = os.environ["ANYSQL_LLM_BASE_URL"]
    if os.environ.get("ANYSQL_SERVER_PORT"):
        _config.server.port = int(os.environ["ANYSQL_SERVER_PORT"])
    setup_file_logging(
        log_dir=_config.logging.dir,
        level=_config.logging.level,
        rotation=_config.logging.rotation,
        retention=_config.logging.retention,
        fmt=_config.logging.format,
    )

    # 确保目录存在
    for product_id, product in _config.products.items():
        if not product.physical_id:
            product.physical_id = str(uuid4())
        for d in [product.sql_dir, product.desc_dir, product.metadata_dir]:
            Path(d).mkdir(parents=True, exist_ok=True)

    Path(_config.vector_db.persist_dir).mkdir(parents=True, exist_ok=True)
    Path(_config.logging.dir).mkdir(parents=True, exist_ok=True)

    logger.info(
        f"配置加载完成: {len(_config.products)} 个产品, "
        f"LLM={_config.llm.chat_model}, "
        f"Embed={_config.llm.embed_model}"
    )

    return _config


def save_config(config: Optional[AppConfig] = None) -> None:
    """保存当前配置到 config.yaml。"""
    global _config, _config_path
    target = config or get_config()
    path = _config_path or _find_config_file()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            target.model_dump(mode="json"),
            f,
            allow_unicode=True,
            sort_keys=False,
        )
    _config = target
    logger.info(f"配置已保存: {path}")


def get_config() -> AppConfig:
    """获取当前配置（单例）"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
