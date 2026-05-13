"""Structured models for the evidence-first Harness Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IntentUnit(BaseModel):
    name: str = "unknown"
    domain: str = "unknown"
    action: str = "select"
    entities: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    filters: list[dict] = Field(default_factory=list)
    sort: list[dict] = Field(default_factory=list)
    aggregation: list[dict] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    blocked_roles: list[str] = Field(default_factory=list)
    requires_join: bool = False


class IntentPlan(BaseModel):
    original: str
    units: list[IntentUnit] = Field(default_factory=list)
    conditions: list[dict] = Field(default_factory=list)
    multi_intent: bool = False


class EvidenceItem(BaseModel):
    node_id: str = ""
    source_type: str = ""
    source_id: str = ""
    facet: str = ""
    table: str = ""
    column: str = ""
    score: float = 0.0
    weight: float = 1.0
    evidence: str = ""
    reasons: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class EvidenceBundle(BaseModel):
    intent: str
    recommended_tables: list[dict] = Field(default_factory=list)
    recommended_fields: list[dict] = Field(default_factory=list)
    recommended_joins: list[dict] = Field(default_factory=list)
    recommended_predicates: list[dict] = Field(default_factory=list)
    sql_examples: list[dict] = Field(default_factory=list)
    feedback: list[dict] = Field(default_factory=list)
    blocked_candidates: list[dict] = Field(default_factory=list)
    score_breakdown: list[dict] = Field(default_factory=list)
    sufficient: bool = True
    reason: str = ""


class ValidationResult(BaseModel):
    valid: bool = True
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    missing_conditions: list[dict] = Field(default_factory=list)
    extra_conditions: list[str] = Field(default_factory=list)
    repaired: bool = False


class HarnessResult(BaseModel):
    agent_run_id: str
    intent_plan: dict
    evidence_bundles: list[dict]
    context_budget: dict
    retrieval_scores: list[dict]
    validation_result: dict
    repair_count: int = 0
    llm_call_count: int = 0
    invalid_reason: str = ""
    source: str = ""
    knowledge_gap_id: str | None = None
