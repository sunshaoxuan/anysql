"""SQL cleanup helpers for LLM generated statements."""

from __future__ import annotations

import html
import re

import sqlparse


_FENCE_RE = re.compile(r"^```(?:sql|json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def decode_html_entities(value: str) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def clean_generated_sql(sql: str) -> str:
    text = decode_html_entities(sql)
    text = _FENCE_RE.sub("", text).strip()
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    text = text.replace('\\"', '"').replace("\\'", "'")
    text = re.sub(r"^\s*(SQL|sql)\s*:\s*", "", text).strip()
    text = text.rstrip().rstrip(";")
    return f"{text};" if text else ""


def strip_sql_comments(sql: str) -> str:
    text = decode_html_entities(sql)
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    lines = [re.sub(r"--.*$", "", line).rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def format_sql(sql: str) -> str:
    text = clean_generated_sql(sql)
    if not text:
        return ""
    return sqlparse.format(text, keyword_case="upper", reindent=True, strip_comments=False).strip()


def add_sql_header_comment(sql: str, summary: str, requirement: str) -> str:
    body = format_sql(sql)
    comments = []
    if summary:
        comments.append(f"-- {summary.strip()}")
    if requirement:
        comments.append(f"-- Requirement: {requirement.strip()}")
    if not comments:
        return body
    return "\n".join(comments + [body])
