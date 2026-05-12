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

    async def collect_all(self, use_cache: bool = True) -> Optional[dict]:
        """采集可用表/字段元数据；已有大量本地分片时只重建索引。"""
        local_files = list(self.tables_dir.glob("*.json"))
        if use_cache and len(local_files) > 1000:
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

        if not await self.connect(): return None
        try:
            return await self._real_collect()
        finally:
            await self.close()

    async def _real_collect(self):
        tables: dict[str, dict] = {}
        async with self._conn.cursor() as cursor:
            await cursor.execute("""
                SELECT table_name, comments
                FROM user_tab_comments
                WHERE table_type IN ('TABLE', 'VIEW')
                ORDER BY table_name
            """)
            rows = await cursor.fetchall()
            for table_name, comments in rows:
                tables[str(table_name).upper()] = {
                    "name": str(table_name).upper(),
                    "comment": comments or "",
                    "columns": [],
                }

            await cursor.execute("""
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.data_length,
                    c.data_precision,
                    c.data_scale,
                    c.nullable,
                    cc.comments
                FROM user_tab_columns c
                LEFT JOIN user_col_comments cc
                  ON cc.table_name = c.table_name
                 AND cc.column_name = c.column_name
                WHERE c.table_name IN (
                    SELECT table_name
                    FROM user_tab_comments
                    WHERE table_type IN ('TABLE', 'VIEW')
                )
                ORDER BY c.table_name, c.column_id
            """)
            rows = await cursor.fetchall()
            for row in rows:
                table_name = str(row[0]).upper()
                tables.setdefault(table_name, {
                    "name": table_name,
                    "comment": "",
                    "columns": [],
                })
                tables[table_name]["columns"].append({
                    "name": row[1],
                    "data_type": row[2],
                    "data_length": row[3],
                    "data_precision": row[4],
                    "data_scale": row[5],
                    "nullable": row[6],
                    "comment": row[7] or "",
                })

        for table_name, data in tables.items():
            path = self.tables_dir / f"{table_name}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        index = {
            "database_type": "oracle",
            "table_names": sorted(tables),
            "program_names": [],
            "table_count": len(tables),
            "program_count": 0,
        }
        with open(self.output_dir / "index.json", "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        logger.info(f"数据库元数据采集完成: tables={len(tables)}")
        return index

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
