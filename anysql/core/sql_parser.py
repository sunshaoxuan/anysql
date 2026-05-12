"""
AnySQL SQL 解析器

负责扫描SQL文件、分割语句、提取注释和表名。
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlparse

from anysql.logger import logger
from anysql.models.schemas import SQLStatement, StatementType


# ---------------------------------------------------------------------------
# 语句类型检测
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, StatementType] = {
    "SELECT": StatementType.SELECT,
    "INSERT": StatementType.INSERT,
    "UPDATE": StatementType.UPDATE,
    "DELETE": StatementType.DELETE,
    "CREATE": StatementType.CREATE,
    "ALTER": StatementType.ALTER,
    "DROP": StatementType.DROP,
    "MERGE": StatementType.MERGE,
}

# 表名提取正则（FROM / JOIN / INTO / UPDATE / TABLE 后的标识符）
_TABLE_PATTERN = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+"
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)",
    re.IGNORECASE,
)


def _detect_type(sql: str) -> StatementType:
    """检测 SQL 语句类型"""
    stripped = sql.strip().upper()
    for keyword, stype in _TYPE_MAP.items():
        if stripped.startswith(keyword):
            return stype
    return StatementType.OTHER


def _extract_tables(sql: str) -> list[str]:
    """从 SQL 中提取引用的表名"""
    matches = _TABLE_PATTERN.findall(sql)
    # 去重并保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        upper = m.upper()
        if upper not in seen:
            seen.add(upper)
            result.append(upper)
    return result


# ---------------------------------------------------------------------------
# SQL 文件解析
# ---------------------------------------------------------------------------

def _collect_leading_comments(lines: list[str], stmt_start_line: int) -> str:
    """
    向上搜索语句起始行之前的连续注释行。
    返回合并后的注释文本（去掉 -- 前缀）。
    """
    comments: list[str] = []
    idx = stmt_start_line - 1

    while idx >= 0:
        line = lines[idx].strip()
        if line.startswith("--"):
            # 去掉 -- 前缀和分隔线
            text = line.lstrip("-").strip()
            if text and not all(c in "*=-~" for c in text):
                comments.insert(0, text)
            idx -= 1
        elif line == "" or all(c in "*=-~" for c in line):
            # 空行或纯分隔线，继续向上
            idx -= 1
        else:
            break

    return " / ".join(comments) if comments else ""


def parse_sql_file(
    filepath: Path,
    product: str,
) -> list[SQLStatement]:
    """
    解析单个 SQL 文件，返回 SQLStatement 列表。

    1. 读取文件全文
    2. 使用 sqlparse 按分号分割语句
    3. 对每条语句提取前导注释、表名、类型
    """
    logger.info(f"解析SQL文件: {filepath.name}")

    content = filepath.read_text(encoding="utf-8")
    lines = content.splitlines()

    # sqlparse 分割语句
    parsed_stmts = sqlparse.split(content)

    statements: list[SQLStatement] = []
    file_stem = filepath.stem

    # 跟踪当前在原文中的搜索位置
    search_start = 0

    for idx, raw in enumerate(parsed_stmts):
        cleaned = raw.strip()
        if not cleaned:
            continue

        # 跳过纯注释块（没有实际SQL）
        parsed = sqlparse.parse(cleaned)
        if parsed:
            tokens = [
                t for t in parsed[0].tokens
                if t.ttype not in (
                    sqlparse.tokens.Whitespace,
                    sqlparse.tokens.Newline,
                    sqlparse.tokens.Comment.Single,
                    sqlparse.tokens.Comment.Multiline,
                )
            ]
            if not tokens:
                continue

        # 找到此语句在原文中的行号
        stmt_first_keyword = ""
        for t in (parsed[0].tokens if parsed else []):
            text = str(t).strip()
            if text and not text.startswith("--"):
                stmt_first_keyword = text.split()[0] if text.split() else ""
                break

        line_number = 0
        for li in range(search_start, len(lines)):
            line_stripped = lines[li].strip()
            if not line_stripped or line_stripped.startswith("--"):
                continue
            if stmt_first_keyword and stmt_first_keyword.upper() in line_stripped.upper():
                line_number = li + 1  # 1-indexed
                search_start = li + 1
                break

        # 提取前导注释
        comment_search_line = (line_number - 2) if line_number > 1 else 0
        comment = _collect_leading_comments(lines, comment_search_line + 1)

        # 从 raw SQL 中移除嵌入的注释（仅用于分析，保留原始 raw_sql）
        sql_only = "\n".join(
            line for line in cleaned.splitlines()
            if not line.strip().startswith("--")
        ).strip()

        if not sql_only:
            continue

        stmt = SQLStatement(
            id=f"{product}_{file_stem}_{idx:03d}",
            product=product,
            source_file=filepath.name,
            raw_sql=sql_only,
            comment=comment,
            tables=_extract_tables(sql_only),
            statement_type=_detect_type(sql_only),
            line_number=line_number,
        )
        statements.append(stmt)
        logger.debug(
            f"  [{idx:03d}] {stmt.statement_type.value} "
            f"tables={stmt.tables} comment='{stmt.comment[:40]}...'"
        )

    logger.info(f"  → 共解析 {len(statements)} 条SQL语句")
    return statements


def scan_product_sqls(
    sql_dir: str,
    product: str,
) -> list[SQLStatement]:
    """
    扫描产品 SQL 目录中的所有 .sql 文件并解析。
    """
    sql_path = Path(sql_dir)
    if not sql_path.exists():
        logger.warning(f"SQL目录不存在: {sql_path}")
        return []

    sql_files = sorted(sql_path.glob("*.sql"))
    if not sql_files:
        logger.warning(f"SQL目录为空: {sql_path}")
        return []

    logger.info(f"扫描产品 [{product}]: 发现 {len(sql_files)} 个SQL文件")

    all_stmts: list[SQLStatement] = []
    for f in sql_files:
        stmts = parse_sql_file(f, product)
        all_stmts.extend(stmts)

    logger.info(f"产品 [{product}] 共计 {len(all_stmts)} 条SQL语句")
    return all_stmts
