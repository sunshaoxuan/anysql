"""
AnySQL API - SQL 辅助对话。
"""

from __future__ import annotations

from datetime import datetime
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from anysql.core.sql_cleaner import clean_generated_sql
from anysql.models.schemas import AnalysisStatus, GeneratedSQL, SQLAssistantRequest, SQLAssistantResponse, SQLLearnRequest
from anysql.storage.pgvector_engine import PGVectorRepository
from anysql.storage.repositories import ProductRepository, SQLKnowledgeRepository

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.post("/sql", response_model=SQLAssistantResponse)
async def assist_sql(req: SQLAssistantRequest):
    """
    SQL 专用对话入口。

    先用向量检索找候选，再由 LLM 判断匹配度；高匹配返回已有 SQL。
    低匹配生成草稿，带当前 SQL 时按修正意见改写草稿。草稿必须人工确认后才入库。
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
            current_sql = req.current_sql
            if not current_sql and req.current_sql_id:
                record = SQLKnowledgeRepository(session).get(req.current_sql_id)
                current_sql = record.statement.raw_sql if record else None
        try:
            pipeline = state["pipeline"]
            if current_sql:
                query_ja = await pipeline._normalize_query_to_japanese(req.message)
                with storage.database.session() as search_session:
                    product = ProductRepository(search_session).get_by_code(req.product)
                    vector = PGVectorRepository(search_session, state["llm"], config.llm.embed_model)
                    metadata_hits = await vector.search_metadata(query_ja, product.id, 10)
                    metadata_hits = _merge_metadata_hits(metadata_hits, vector.search_metadata_text(f"{req.message} {query_ja}", product.id, 10))
                generated = await _generate_sql_from_context(
                    state,
                    config,
                    req.product,
                    req.message,
                    query_ja,
                    metadata_hits,
                    [],
                    current_sql=current_sql,
                )
                generated.sql = clean_generated_sql(generated.sql)
                record = pipeline._draft_record(req.product, req.message, generated)
                mode, matches, learned = "revised", [], False
            else:
                query_ja = await pipeline._normalize_query_to_japanese(req.message)
                with storage.database.session() as search_session:
                    product = ProductRepository(search_session).get_by_code(req.product)
                    vector = PGVectorRepository(search_session, state["llm"], config.llm.embed_model)
                    candidates = await vector.search(query_ja, req.product, req.top_k)
                    metadata_hits = await vector.search_metadata(query_ja, product.id, 8)
                    metadata_hits = _merge_metadata_hits(metadata_hits, vector.search_metadata_text(f"{req.message} {query_ja}", product.id, 10))
                matches = await pipeline._llm_match_candidates(req.product, f"{req.message}\n日文检索意图: {query_ja}", candidates)
                best = matches[0] if matches else None
                if best and best.llm_score >= req.match_threshold:
                    with storage.database.session() as record_session:
                        record = SQLKnowledgeRepository(record_session).get(best.sql_id)
                        if record:
                            record.statement.product = req.product
                    mode, learned = "matched", False
                else:
                    relevant_candidates = [
                        item for item in candidates
                        if any(m.sql_id == item.sql_id and m.llm_score >= 0.55 for m in matches)
                    ]
                    generated = await _generate_sql_from_context(
                        state,
                        config,
                        req.product,
                        req.message,
                        query_ja,
                        metadata_hits,
                        relevant_candidates,
                    )
                    generated.sql = clean_generated_sql(generated.sql)
                    record = pipeline._draft_record(req.product, req.message, generated)
                    mode, learned = "generated", False
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                if mode != "matched":
                    SQLKnowledgeRepository(session).save_draft(product.id, req.message, record, req.session_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        text = "找到可直接复用的 SQL。" if mode == "matched" else "已根据修正意见改写 SQL。请确认正确后再采纳入库。" if mode == "revised" else "没有足够高匹配的 SQL，已根据 Metadata 和既有知识生成草稿。请确认正确后再采纳入库。"
        return SQLAssistantResponse(product=req.product, session_id=req.session_id, mode=mode, message=text, matches=matches, record=record, learned=learned)
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    current_sql = req.current_sql
    if not current_sql and req.current_sql_id:
        from anysql.harness.tasks import load_all_records

        pcfg = config.products[req.product]
        for record in load_all_records(pcfg.desc_dir):
            if record.statement.id == req.current_sql_id:
                current_sql = record.statement.raw_sql
                break

    try:
        mode, record, matches, learned = await state["pipeline"].assist_sql(
            product_id=req.product,
            message=req.message,
            current_sql=current_sql,
            top_k=req.top_k,
            match_threshold=req.match_threshold,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    if mode == "matched":
        text = "找到可直接复用的 SQL。"
    elif mode == "revised":
        text = "已根据修正意见改写 SQL。请确认正确后再采纳入库。"
    else:
        text = "没有足够高匹配的 SQL，已根据 Metadata 和既有知识生成草稿。请确认正确后再采纳入库。"

    return SQLAssistantResponse(
        product=req.product,
        session_id=req.session_id,
        mode=mode,
        message=text,
        matches=matches,
        record=record,
        learned=learned,
    )


@router.post("/learn")
async def learn_sql(req: SQLLearnRequest):
    """人工确认 SQL 正确后，纳入知识库。"""
    state = _get_app_state()
    config = state["config"]
    storage = state.get("storage")
    if storage and storage.is_database_mode:
        with storage.database.session() as session:
            product = ProductRepository(session).get_by_code(req.product)
            if not product:
                raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")
            config.products[req.product] = ProductRepository(session).to_product_config(product)
        generated = GeneratedSQL(
            sql=req.sql,
            summary=req.summary,
            business_meaning=req.business_meaning,
            usage_guide=req.usage_guide,
            parameters=req.parameters,
            tables=req.tables,
        )
        try:
            record = state["pipeline"]._draft_record(req.product, req.requirement, generated)
            record.statement.id = f"{req.product}_accepted_{uuid4().hex[:12]}"
            record.statement.source_file = "accepted"
            record.status = AnalysisStatus.SUCCESS
            record.analyzed_at = datetime.now()
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                SQLKnowledgeRepository(session).accept_generated(product.id, req.requirement, record)
                await PGVectorRepository(session, state["llm"], config.llm.embed_model).index_records([record], product.id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {"status": "learned", "learned": True, "accepted": True, "source": "metadata_generated", "record": record.model_dump(mode="json")}
    if req.product not in config.products:
        raise HTTPException(status_code=404, detail=f"产品不存在: {req.product}")

    generated = GeneratedSQL(
        sql=req.sql,
        summary=req.summary,
        business_meaning=req.business_meaning,
        usage_guide=req.usage_guide,
        parameters=req.parameters,
        tables=req.tables,
    )
    try:
        record = await state["pipeline"].learn_confirmed_sql(
            product_id=req.product,
            requirement=req.requirement,
            generated=generated,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"status": "learned", "record": record.model_dump(mode="json")}


def _merge_metadata_hits(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for item in [*secondary, *primary]:
        table = str(item.get("table") or item.get("id") or "").upper()
        if table and table not in seen:
            seen.add(table)
            merged.append(item)
    return sorted(merged, key=_metadata_priority)[:12]


def _metadata_priority(item: dict) -> tuple[int, str]:
    table = str(item.get("table") or item.get("id") or "").upper()
    document = str(item.get("document") or "")
    if table == "XKKIHON":
        return (0, table)
    if table == "BTKIHON":
        return (1, table)
    if "給与基本情報" in document or "[基本]" in document:
        return (2, table)
    if table.startswith(("MAST", "MAESTRO")):
        return (8, table)
    return (5, table)


async def _generate_sql_from_context(
    state,
    config,
    product_code: str,
    requirement: str,
    query_ja: str,
    metadata_hits: list[dict],
    candidates,
    current_sql: str | None = None,
) -> GeneratedSQL:
    examples = [
        f"- {item.summary}\n  SQL: {item.raw_sql}\n  Context: {' / '.join(item.business_context)}"
        for item in candidates
    ]
    product_rules = config.products[product_code].rules.strip() or "No product-specific rules."
    metadata_context = "\n\n".join(
        f"- {m['table']} score={m['score']}\n{m['document']}"
        for m in metadata_hits
    ) or "No metadata hits."
    allowed_tables = ", ".join(dict.fromkeys(str(m.get("table", "")).upper() for m in metadata_hits if m.get("table"))) or "No allowed tables."
    domain_hint = _domain_hint(requirement, metadata_hits)
    deterministic = _deterministic_basic_employee_sql(product_code, requirement, metadata_hits)
    if deterministic:
        return deterministic
    revise_block = f"\n当前 SQL（只能作为上一轮错误草稿参考，不得盲目保留错误表名）:\n{current_sql}\n" if current_sql else ""
    prompt = f"""你要为产品 {product_code} 生成或修正一段满足业务需求的 Oracle SQL。

业务需求:
{requirement}
{revise_block}
日文检索意图（元数据语言）:
{query_ja}

产品规则（必须遵守）:
{product_rules}

可用 Metadata 候选（只允许使用这些候选里的表和字段；如果无法确定，返回最保守的候选并在 assumptions 说明）:
{metadata_context}

允许使用的表名:
{allowed_tables}

本次需求的表选择约束:
{domain_hint}

相似SQL知识:
{chr(10).join(examples) if examples else "No similar SQL knowledge found."}

硬性要求:
1. 不得编造 Metadata 中不存在的表名或字段名。
2. 中文“员工/职员/社員”通常对应 職員番号/社員番号；“姓名/姓/名字”通常优先匹配 氏名/漢字氏名/CNAMEKNJ。
3. 对 UPDS 的“员工基本情况/基本情報/給与基本情報”优先使用 XKKIHON；联携/取込场景才使用 BTKIHON；不要在没有明确要求 master/マスタ 时优先使用 MAST_*/MAESTRO_*。
4. 如果按姓名查询，优先使用 Metadata 中带 漢字氏名/氏名 注释的字段，例如 CNAMEKNJ。
5. SQL 必须是可直接执行的 Oracle SQL，不能出现 HTML 实体、反斜杠转义、Markdown、JSON 字符串转义。
6. SQL 字符串字面量必须直接使用单引号，例如 LIKE '松下%'。
7. 在 SQL 开头生成 1-2 行 -- 注释，说明用途和参数。
8. 输出 JSON，字段必须符合 GeneratedSQL schema。
"""
    return await state["agent"].execute_task(prompt, GeneratedSQL)


def _domain_hint(requirement: str, metadata_hits: list[dict]) -> str:
    text = requirement or ""
    tables = {str(m.get("table") or "").upper() for m in metadata_hits}
    asks_basic_employee = any(word in text for word in ("基本情况", "基本信息", "基本資料", "基本情報")) and any(
        word in text for word in ("员工", "職員", "社員", "姓", "姓名", "氏名")
    )
    if asks_basic_employee and "XKKIHON" in tables:
        return "本次是 UPDS 员工/职员基本信息查询，必须优先使用 XKKIHON；姓名字段使用 CNAMEKNJ；员工/职员编号字段使用 CSHAINNO。"
    return "按 Metadata 候选顺序优先选择最贴近业务含义的表；不要被低匹配 SQL 示例带偏。"


def _deterministic_basic_employee_sql(product_code: str, requirement: str, metadata_hits: list[dict]) -> GeneratedSQL | None:
    tables = {str(m.get("table") or "").upper() for m in metadata_hits}
    hint = _domain_hint(requirement, metadata_hits)
    product_is_upds = product_code.lower().startswith("upds")
    asks_basic_employee = any(word in (requirement or "") for word in ("基本情况", "基本信息", "基本資料", "基本情報")) and any(
        word in (requirement or "") for word in ("员工", "職員", "社員", "姓", "姓名", "氏名")
    )
    if not ((("XKKIHON" in tables and hint.startswith("本次是 UPDS")) or product_is_upds) and asks_basic_employee):
        return None
    surname = _extract_quoted_value(requirement) or _extract_after_surname_word(requirement)
    condition = "CNAMEKNJ LIKE :surname || '%'"
    params = [":surname 姓（例: 松下）"]
    if surname:
        condition = f"CNAMEKNJ LIKE '{surname}%'"
        params = [f"'{surname}%' 姓の前方一致条件"]
    sql = f"""SELECT
    CSHAINNO,
    CNAMEKNJ,
    CNAMEKNA,
    KYU_KJ_NME,
    KYU_KN_NME
FROM XKKIHON
WHERE {condition}
ORDER BY CSHAINNO;"""
    return GeneratedSQL(
        sql=sql,
        summary="姓を条件に職員の基本情報を取得する",
        business_meaning="UPDS の給与基本情報から、指定姓に該当する職員の番号・漢字氏名・カナ氏名・旧姓情報を確認する",
        usage_guide="姓を固定値で指定するか、:surname パラメータに置き換えて使用します。",
        parameters=params,
        tables=["XKKIHON"],
        assumptions=["UPDS の職員基本情報は XKKIHON（[基本]給与基本情報）を優先します。"],
    )


def _extract_quoted_value(text: str) -> str | None:
    match = re.search(r"[\"“”'「『](.+?)[\"“”'」』]", text or "")
    return match.group(1).strip() if match else None


def _extract_after_surname_word(text: str) -> str | None:
    match = re.search(r"姓\s*([\u3400-\u9fffぁ-んァ-ヶー]{1,8})", text or "")
    if not match:
        return None
    value = match.group(1).strip()
    value = re.split(r"(员工|職員|社員|的|の|基本|情况|情報|資料)", value, maxsplit=1)[0]
    return value[:4] if value else None
