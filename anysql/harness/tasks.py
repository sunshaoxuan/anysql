"""
AnySQL Harness 任务处理 (参数校正版)
"""

from __future__ import annotations

import json
from pathlib import Path
from anysql.core.agent_engine import AgentEngine
from anysql.logger import logger
from anysql.models.schemas import (
    AnalysisStatus,
    SQLAnalysis,
    SQLRecord,
    SQLStatement,
)


def load_all_records(desc_dir: str | Path) -> list[SQLRecord]:
    records = []
    desc_path = Path(desc_dir)
    if not desc_path.exists():
        return []
    for f in sorted(desc_path.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as f_in:
                records.append(SQLRecord.model_validate(json.load(f_in)))
        except Exception as e:
            logger.warning(f"跳过无效分析记录 {f}: {e}")
    return records


def get_analyzed_ids(desc_dir: str | Path) -> set[str]:
    return {r.statement.id for r in load_all_records(desc_dir) if r.status == AnalysisStatus.SUCCESS}


def save_record(output_dir: Path, record: SQLRecord):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record.statement.id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)


async def analyze_single_sql(
    agent: AgentEngine,  # AgentEngine 会自动传给第一个参数
    stmt: SQLStatement, 
    db_context: str = "",
    **kwargs
) -> SQLRecord:
    """调用 Agent 分析单条 SQL (参数顺序已校正)"""
    try:
        prompt = (
            f"Analyze SQL:\n{stmt.raw_sql}\n\n"
            f"Comment:\n{stmt.comment or '(none)'}\n\n"
            f"Database metadata:\n{db_context or '(none)'}"
        )
        analysis = await agent.execute_task(prompt, SQLAnalysis)

        return SQLRecord(
            statement=stmt,
            analysis=analysis,
            status=AnalysisStatus.SUCCESS
        )
    except Exception as e:
        logger.error(f"分析失败 {stmt.id}: {e}")
        return SQLRecord(
            statement=stmt,
            status=AnalysisStatus.FAILED,
            error_message=str(e)
        )
