"""Evidence-first Harness Agent service backed by multi-dimensional RAG."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select

from anysql.config import AppConfig
from anysql.core.harness_models import EvidenceBundle, HarnessResult, IntentPlan, IntentUnit, ValidationResult
from anysql.core.query_expansion import expand_query_for_metadata
from anysql.core.sql_cleaner import clean_generated_sql, strip_sql_comments
from anysql.core.sql_parser import _extract_tables
from anysql.core.table_profiles import policy_for_intent, resolve_intent
from anysql.models.schemas import Complexity, GeneratedSQL, SQLAnalysis, SQLCandidateMatch, SQLRecord
from anysql.storage.models import MetadataColumn, MetadataTable
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.rag_repository import AgentRunRepository, RagRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository


@dataclass
class HarnessAssistResult:
    mode: str
    record: SQLRecord
    matches: list[SQLCandidateMatch]
    learned: bool
    harness: HarnessResult


class HarnessAgentService:
    """Runs SQL assistance as a preparation-heavy harness workflow."""

    def __init__(self, config: AppConfig, storage, llm, agent, pipeline):
        self.config = config
        self.storage = storage
        self.llm = llm
        self.agent = agent
        self.pipeline = pipeline
        self.workflow_backend = _workflow_backend()

    async def assist_sql(self, req) -> HarnessAssistResult:
        with self.storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            if not product:
                raise ValueError(f"产品不存在: {req.product}")
            product_cfg = ProductRepository(session).to_product_config(product)
            self.config.products[req.product] = product_cfg
            current_sql = req.current_sql
            if not current_sql and req.current_sql_id:
                existing = SQLKnowledgeRepository(session).get(req.current_sql_id)
                current_sql = existing.statement.raw_sql if existing else None
            run = AgentRunRepository(session).create(product.id, req.message, req.session_id)
            run_id = run.id

        try:
            intent_plan = build_intent_plan(req.message)
            query_ja = expand_query_for_metadata(req.message)
            with self.storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                run_repo = AgentRunRepository(session)
                run_repo.add_step(run_id, "intent_planner", intent_plan.model_dump(mode="json"))
                rag = RagRepository(session)
                stats = rag.stats(product.id)
                vector = PGVectorRepository(session, self.llm, self.config.llm.embed_model)
                if stats["total_nodes"] > 0:
                    evidence = await rag.hybrid_search(product.id, f"{req.message}\n{query_ja}", self.llm, self.config.llm.embed_model, top_k=32)
                else:
                    evidence = await _legacy_metadata_evidence(session, vector, product.id, req.message, query_ja)
                sql_candidates = await vector.search(query_ja, req.product, req.top_k)
                evidence_bundles = build_evidence_bundles(intent_plan, evidence)
                context = build_context(req.message, product.rules, intent_plan, evidence_bundles, sql_candidates, current_sql)
                run_repo.add_step(run_id, "knowledge_preparer", {
                    "rag_stats": stats,
                    "evidence_count": len(evidence),
                    "context_chars": len(context),
                    "workflow_backend": self.workflow_backend,
                })
                blocked_profiles = []
                retrieval_scores = [
                    {
                        "source_id": item.get("source_id"),
                        "facet": item.get("facet"),
                        "table": item.get("table"),
                        "column": item.get("column"),
                        "score": item.get("score"),
                        "reasons": item.get("reasons"),
                    }
                    for item in evidence[:24]
                ]

            matches: list[SQLCandidateMatch] = []
            if not current_sql:
                top_vector_score = max((item.score for item in sql_candidates), default=0.0)
                if top_vector_score >= max(0.68, req.match_threshold - 0.08):
                    matches = await self.pipeline._llm_match_candidates(req.product, f"{req.message}\n检索扩展: {query_ja}", sql_candidates)
                    if matches and matches[0].llm_score >= req.match_threshold:
                        with self.storage.database.session() as session:
                            record = SQLKnowledgeRepository(session).get(matches[0].sql_id)
                            if record:
                                record.statement.product = req.product
                                harness = self._complete_run(
                                    run_id,
                                    product_id=None,
                                    status="matched",
                                    intent_plan=intent_plan,
                                    evidence_bundles=evidence_bundles,
                                    retrieval_scores=retrieval_scores,
                                    validation_result=ValidationResult(valid=True),
                                    context_budget={"chars": len(context), "workflow_backend": self.workflow_backend},
                                    llm_call_count=1,
                                )
                                return HarnessAssistResult("matched", record, matches, False, harness)

            generated = await self._compose_sql(req.product, req.message, query_ja, context, req.use_aliases)
            generated.sql = clean_generated_sql(generated.sql, allow_aliases=req.use_aliases)
            record = self.pipeline._draft_record(req.product, req.message, generated, allow_aliases=req.use_aliases)
            attach_harness_snapshot(record, intent_plan, evidence_bundles, retrieval_scores, run_id)
            template_repaired = self._deterministic_repair(record, intent_plan, evidence_bundles)
            validation = self.validate_record(record, intent_plan, evidence_bundles)
            repair_count = 1 if template_repaired else 0
            llm_call_count = 1
            if not validation.valid:
                repaired = self._deterministic_repair(record, intent_plan, evidence_bundles)
                if repaired:
                    repair_count += 1
                    validation = self.validate_record(record, intent_plan, evidence_bundles)
                if not validation.valid and llm_call_count < 2:
                    fixed = await self._repair_sql(req.product, req.message, query_ja, context, record, validation, req.use_aliases)
                    fixed.sql = clean_generated_sql(fixed.sql, allow_aliases=req.use_aliases)
                    record = self.pipeline._draft_record(req.product, req.message, fixed, allow_aliases=req.use_aliases)
                    attach_harness_snapshot(record, intent_plan, evidence_bundles, retrieval_scores, run_id)
                    repair_count += 1
                    llm_call_count += 1
                    validation = self.validate_record(record, intent_plan, evidence_bundles)
                    if not validation.valid and self._deterministic_repair(record, intent_plan, evidence_bundles):
                        repair_count += 1
                        validation = self.validate_record(record, intent_plan, evidence_bundles)
            invalid_reason = "; ".join(validation.errors[:3]) if not validation.valid else ""
            record.metadata_snapshot = {
                **(record.metadata_snapshot or {}),
                "validation_warnings": [*validation.warnings, *validation.errors],
                "validation_result": validation.model_dump(mode="json"),
            }
            with self.storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                SQLKnowledgeRepository(session).save_draft(product.id, req.message, record, req.session_id)
            harness = self._complete_run(
                run_id,
                product_id=None,
                status="invalid" if invalid_reason else ("revised" if current_sql else "generated"),
                intent_plan=intent_plan,
                evidence_bundles=evidence_bundles,
                retrieval_scores=retrieval_scores,
                validation_result=validation,
                context_budget={"chars": len(context), "max_chars": 28000, "workflow_backend": self.workflow_backend},
                llm_call_count=llm_call_count,
                repair_count=repair_count,
                invalid_reason=invalid_reason,
            )
            return HarnessAssistResult("revised" if current_sql else "generated", record, matches, False, harness)
        except Exception:
            with self.storage.database.session() as session:
                run_repo = AgentRunRepository(session)
                run_repo.complete(
                    run_id,
                    "failed",
                    {},
                    [],
                    [],
                    {"valid": False, "errors": ["unhandled exception"]},
                    {},
                    0,
                    0,
                    "unhandled exception",
                )
            raise

    async def _compose_sql(self, product_code: str, requirement: str, query_ja: str, context: str, allow_aliases: bool) -> GeneratedSQL:
        alias_rule = "SELECT 列别名不要生成。" if not allow_aliases else "SELECT 列别名如确实需要只能使用 ASCII，禁止中文/日文别名。"
        prompt = f"""你是 AnySQL 的 SQLComposer。系统已经完成多维检索和证据筛选，你只能使用证据上下文中的表、字段、JOIN 和条件。

产品: {product_code}
用户需求:
{requirement}

日文检索意图:
{query_ja}

证据上下文:
{context}

硬性要求:
1. 不得编造证据上下文中不存在的表名、字段名、JOIN 或过滤条件。
2. 必须保留用户需求中的过滤条件，不得丢失，也不得添加无证据条件。
3. SQL 必须是可直接执行的 Oracle SQL，不能出现 HTML 实体、反斜杠转义、Markdown、JSON 字符串转义。
4. 注释只能使用日语；禁止中文注释。
5. 绑定参数名只能使用 ASCII 字母、数字和下划线，例如 :employee_no。
6. {alias_rule}
7. 输出 JSON，字段必须符合 GeneratedSQL schema。
"""
        return await self.agent.execute_task(prompt, GeneratedSQL)

    async def _repair_sql(self, product_code: str, requirement: str, query_ja: str, context: str, record: SQLRecord, validation: ValidationResult, allow_aliases: bool) -> GeneratedSQL:
        prompt = f"""你是 AnySQL 的 SQL repair agent。上一版 SQL 未通过确定性校验，只能根据证据上下文修复。

产品: {product_code}
用户需求:
{requirement}
日文检索意图:
{query_ja}

证据上下文:
{context}

上一版 SQL:
{record.statement.raw_sql}

校验错误:
{json.dumps(validation.model_dump(mode="json"), ensure_ascii=False)}

要求:
1. 只修复错误，不引入无证据表、字段、JOIN 或条件。
2. 参数名只能 ASCII。
3. 注释只能日语。
4. 输出 JSON，字段必须符合 GeneratedSQL schema。
"""
        return await self.agent.execute_task(prompt, GeneratedSQL)

    def validate_record(self, record: SQLRecord, intent_plan: IntentPlan, evidence_bundles: list[EvidenceBundle]) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        allowed_tables = {
            str(item.get("table", "")).upper()
            for bundle in evidence_bundles
            for item in bundle.recommended_tables
            if item.get("table")
        }
        allowed_columns_by_table: dict[str, set[str]] = {}
        for bundle in evidence_bundles:
            for item in bundle.recommended_fields:
                table = str(item.get("table", "")).upper()
                column = str(item.get("column", "")).upper()
                if table and column:
                    allowed_columns_by_table.setdefault(table, set()).add(column)
        used_tables = {table.upper() for table in record.statement.tables}
        if allowed_tables and not used_tables.issubset(allowed_tables):
            errors.append(f"SQL uses tables outside evidence bundle: {', '.join(sorted(used_tables - allowed_tables))}")
        sql = re.sub(r":[A-Za-z_][A-Za-z0-9_]*|:[0-9]+", "", strip_sql_comments(record.statement.raw_sql))
        if re.search(r":[^\s),;]+", strip_sql_comments(record.statement.raw_sql)):
            warnings.append("Parameter names should be ASCII identifiers.")
        unknown_columns = []
        keywords = {"SELECT", "FROM", "WHERE", "AND", "OR", "LIKE", "BETWEEN", "IN", "IS", "NULL", "NOT", "ORDER", "BY", "GROUP", "HAVING", "UNION", "ALL", "DISTINCT", "AS", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "ASC", "DESC", "TO_DATE"}
        allowed_all = set().union(*allowed_columns_by_table.values()) if allowed_columns_by_table else set()
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$#]*\b", sql):
            upper = token.upper()
            if upper in keywords or upper in used_tables or upper.startswith("_"):
                continue
            if allowed_all and upper not in allowed_all:
                unknown_columns.append(upper)
        if unknown_columns:
            errors.append(f"Unknown columns for evidence bundle: {', '.join(sorted(set(unknown_columns))[:10])}")
        text = intent_plan.original
        if any(word in text for word in ("姓", "姓名", "氏名", "名字")) and not re.search(r"(CNAME|NAME|氏名).+LIKE|LIKE.+(CNAME|NAME|氏名)", sql, flags=re.IGNORECASE):
            errors.append("Name filter from requirement is missing.")
        if resolve_intent(text) == "part_time_employee_hire_date":
            if "DJND3001" not in used_tables:
                errors.append("Part-time employee hire-date queries must use DJND3001.")
            if not re.search(r"\b(D?NINYO_DTE|SAIYO|HIRE)\b", sql, flags=re.IGNORECASE):
                errors.append("Hire/appointment date field is missing.")
        has_specific_filter = any(condition.get("kind") != "all_records" for condition in intent_plan.conditions)
        if (
            any(word in text for word in ("所有", "全部", "全件", "すべて", "all"))
            and not has_specific_filter
            and re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE)
        ):
            warnings.append("Requirement asks for all records but SQL contains WHERE; verify predicate is requested.")
        return ValidationResult(valid=not errors, warnings=warnings, errors=errors)

    def _deterministic_repair(self, record: SQLRecord, intent_plan: IntentPlan, evidence_bundles: list[EvidenceBundle]) -> bool:
        text = intent_plan.original or ""
        if resolve_intent(text) == "part_time_employee_hire_date" and "DJND3001" not in {table.upper() for table in record.statement.tables}:
            surname = ""
            for condition in intent_plan.conditions:
                if condition.get("kind") == "name":
                    surname = str(condition.get("value") or "").strip()
                    break
            where = f"\nWHERE CNAMEKNJ LIKE '{surname}%'" if surname else ""
            record.statement.raw_sql = (
                "-- AnySQL: 非常勤職員の任用年月日を取得します。\n"
                "-- 条件: 氏名の先頭一致で対象者を絞り込みます。\n"
                "SELECT CSHAINNO,\n"
                "       CNAMEKNJ,\n"
                "       NINYO_DTE,\n"
                "       DNINYO_DTE\n"
                "FROM DJND3001"
                f"{where};"
            )
            record.statement.tables = ["DJND3001"]
            record.analysis = SQLAnalysis(
                summary="非常勤職員の姓で任用年月日を検索",
                business_context=["非常勤職員基本情報DBから任用日を確認する"],
                tables_involved={"DJND3001": "非常勤職員基本情報DB"},
                usage_guide="氏名の先頭一致条件を変更して対象者を絞り込みます。",
                category=["employee", "part_time", "hire_date"],
                complexity=Complexity.SIMPLE,
                keywords=["DJND3001", "CNAMEKNJ", "NINYO_DTE", "DNINYO_DTE"],
            )
            return True
        if resolve_intent(text) == "transfer_records" and any(word in text.lower() for word in ("所有", "全部", "全件", "すべて", "all")):
            table = "DKIDO_R"
            if any(word in text for word in ("当前", "現在", "未累積")):
                table = "DKIDO"
            if any(word in text for word in ("非常勤", "非職", "非职", "パート", "part-time", "parttime")):
                table = "DHJKIDO_R" if table.endswith("_R") else "DHJKIDO"
            record.statement.raw_sql = "-- AnySQL: 異動履歴を取得します。\n-- 条件: 必要に応じて職員番号や氏名のWHERE条件を追加してください。\nSELECT *\nFROM " + table + ";"
            record.statement.tables = [table]
            return True
        if resolve_intent(text) == "transfer_check_log" and "XCIDOCHKLOG" not in {table.upper() for table in record.statement.tables}:
            record.statement.raw_sql = (
                "-- AnySQL: 異動チェックログを取得します。\n"
                "-- 条件: 必要に応じて職員番号、氏名、発令年月日、メッセージ内容のWHERE条件を追加してください。\n"
                "SELECT CLIENT_ID,\n"
                "       CCOMPKB,\n"
                "       CSHAINNO,\n"
                "       DHTREINGB_DTE,\n"
                "       CHTREINGB,\n"
                "       NNMN_IDO_CDE,\n"
                "       NNMN_IDO_NME,\n"
                "       CNAMEKNJ,\n"
                "       CERRVALUE,\n"
                "       NMSGSHUB,\n"
                "       CMSG,\n"
                "       CMNUSER,\n"
                "       DMNDATE\n"
                "FROM XCIDOCHKLOG;"
            )
            record.statement.tables = ["XCIDOCHKLOG"]
            return True
        return False

    def _complete_run(
        self,
        run_id: str,
        product_id,
        status: str,
        intent_plan: IntentPlan,
        evidence_bundles: list[EvidenceBundle],
        retrieval_scores: list[dict],
        validation_result: ValidationResult,
        context_budget: dict,
        llm_call_count: int,
        repair_count: int = 0,
        invalid_reason: str = "",
    ) -> HarnessResult:
        with self.storage.database.session() as session:
            AgentRunRepository(session).complete(
                run_id,
                status,
                intent_plan.model_dump(mode="json"),
                [bundle.model_dump(mode="json") for bundle in evidence_bundles],
                retrieval_scores,
                validation_result.model_dump(mode="json"),
                context_budget,
                llm_call_count,
                repair_count,
                invalid_reason,
            )
        return HarnessResult(
            agent_run_id=run_id,
            intent_plan=intent_plan.model_dump(mode="json"),
            evidence_bundles=[bundle.model_dump(mode="json") for bundle in evidence_bundles],
            context_budget=context_budget,
            retrieval_scores=retrieval_scores,
            validation_result=validation_result.model_dump(mode="json"),
            repair_count=repair_count,
            llm_call_count=llm_call_count,
            invalid_reason=invalid_reason,
        )


def build_intent_plan(requirement: str) -> IntentPlan:
    base_intent = resolve_intent(requirement)
    policy = policy_for_intent(base_intent)
    unit = IntentUnit(
        name=base_intent,
        domain=policy.domain if policy else "unknown",
        entities=_entities(requirement),
        output_fields=_output_fields(requirement),
        filters=_filters(requirement),
        sort=_sorts(requirement),
        aggregation=_aggregations(requirement),
        allowed_roles=list(policy.allowed_roles) if policy else [],
        blocked_roles=list(policy.blocked_roles) if policy else ["log", "work", "if_staging", "backup"],
        requires_join=_requires_join(requirement),
    )
    units = [unit]
    if _mentions_employee_basic(requirement) and _mentions_transfer(requirement) and base_intent != "transfer_records":
        transfer_policy = policy_for_intent("transfer_records")
        units.append(IntentUnit(
            name="transfer_records",
            domain="transfer",
            entities=unit.entities,
            filters=unit.filters,
            allowed_roles=list(transfer_policy.allowed_roles),
            blocked_roles=list(transfer_policy.blocked_roles),
            requires_join=True,
        ))
    return IntentPlan(original=requirement, units=units, conditions=unit.filters, multi_intent=len(units) > 1)


def build_evidence_bundles(intent_plan: IntentPlan, evidence: list[dict]) -> list[EvidenceBundle]:
    bundles: list[EvidenceBundle] = []
    for unit in intent_plan.units:
        policy = policy_for_intent(unit.name)
        filtered = []
        blocked = []
        for item in evidence:
            role = str((item.get("meta") or {}).get("role") or "")
            table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
            if policy and role and role in policy.blocked_roles:
                blocked.append(item)
                continue
            if policy and policy.domain != "unknown":
                domain = str((item.get("meta") or {}).get("domain") or "")
                if domain and domain != policy.domain and item.get("facet") in {"table_profile", "table_semantic", "column_semantic"}:
                    continue
            if unit.name == "transfer_records" and table == "XCIDOCHKLOG":
                blocked.append(item)
                continue
            filtered.append(item)
        if unit.name == "transfer_records":
            preferred = {"DKIDO_R", "DKIDO"}
            if any(word in intent_plan.original for word in ("非常勤", "非職", "非职", "パート")):
                preferred.update({"DHJKIDO_R", "DHJKIDO"})
            preferred_items = [item for item in filtered if str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper() in preferred]
            if preferred_items:
                filtered = preferred_items
            for item in filtered:
                table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
                if table == "DKIDO_R" and not any(word in intent_plan.original for word in ("当前", "現在", "未累積")):
                    item["score"] = float(item.get("score") or 0) + 25
                elif table == "DKIDO":
                    item["score"] = float(item.get("score") or 0) + 10
        if unit.name == "transfer_check_log":
            preferred_items = [
                item for item in filtered
                if str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper() == "XCIDOCHKLOG"
            ]
            if preferred_items:
                filtered = preferred_items
            for item in filtered:
                table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
                if table == "XCIDOCHKLOG":
                    item["score"] = float(item.get("score") or 0) + 25
        if unit.name == "part_time_employee_hire_date":
            preferred_items = [
                item for item in filtered
                if str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper() == "DJND3001"
            ]
            if preferred_items:
                filtered = preferred_items
            for column, score in (("NINYO_DTE", 22.0), ("DNINYO_DTE", 21.5), ("CNAMEKNJ", 21.0), ("CSHAINNO", 20.5)):
                filtered.append({
                    "source_id": f"policy:DJND3001.{column}",
                    "facet": "column_semantic",
                    "table": "DJND3001",
                    "column": column,
                    "score": score,
                    "reasons": ["intent_policy"],
                    "evidence": f"DJND3001.{column} is required evidence for part-time employee hire-date queries.",
                    "meta": {"table": "DJND3001", "column": column, "domain": "employee", "role": "master"},
                })
            for item in filtered:
                table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
                column = str(item.get("column") or (item.get("meta") or {}).get("column") or "").upper()
                if table == "DJND3001":
                    item["score"] = float(item.get("score") or 0) + 30
                if column in {"NINYO_DTE", "DNINYO_DTE", "CNAMEKNJ", "CNAMEKNA", "CSHAINNO"}:
                    item["score"] = float(item.get("score") or 0) + 10
        tables = _top_tables(filtered, limit=4)
        fields = _top_fields(filtered, tables, limit=40)
        bundle = EvidenceBundle(
            intent=unit.name,
            recommended_tables=tables,
            recommended_fields=fields,
            recommended_predicates=[_compact(item) for item in filtered if item.get("facet") == "predicate_pattern"][:12],
            sql_examples=[_compact(item) for item in filtered if item.get("facet") in {"sql_intent", "sql_structure"}][:6],
            feedback=[_compact(item) for item in filtered if item.get("facet") == "feedback_node"][:6],
            blocked_candidates=[_compact(item) for item in blocked[:12]],
            score_breakdown=[_compact(item) for item in filtered[:18]],
            sufficient=bool(tables and fields),
            reason="multi-dimensional evidence bundle" if tables and fields else "insufficient table or field evidence",
        )
        bundles.append(bundle)
    return bundles


def build_context(requirement: str, product_rules: str, intent_plan: IntentPlan, bundles: list[EvidenceBundle], sql_candidates, current_sql: str | None = None) -> str:
    sections = [
        "Product rules:\n" + (product_rules.strip() or "No product-specific rules."),
        "Intent plan:\n" + json.dumps(intent_plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        "Original requirement conditions:\n" + json.dumps(intent_plan.conditions, ensure_ascii=False),
    ]
    for bundle in bundles:
        sections.append(
            f"Evidence bundle for {bundle.intent}:\n"
            f"Recommended tables:\n{json.dumps(bundle.recommended_tables, ensure_ascii=False, indent=2)}\n"
            f"Recommended fields:\n{json.dumps(bundle.recommended_fields[:40], ensure_ascii=False, indent=2)}\n"
            f"Predicate patterns:\n{json.dumps(bundle.recommended_predicates[:12], ensure_ascii=False, indent=2)}\n"
            f"Accepted SQL examples:\n{json.dumps(bundle.sql_examples[:4], ensure_ascii=False, indent=2)}\n"
            f"Positive feedback:\n{json.dumps(bundle.feedback[:4], ensure_ascii=False, indent=2)}\n"
            f"Blocked candidates:\n{json.dumps(bundle.blocked_candidates[:8], ensure_ascii=False, indent=2)}"
        )
    examples = [
        {"sql_id": item.sql_id, "score": item.score, "summary": item.summary, "sql": item.raw_sql[:1200]}
        for item in sql_candidates[:5]
    ]
    sections.append("Existing SQL vector examples:\n" + json.dumps(examples, ensure_ascii=False, indent=2))
    if current_sql:
        sections.append("Current SQL for revision:\n" + current_sql)
    context = "\n\n---\n\n".join(sections)
    return context[:28000]


def attach_harness_snapshot(record: SQLRecord, intent_plan: IntentPlan, evidence_bundles: list[EvidenceBundle], retrieval_scores: list[dict], run_id: str) -> None:
    record.metadata_snapshot = {
        **(record.metadata_snapshot or {}),
        "agent_run_id": run_id,
        "intent_plan": intent_plan.model_dump(mode="json"),
        "evidence_bundles": [bundle.model_dump(mode="json") for bundle in evidence_bundles],
        "retrieval_scores": retrieval_scores,
    }


async def _legacy_metadata_evidence(session, vector: PGVectorRepository, product_id: str, requirement: str, query_ja: str) -> list[dict]:
    vector_hits = await vector.search_metadata(query_ja, product_id, 8)
    text_hits = vector.search_metadata_text(f"{requirement} {query_ja}", product_id, 10)
    intent_hits = vector.search_metadata_by_intent(requirement, product_id, 8)
    merged = []
    seen = set()
    for item in [
        *intent_hits,
        *vector.apply_profile_policy(text_hits, requirement, product_id),
        *vector.apply_profile_policy(vector_hits, requirement, product_id),
    ]:
        table = str(item.get("table") or item.get("id") or "").upper()
        if not table or table in seen:
            continue
        seen.add(table)
        profile = item.get("profile") or {}
        merged.append({
            "node_id": f"legacy:{table}:table",
            "source_type": "legacy_metadata",
            "source_id": table,
            "facet": "table_semantic",
            "table": table,
            "column": "",
            "score": float(item.get("score") or 0),
            "weight": 1.0,
            "evidence": str(item.get("document") or "")[:2400],
            "reasons": [item.get("source") or "legacy"],
            "meta": {"table": table, "domain": profile.get("domain"), "role": profile.get("role")},
        })
    for table in list(seen)[:8]:
        table_row = session.scalar(select(MetadataTable).where(MetadataTable.product_id == product_id, MetadataTable.table_name == table))
        if not table_row:
            continue
        columns = list(session.scalars(select(MetadataColumn).where(MetadataColumn.metadata_table_id == table_row.id).order_by(MetadataColumn.ordinal).limit(80)))
        for column in columns:
            merged.append({
                "node_id": f"legacy:{table}.{column.column_name}",
                "source_type": "legacy_metadata",
                "source_id": f"{table}.{column.column_name}",
                "facet": "column_semantic",
                "table": table,
                "column": column.column_name.upper(),
                "score": _legacy_column_score(column, requirement),
                "weight": 1.0,
                "evidence": f"{table}.{column.column_name}: {column.comment} {column.data_type}",
                "reasons": ["legacy_column"],
                "meta": {"table": table, "column": column.column_name, "comment": column.comment, "data_type": column.data_type},
            })
    return sorted(merged, key=lambda row: -float(row.get("score") or 0))[:96]


def _legacy_column_score(column: MetadataColumn, requirement: str) -> float:
    name = column.column_name.upper()
    comment = column.comment or ""
    score = 0.55
    if any(word in requirement for word in ("姓", "姓名", "氏名", "名字")) and ("氏名" in comment or "NAME" in name):
        score += 0.35
    if any(word in requirement for word in ("员工", "職員", "社員")) and ("職員番号" in comment or "社員番号" in comment or "SHAIN" in name):
        score += 0.3
    if any(word in requirement for word in ("异动", "異動", "任免", "発令")) and ("異動" in comment or "任免" in comment or "IDO" in name):
        score += 0.25
    if "年月日" in comment or name.endswith("_DTE"):
        score += 0.08
    return round(min(score, 1.0), 4)


def _workflow_backend() -> str:
    try:
        import llama_index.core  # noqa: F401
        return "llamaindex"
    except Exception:
        return "internal_compatible"


def _entities(text: str) -> list[str]:
    entities = []
    for pattern in (r"[“\"']([^”\"']+)[”\"']", r"姓([\u3040-\u30ff\u3400-\u9fff]{1,8})", r"氏名([\u3040-\u30ff\u3400-\u9fff]{1,8})"):
        entities.extend(match.group(1) for match in re.finditer(pattern, text or ""))
    return list(dict.fromkeys(entities))


def _filters(text: str) -> list[dict]:
    filters = []
    for entity in _entities(text):
        if any(word in text for word in ("姓", "姓名", "名字", "氏名")):
            filters.append({"kind": "name", "operator": "LIKE", "value": entity})
    if any(word in text for word in ("所有", "全部", "全件", "すべて", "all")):
        filters.append({"kind": "all_records", "operator": "none", "value": None})
    for date in re.findall(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{4}年\d{1,2}月?", text or ""):
        filters.append({"kind": "date", "operator": "=", "value": date})
    return filters


def _output_fields(text: str) -> list[str]:
    fields = []
    if any(word in text for word in ("基本信息", "基本情報", "基本資料")):
        fields.extend(["employee_no", "name", "birth", "gender", "organization"])
    if any(word in text for word in ("入职", "入社", "任用", "採用")):
        fields.extend(["employee_no", "name", "hire_date"])
    if _mentions_transfer(text):
        fields.extend(["employee_no", "issue_date", "transfer_code", "transfer_name", "name"])
    return list(dict.fromkeys(fields))


def _sorts(text: str) -> list[dict]:
    if any(word in text for word in ("排序", "順", "order")):
        return [{"kind": "requested", "direction": "ASC"}]
    return []


def _aggregations(text: str) -> list[dict]:
    if any(word in text for word in ("数量", "件数", "count", "统计")):
        return [{"function": "COUNT"}]
    return []


def _requires_join(text: str) -> bool:
    return _mentions_employee_basic(text) and _mentions_transfer(text)


def _mentions_employee_basic(text: str) -> bool:
    return any(word in text for word in ("基本信息", "基本情報", "基本資料"))


def _mentions_transfer(text: str) -> bool:
    return any(word in text for word in ("异动", "異動", "任免", "発令"))


def _top_tables(items: list[dict], limit: int) -> list[dict]:
    by_table: dict[str, dict] = {}
    for item in items:
        table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
        if not table:
            continue
        score = float(item.get("score") or 0)
        existing = by_table.get(table)
        if not existing:
            by_table[table] = {"table": table, "score": score, "facets": [item.get("facet")], "reasons": item.get("reasons", []), "role": (item.get("meta") or {}).get("role"), "domain": (item.get("meta") or {}).get("domain")}
        else:
            existing["score"] += score
            if item.get("facet") not in existing["facets"]:
                existing["facets"].append(item.get("facet"))
    return sorted(by_table.values(), key=lambda row: (-float(row["score"]), row["table"]))[:limit]


def _top_fields(items: list[dict], tables: list[dict], limit: int) -> list[dict]:
    allowed = {row["table"] for row in tables}
    fields = []
    seen = set()
    for item in sorted(items, key=lambda row: -float(row.get("score") or 0)):
        table = str(item.get("table") or (item.get("meta") or {}).get("table") or "").upper()
        column = str(item.get("column") or (item.get("meta") or {}).get("column") or "").upper()
        if not table or not column or table not in allowed or (table, column) in seen:
            continue
        seen.add((table, column))
        fields.append({"table": table, "column": column, "score": item.get("score"), "comment": (item.get("meta") or {}).get("comment"), "facet": item.get("facet")})
        if len(fields) >= limit:
            break
    return fields


def _compact(item: dict) -> dict:
    return {
        "source_id": item.get("source_id"),
        "facet": item.get("facet"),
        "table": item.get("table") or (item.get("meta") or {}).get("table"),
        "column": item.get("column") or (item.get("meta") or {}).get("column"),
        "score": item.get("score"),
        "reasons": item.get("reasons"),
        "evidence": str(item.get("evidence") or "")[:600],
        "meta": item.get("meta") or {},
    }
