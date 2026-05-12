"""Deterministic table role catalog and intent policies."""

from __future__ import annotations

from dataclasses import dataclass
import re


DANGEROUS_ROLES = {"log", "work", "if_staging", "backup"}


@dataclass(frozen=True)
class TableProfileData:
    table_name: str
    domain: str
    role: str
    confidence: float
    reason: str
    source: str = "auto"


@dataclass(frozen=True)
class IntentPolicy:
    intent: str
    allowed_roles: tuple[str, ...]
    blocked_roles: tuple[str, ...]
    preferred_tables: tuple[str, ...]
    domain: str = "unknown"


POLICIES: dict[str, IntentPolicy] = {
    "employee_basic_name": IntentPolicy(
        intent="employee_basic_name",
        domain="employee",
        allowed_roles=("master",),
        blocked_roles=("log", "work", "if_staging", "backup"),
        preferred_tables=("DJND0110", "MAST_EMPLOYEES", "MAST_EMPLOYEES2"),
    ),
    "transfer_records": IntentPolicy(
        intent="transfer_records",
        domain="transfer",
        allowed_roles=("history_fact", "business_fact"),
        blocked_roles=("log", "work", "if_staging", "backup"),
        preferred_tables=("DKIDO_R", "DKIDO", "DHJKIDO_R", "DHJKIDO"),
    ),
    "transfer_check_log": IntentPolicy(
        intent="transfer_check_log",
        domain="transfer",
        allowed_roles=("log",),
        blocked_roles=("work", "if_staging", "backup"),
        preferred_tables=("XCIDOCHKLOG",),
    ),
}


def resolve_intent(requirement: str) -> str:
    text = requirement or ""
    transfer = any(word in text for word in ("异动", "異動", "任免", "発令"))
    check_log = any(word in text for word in ("检查", "チェック", "错误", "エラー", "日志", "ログ", "校验", "検証", "メッセージ"))
    basic = any(word in text for word in ("基本情况", "基本信息", "基本資料", "基本情報"))
    employee = any(word in text for word in ("员工", "职员", "職員", "社員", "人的", "人の"))
    name = any(word in text for word in ("姓", "姓名", "名字", "氏名"))
    if transfer and check_log:
        return "transfer_check_log"
    if transfer:
        return "transfer_records"
    if basic and employee and name:
        return "employee_basic_name"
    return "unknown"


def policy_for_intent(intent: str) -> IntentPolicy | None:
    return POLICIES.get(intent)


def infer_table_profile(table_name: str, comment: str = "", columns: list[dict] | None = None) -> TableProfileData:
    table = (table_name or "").upper()
    text = f"{table} {comment or ''} " + " ".join(
        f"{str(col.get('column_name') or col.get('name') or '').upper()} {col.get('comment') or ''}"
        for col in (columns or [])
    )
    role = "unknown"
    reasons: list[str] = []
    if _has(text, "ログ", "LOG", "CHKLOG", "CMSG", "CERRVALUE", "チェックログ", "CHK", "チェック"):
        role, reasons = "log", ["log/check markers"]
    elif _has(text, "DELBK", "バックアップ") or table.endswith("_BK") or table.startswith("BK"):
        role, reasons = "backup", ["backup markers"]
    elif _has(text, "ワーク", "WORK", "一時", "入力", "取込") or table.startswith(("WK", "WPT")):
        role, reasons = "work", ["work/staging markers"]
    elif _has(text, "IF", "連携", "BRG", "コントロール", "制御") or table.startswith("DT_"):
        role, reasons = "if_staging", ["interface/staging markers"]
    elif table.endswith("_R") or _has(text, "累積", "履歴"):
        role, reasons = "history_fact", ["history markers"]
    elif _has(text, "マスタ", "MST", "基本情報DB"):
        role, reasons = "master", ["master/basic-db markers"]
    elif _has(text, "異動情報", "任免", "発令"):
        role, reasons = "business_fact", ["business fact markers"]

    domain = "unknown"
    if _has(text, "異動情報", "任免", "発令", "IDO", "KIDO"):
        domain = "transfer"
    elif _has(text, "個人基本情報", "社員", "職員", "氏名", "CNAMEKNJ", "CNAMEKNA"):
        domain = "employee"
    elif _has(text, "給与", "賞与"):
        domain = "payroll"
    elif _has(text, "所属", "組織", "部門"):
        domain = "organization"

    preferred = {
        "DKIDO": ("transfer", "business_fact", 0.95, "standard transfer fact table"),
        "DKIDO_R": ("transfer", "history_fact", 0.96, "standard transfer history table"),
        "DHJKIDO": ("transfer", "business_fact", 0.9, "part-time transfer fact table"),
        "DHJKIDO_R": ("transfer", "history_fact", 0.91, "part-time transfer history table"),
        "XCIDOCHKLOG": ("transfer", "log", 0.98, "transfer check log table"),
        "DJND0110": ("employee", "master", 0.96, "personal basic master table"),
    }
    if table in preferred:
        domain, role, confidence, reason = preferred[table]
        return TableProfileData(table, domain, role, confidence, reason)

    confidence = 0.85 if role != "unknown" or domain != "unknown" else 0.2
    return TableProfileData(table, domain, role, confidence, ", ".join(reasons) or "no strong markers")


def is_table_allowed(profile: object, policy: IntentPolicy | None) -> bool:
    if not policy:
        return True
    role = getattr(profile, "role", "") or ""
    if role in policy.blocked_roles:
        return False
    return role in policy.allowed_roles


def table_policy_score(table_name: str, profile: object | None, policy: IntentPolicy | None) -> float:
    if not profile:
        return -0.2
    role = getattr(profile, "role", "") or ""
    table = (table_name or "").upper()
    if policy:
        if role in policy.blocked_roles:
            return -1.0
        score = 0.25 if role in policy.allowed_roles else -0.2
        if table in policy.preferred_tables:
            score += 0.35 - (policy.preferred_tables.index(table) * 0.03)
        if getattr(profile, "domain", "") == policy.domain:
            score += 0.15
        return score
    return -0.25 if role in DANGEROUS_ROLES else 0.0


def _has(text: str, *patterns: str) -> bool:
    value = text.upper()
    return any((pattern.upper() in value) if re.search(r"[A-Za-z_]", pattern) else pattern in text for pattern in patterns)
