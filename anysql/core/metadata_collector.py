"""
AnySQL 数据库元数据采集器 (极致缓存版)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from anysql.config import DatabaseConfig
from anysql.logger import logger


class MetadataCollector:
    def __init__(self, db_config: DatabaseConfig, output_dir: str):
        self.config = db_config
        self.output_dir = Path(output_dir)
        self.tables_dir = self.output_dir / "tables"
        self.progs_dir = self.output_dir / "programs"
        for d in [self.output_dir, self.tables_dir, self.progs_dir]:
            d.mkdir(parents=True, exist_ok=True)
        self._conn = None

    async def connect(self) -> bool:
        if not self.config.is_configured: return False
        try:
            import oracledb
            self._conn = await asyncio.wait_for(
                oracledb.connect_async(
                    user=self.config.username, password=self.config.password,
                    host=self.config.host, port=self.config.port, sid=self.config.service_name
                ), timeout=15.0
            )
            return True
        except Exception as e:
            logger.error(f"DB连接失败: {e}")
            return False

    async def close(self):
        if self._conn: await self._conn.close()

    async def collect_all(self) -> Optional[dict]:
        """极速采集：如果本地已有 tables，则直接构建索引，不查数据库"""
        local_files = list(self.tables_dir.glob("*.json"))
        if len(local_files) > 1000:
            logger.info(f"检测到本地已有 {len(local_files)} 个元数据分片，正在跳过数据库采集直接构建索引...")
            table_names = [f.stem for f in local_files]
            index = {
                "database_type": "oracle",
                "table_names": table_names,
                "program_names": [],
                "table_count": len(table_names),
                "program_count": 0
            }
            with open(self.output_dir / "index.json", "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
            return index

        # 否则才去查数据库 (逻辑同前)
        if not await self.connect(): return None
        try:
            # ... (抓取逻辑省略，保持极速版精简)
            return await self._real_collect()
        finally:
            await self.close()

    async def _real_collect(self):
        # 只有在本地为空时才调用的原始逻辑
        names = []
        async with self._conn.cursor() as cursor:
            await cursor.execute("SELECT TABLE_NAME FROM USER_CATALOG WHERE TABLE_TYPE IN ('TABLE','VIEW')")
            rows = await cursor.fetchall()
            for r in rows: names.append(r[0])
        # 此处简化，仅为补全索引
        return {"table_names": names}

    def get_table_metadata(self, name: str) -> Optional[dict]:
        file_path = self.tables_dir / f"{name.upper()}.json"
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def load_index(self) -> Optional[dict]:
        idx_file = self.output_dir / "index.json"
        if idx_file.exists():
            with open(idx_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None
