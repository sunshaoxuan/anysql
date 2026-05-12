"""
AnySQL 日志系统

基于 loguru 的结构化日志，支持文件轮转和控制台输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# 移除默认 handler
logger.remove()

# 控制台输出（彩色）
logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    ),
    level="DEBUG",
    colorize=True,
)

# 文件日志会在配置加载后初始化
_file_handler_id: int | None = None


def setup_file_logging(
    log_dir: str = "./data/logs",
    level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "30 days",
    fmt: str | None = None,
) -> None:
    """初始化文件日志"""
    global _file_handler_id

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    if _file_handler_id is not None:
        logger.remove(_file_handler_id)

    log_format = fmt or (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
        "{name}:{function}:{line} | {message}"
    )

    _file_handler_id = logger.add(
        str(log_path / "anysql_{time:YYYY-MM-DD}.log"),
        format=log_format,
        level=level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,  # 线程安全
    )

    logger.info(f"文件日志已初始化: dir={log_dir}, level={level}")


# 导出 logger 供其他模块使用
__all__ = ["logger", "setup_file_logging"]
