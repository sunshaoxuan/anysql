"""SQL cleanup helpers for LLM generated statements."""

from __future__ import annotations

import html
import re

import sqlparse


_FENCE_RE = re.compile(r"^```(?:sql|json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def decode_html_entities(value: str) -> str:
    text = str(value or "")
    for _ in range(5):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def clean_generated_sql(sql: str, allow_aliases: bool = False) -> str:
    text = decode_html_entities(sql)
    text = _FENCE_RE.sub("", text).strip()
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    text = text.replace('\\"', '"').replace("\\'", "'")
    text = text.replace("&#39;", "'").replace("&quot;", '"')
    text = re.sub(r"^\s*(SQL|sql)\s*:\s*", "", text).strip()
    text = _normalize_column_aliases(text, allow_aliases=allow_aliases)
    text = text.rstrip().rstrip(";")
    return f"{text};" if text else ""


def preserve_requirement_literal(sql: str, requirement: str, allow_aliases: bool = False) -> str:
    """Replace broken placeholder literals with the quoted value from the request."""
    text = clean_generated_sql(sql, allow_aliases=allow_aliases)
    values = re.findall(r"[\"“”'‘’]([^\"“”'‘’]{1,40})[\"“”'‘’]", str(requirement or ""))
    value = next((item.strip() for item in values if item.strip()), "")
    if not value:
        return _remove_unrequested_parameter_filters(text, requirement)
    escaped = value.replace("'", "''")
    text = re.sub(
        r"LIKE\s+'(?:\[[^'\]]+\]|:[A-Za-z0-9_]+|:[0-9]+)%'",
        f"LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\(\s*:[A-Za-z_]*(?:name|mei|shimei)[A-Za-z0-9_]*\s+IS\s+NULL\s+OR\s+([\w.]+)\s+LIKE\s+:[A-Za-z_]*(?:name|mei|shimei)[A-Za-z0-9_]*\s*\|\|\s*'%'\s*\)",
        rf"\1 LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"LIKE\s+:[A-Za-z_]*(?:name|mei|shimei)[A-Za-z0-9_]*\s*\|\|\s*'%'",
        f"LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"LIKE\s+:[A-Za-z_]*(?:name|mei|shimei)[A-Za-z0-9_]*",
        f"LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"LIKE\s+'\$_[A-Za-z0-9_]*(?:NAME|MEI|SHIMEI)[A-Za-z0-9_]*%'",
        f"LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"LIKE\s+''?:[A-Za-z_]*(?:name|mei|shimei)[A-Za-z0-9_]*'?'?%?'?",
        f"LIKE '{escaped}%'",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"LIKE\s+'%{2,}'", f"LIKE '{escaped}%'", text, flags=re.IGNORECASE)
    if any(word in str(requirement or "") for word in ("姓", "姓名", "名字", "氏名")):
        text = re.sub(
            r"((?:C?NAME|KANJI|KANA|MEI|SHIMEI|SIMEI|氏名)[\w.]*\s+LIKE\s+)'[^']*%'",
            rf"\1'{escaped}%'",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"((?:C?NAME|KANJI|KANA|MEI|SHIMEI|SIMEI|氏名)[\w.]*\s+LIKE\s+)'{re.escape(escaped)}%'\s*\|\|\s*:[A-Za-z_][A-Za-z0-9_]*\s*\|\|\s*'%'",
            rf"\1'{escaped}%'",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\n\s+(?:OR|AND)\s+[\w.]*?(?:SHAIN|EMPLOYEE|CEMPLOYEE|NO|ID)[\w.]*\s+LIKE\s+'{re.escape(escaped)}%';?",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\n\s+(?:OR|AND)\s+\(\s*:[A-Za-z_]*(?:shain|employee|cemployee|no|id)[A-Za-z0-9_]*\s+IS\s+NULL\s+OR\s+[\w.]*?(?:SHAIN|EMPLOYEE|CEMPLOYEE|NO|ID)[\w.]*\s*=\s*:[A-Za-z_]*(?:shain|employee|cemployee|no|id)[A-Za-z0-9_]*\s*\)",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"WHERE\s+[\w.]*?(?:SHAIN|EMPLOYEE|CEMPLOYEE|NO|ID)[\w.]*\s+LIKE\s+'[^']*'\s+OR\s+((?:[\w.]*?(?:C?NAME|KANJI|KANA|MEI|SHIMEI|SIMEI|氏名)[\w.]*)\s+LIKE\s+'{re.escape(escaped)}%')",
            r"WHERE \1",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\n\s+(?:AND|OR)\s+[\w.]*?(?:SHAIN|EMPLOYEE|CEMPLOYEE|NO|ID)[\w.]*\s*=\s*'[^']*'",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\n\s+(?:AND|OR)\s+[^\n;]*\$_PARAM_(?!NAME|MEI|SHIMEI)[^\n;]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+;", ";", text.rstrip())
    text = re.sub(r"\s+;", ";", text.rstrip())
    text = re.sub(r"\s+;", ";", text.rstrip())
    return f"{text.rstrip().rstrip(';')};" if text else ""


def _remove_unrequested_parameter_filters(sql: str, requirement: str) -> str:
    req = requirement or ""
    if not any(word in req for word in ("所有", "全部", "全件", "すべて", "all")):
        return sql
    text = re.sub(
        r"\n?WHERE\s+[\s\S]*?(?=(?:\nGROUP\s+BY|\nORDER\s+BY|\nUNION\b|;|$))",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+;", ";", text.rstrip())
    return f"{text.rstrip().rstrip(';')};" if text else ""


def strip_sql_comments(sql: str) -> str:
    text = decode_html_entities(sql)
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    lines = [re.sub(r"--.*$", "", line).rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def format_sql(sql: str, allow_aliases: bool = False) -> str:
    text = clean_generated_sql(sql, allow_aliases=allow_aliases)
    if not text:
        return ""
    return sqlparse.format(text, keyword_case="upper", reindent=True, strip_comments=False).strip()


def add_sql_header_comment(sql: str, summary: str, requirement: str, allow_aliases: bool = False) -> str:
    body = format_sql(strip_sql_comments(sql), allow_aliases=allow_aliases)
    if not body:
        return ""
    return "\n".join(
        [
            "-- AnySQL: Metadata と既存知識をもとに生成したSQLです。",
            "-- 条件: 必要に応じてWHERE句の値を調整してください。",
            body,
        ]
    )


def _normalize_column_aliases(sql: str, allow_aliases: bool = False) -> str:
    def replace_alias(match: re.Match[str]) -> str:
        alias = match.group(1).strip().strip('"')
        if not allow_aliases:
            return ""
        if all(ch.isascii() and (ch.isalnum() or ch in "_$#") for ch in alias):
            return match.group(0)
        return ""

    text = re.sub(r"\s+AS\s+(\"[^\"]+\"|[^\s,;]+)", replace_alias, sql, flags=re.IGNORECASE)
    alias_keywords = {"LIKE", "FROM", "WHERE", "AND", "OR", "ON", "JOIN", "ORDER", "GROUP", "BY"}
    return re.sub(
        r"(\b[A-Za-z_][A-Za-z0-9_.$#]*\b)\s+(\"[^\"]+\"|[^\s,;]+)(?=\s*(?:,|\nFROM\b|\r?\n))",
        lambda match: match.group(0)
        if match.group(1).upper() in alias_keywords or match.group(2).startswith("'") or match.group(2).strip().strip('"').isascii()
        else match.group(1),
        text,
        flags=re.IGNORECASE,
    )
