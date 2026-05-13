"""LLM-assisted table profile classification with deterministic guardrails."""

from __future__ import annotations

import json

from anysql.core.table_profiles import TableProfileData, infer_table_profile, profile_review_context


DOMAINS = ("employee", "transfer", "payroll", "organization", "unknown")
ROLES = ("business_fact", "history_fact", "master", "log", "work", "if_staging", "backup", "config", "unknown")


async def classify_table_profile(table_name: str, comment: str, columns: list[dict], llm) -> TableProfileData:
    fallback = infer_table_profile(table_name, comment, columns)
    context = profile_review_context(table_name, comment, columns)
    if not context["needs_llm_review"]:
        return fallback

    column_sample = columns[:80]
    prompt = f"""
Classify one product database table for an SQL assistant.

You must choose exactly one domain and one role from the allowed enum values.
Do not invent values. Prefer the actual table purpose over isolated column keywords.

Allowed domain values: {", ".join(DOMAINS)}
Allowed role values: {", ".join(ROLES)}

Definitions:
- employee: employee/person/part-time employee basic information and attributes.
- transfer: personnel transfer, appointment, issue/order history, or transfer check logs.
- master: main reference or basic information table used for business queries.
- business_fact/history_fact: transaction/history business fact table.
- log/work/if_staging/backup: non-business-query support tables unless the user explicitly asks for logs, work, interface, or backup data.

Current deterministic fallback:
{json.dumps(fallback.__dict__, ensure_ascii=False)}

Table evidence:
{json.dumps({"table_name": table_name, "comment": comment, "columns": column_sample, "marker_context": context}, ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "domain": "employee|transfer|payroll|organization|unknown",
  "role": "business_fact|history_fact|master|log|work|if_staging|backup|config|unknown",
  "confidence": 0.0,
  "reason": "short reason using table/comment/field evidence"
}}
""".strip()
    data = await llm.chat_json(
        prompt,
        system_prompt="You are a strict metadata table classifier. Return only valid JSON.",
        temperature=0.0,
    )
    domain = str(data.get("domain") or fallback.domain)
    role = str(data.get("role") or fallback.role)
    if domain not in DOMAINS or role not in ROLES:
        return fallback
    try:
        confidence = float(data.get("confidence", fallback.confidence))
    except (TypeError, ValueError):
        confidence = fallback.confidence
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or fallback.reason)[:500]
    return TableProfileData((table_name or "").upper(), domain, role, confidence, f"llm: {reason}", source="auto_llm")
