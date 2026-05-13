"""
AnySQL API - SQL 生成草稿。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.core.query_expansion import expand_query_for_metadata
from anysql.core.sql_cleaner import clean_generated_sql
from anysql.core.table_profiles import resolve_intent
from anysql.models.schemas import (
    GeneratedSQL,
    SQLGenerationRequest,
    SQLGenerationResponse,
)
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository

router = APIRouter(prefix="/api/generate", tags=["generation"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.post("/sql", response_model=SQLGenerationResponse)
async def generate_sql(req: SQLGenerationRequest):
    """
    根据产品 Metadata 和既有 SQL 知识生成 SQL 草稿。
    草稿需要通过 /api/assistant/learn 人工确认后才会入库。
    """
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")
            config.products[req.product] = ProductRepository(session).to_product_config(product)
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    pipeline = state["pipeline"]
    try:
        if storage and storage.is_database_mode:
            query_ja = expand_query_for_metadata(req.requirement)
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                vector = PGVectorRepository(session, state["llm"], config.llm.embed_model)
                candidates = await vector.search(query_ja, req.product, req.top_k)
                vector_hits = await vector.search_metadata(query_ja, product.id, 8)
                text_hits = vector.search_metadata_text(f"{req.requirement} {query_ja}", product.id, 10)
                intent_hits = vector.search_metadata_by_intent(req.requirement, product.id, 8)
                metadata_hits = _merge_metadata_hits(
                    intent_hits,
                    vector.apply_profile_policy(text_hits, req.requirement, product.id),
                    vector.apply_profile_policy(vector_hits, req.requirement, product.id),
                )
                metadata_hits = _restrict_intent_tables(metadata_hits, req.requirement)
                blocked_profiles = vector.blocked_profiles([*vector_hits, *text_hits], req.requirement, product.id)
                intent_name = resolve_intent(req.requirement)
            examples = [
                f"- {item.summary}\n  SQL: {item.raw_sql}\n  Context: {' / '.join(item.business_context)}"
                for item in candidates
                if item.score >= 0.62
            ]
            product_rules = config.products[req.product].rules.strip() or "No product-specific rules."
            metadata_context = "\n\n".join(
                f"- {m['table']} score={m['score']}\n{m['document']}"
                for m in metadata_hits
            ) or "No metadata vector hits."
            allowed_tables = ", ".join(dict.fromkeys(str(m.get("table", "")).upper() for m in metadata_hits if m.get("table"))) or "No allowed tables."
            domain_hint = _domain_hint(req.requirement)
            prompt = f"""你要为产品 {req.product} 生成一段满足业务需求的 SQL。
业务需求:
{req.requirement}

日文检索意图（元数据语言）:
{query_ja}

产品规则（必须遵守）:
{product_rules}

Metadata RAG 候选（只允许使用这些候选里的表和字段）:
{metadata_context}

允许使用的表名:
{allowed_tables}

本次需求的表选择约束:
{domain_hint}

表角色约束:
{_profile_rule(req.requirement)}

相似SQL知识:
{chr(10).join(examples) if examples else "No similar SQL knowledge found."}

要求:
1. 不得编造 Metadata 中不存在的表名或字段名。
2. 中文“员工/职员/社員”通常对应 職員番号/社員番号；“姓名/姓/名字”通常优先匹配 氏名/漢字氏名/CNAMEKNJ。
3. 不能做字符串严格匹配式决策。表和字段必须来自候选，但要根据向量分数、模糊文本分数、字段注释、表注释和用户意图综合判断。
4. 如果按姓名查询，优先评估 Metadata 中带 氏名/漢字氏名/カナ氏名 注释的字段；不要只因为字段名或表名完全匹配才选中。
5. 必须保留用户的过滤语义：如果用户说“姓/姓名/氏名/名字”，WHERE 条件必须作用在姓名类字段上，使用 LIKE 或可参数化的前方/部分一致；不得改写成员工编号、职员编号或其他代码字段。
6. SQL 必须是可直接执行的 Oracle SQL，不能出现 HTML 实体、反斜杠转义、Markdown、JSON 字符串转义。
7. SQL 字符串字面量必须直接使用单引号，并保留用户输入的实际值；不要照抄示例值。
8. SQL 注释只能使用日语；禁止中文注释。SELECT 列别名不要生成。
9. 在 SQL 开头生成 1-2 行 -- 注释，说明用途和参数。
10. 绑定参数名只能使用 ASCII 字母、数字和下划线，例如 :employee_no，禁止中文/日文参数名。
11. 只返回 JSON，不要返回 Markdown。
"""
            generated = await state["agent"].execute_task(prompt, GeneratedSQL)
            generated.sql = clean_generated_sql(generated.sql, allow_aliases=False)
            record = pipeline._draft_record(req.product, req.requirement, generated, allow_aliases=False)
            _attach_profile_snapshot(record, intent_name, metadata_hits, blocked_profiles)
            _apply_intent_sql_template(record, req.requirement)
        else:
            record = await pipeline.generate_and_learn(
                product_id=req.product,
                requirement=req.requirement,
                top_k=req.top_k,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    generated = GeneratedSQL(
        sql=record.statement.raw_sql,
        summary=record.analysis.summary if record.analysis else "",
        business_meaning=(
            " / ".join(record.analysis.business_context)
            if record.analysis else ""
        ),
        usage_guide=record.analysis.usage_guide if record.analysis else "",
        parameters=record.analysis.parameters if record.analysis else [],
        tables=record.statement.tables,
    )
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            SQLKnowledgeRepository(session).save_draft(product.id, req.requirement, record)
    return SQLGenerationResponse(
        product=req.product,
        requirement=req.requirement,
        generated=generated,
        record=record,
        learned=False,
    )


def _merge_metadata_hits(primary: list[dict], secondary: list[dict], intent: list[dict] | None = None) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for item in [*primary, *secondary, *(intent or [])]:
        table = str(item.get("table") or item.get("id") or "").upper()
        if table and table not in seen:
            seen.add(table)
            merged.append(item)
    return sorted(merged, key=lambda item: float(item.get("score") or 0), reverse=True)[:12]


def _restrict_intent_tables(metadata_hits: list[dict], requirement: str) -> list[dict]:
    intent = resolve_intent(requirement)
    text = requirement or ""
    if intent == "transfer_records":
        preferred = {"DKIDO_R", "DKIDO"}
        if any(word in text for word in ("非常勤", "非職", "非职", "パート", "part-time", "parttime")):
            preferred.update({"DHJKIDO_R", "DHJKIDO"})
        restricted = [item for item in metadata_hits if str(item.get("table", "")).upper() in preferred]
        if restricted:
            return restricted[:4]
    if intent == "employee_basic_name":
        preferred = {"DJND0110", "MAST_EMPLOYEES", "MAST_EMPLOYEES2"}
        restricted = [item for item in metadata_hits if str(item.get("table", "")).upper() in preferred]
        if restricted:
            return restricted[:3]
    if intent == "transfer_check_log":
        restricted = [item for item in metadata_hits if str(item.get("table", "")).upper() == "XCIDOCHKLOG"]
        if restricted:
            return restricted[:1]
    return metadata_hits


def _apply_intent_sql_template(record, requirement: str) -> None:
    intent = resolve_intent(requirement)
    text = requirement or ""
    if intent != "transfer_records":
        return
    if not any(word in text.lower() for word in ("所有", "全部", "全件", "すべて", "all")):
        return
    table = "DKIDO"
    if any(word in text for word in ("履歴", "历史", "歷史", "累積")) or not any(word in text for word in ("当前", "現在", "未累積")):
        table = "DKIDO_R"
    if any(word in text for word in ("非常勤", "非職", "非职", "パート", "part-time", "parttime")):
        table = "DHJKIDO_R" if table.endswith("_R") else "DHJKIDO"
    record.statement.raw_sql = (
        "-- AnySQL: 異動履歴を取得します。\n"
        "-- 条件: 必要に応じて職員番号や氏名のWHERE条件を追加してください。\n"
        f"SELECT *\nFROM {table};"
    )
    record.statement.tables = [table]


def _attach_profile_snapshot(record, intent_name: str, metadata_hits: list[dict], blocked_profiles: list[dict]) -> None:
    selected_profiles = [item.get("profile") for item in metadata_hits if item.get("profile")]
    selected = {str(profile.get("table_name", "")).upper(): profile for profile in selected_profiles}
    blocked = {str(profile.get("table_name", "")).upper(): profile for profile in blocked_profiles}
    warnings = []
    if intent_name != "unknown":
        for table in {table.upper() for table in record.statement.tables}:
            if table in blocked:
                warnings.append(f"{table} role={blocked[table].get('role')} is blocked for intent={intent_name}")
            elif table not in selected:
                warnings.append(f"{table} was not in the allowed table candidates for intent={intent_name}")
    record.metadata_snapshot = {
        **(record.metadata_snapshot or {}),
        "intent": intent_name,
        "selected_table_profiles": selected_profiles,
        "blocked_table_profiles": blocked_profiles,
        "validation_warnings": warnings,
    }


def _domain_hint(requirement: str) -> str:
    text = requirement or ""
    asks_basic_employee = any(word in text for word in ("基本情况", "基本信息", "基本資料", "基本情報")) and any(
        word in text for word in ("员工", "職員", "社員", "姓", "姓名", "氏名")
    )
    if asks_basic_employee:
        return "本次看起来是员工/职员基本信息查询。请让 LLM 在候选表中综合判断最贴近“基本情報/給与基本情報/氏名/職員番号”的表和字段，禁止硬编码固定表。"
    if resolve_intent(text) == "part_time_employee_hire_date":
        return "本次是非常勤職員的入职/任用日期查询。优先使用 DJND3001，日期字段优先 NINYO_DTE/DNINYO_DTE，姓名字段优先 CNAMEKNJ/CNAMEKNA，禁止 DKIDO/DHJKIDO/log/work/IF。"
    if resolve_intent(text) == "transfer_records":
        return "本次是异动记录查询。普通异动记录优先使用 DKIDO_R / DKIDO；只有用户明确要求非常勤/非职时才使用 DHJKIDO_R / DHJKIDO。"
    if resolve_intent(text) == "transfer_check_log":
        return "本次是异动检查日志/错误消息查询，可以使用 XCIDOCHKLOG。"
    return "按向量候选、模糊文本候选和业务意图综合选择最贴近的表字段；不要被低匹配 SQL 示例带偏。"


def _profile_rule(requirement: str) -> str:
    intent = resolve_intent(requirement)
    if intent == "transfer_records":
        return "业务查询不得使用 log/work/if_staging/backup 表。XCIDOCHKLOG 只能用于异动检查日志/错误消息，不得用于普通异动记录。"
    if intent == "transfer_check_log":
        return "本意图允许 log 表；优先使用 XCIDOCHKLOG。"
    if intent == "employee_basic_name":
        return "基本信息查询只允许 master 表。不得使用給与/ワーク/IF/log/backup 表。"
    if intent == "part_time_employee_hire_date":
        return "非常勤職員入职/任用日期查询只允许 employee/master 表，优先 DJND3001；不得使用異動、log、work、IF、backup 表。"
    return "未知意图下避免使用 log/work/if_staging/backup 表，除非用户明确要求日志、检查、接口或临时数据。"
