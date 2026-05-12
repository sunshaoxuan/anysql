"""
AnySQL API - SQL 辅助对话。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from anysql.core.query_expansion import expand_query_for_metadata
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
                query_ja = expand_query_for_metadata(req.message)
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
                query_ja = expand_query_for_metadata(req.message)
                with storage.database.session() as search_session:
                    product = ProductRepository(search_session).get_by_code(req.product)
                    vector = PGVectorRepository(search_session, state["llm"], config.llm.embed_model)
                    candidates = await vector.search(query_ja, req.product, req.top_k)
                    metadata_hits = await vector.search_metadata(query_ja, product.id, 8)
                    metadata_hits = _merge_metadata_hits(metadata_hits, vector.search_metadata_text(f"{req.message} {query_ja}", product.id, 10))
                top_vector_score = max((item.score for item in candidates), default=0.0)
                matches = []
                if top_vector_score >= max(0.68, req.match_threshold - 0.08):
                    matches = await pipeline._llm_match_candidates(req.product, f"{req.message}\n检索扩展: {query_ja}", candidates)
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
    return sorted(merged, key=lambda item: float(item.get("score") or 0), reverse=True)[:12]


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
    domain_hint = _domain_hint(requirement)
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
3. 不能做字符串严格匹配式决策。表和字段必须来自候选，但要根据向量分数、模糊文本分数、字段注释、表注释和用户意图综合判断。
4. 如果按姓名查询，优先评估 Metadata 中带 氏名/漢字氏名/カナ氏名 注释的字段；不要只因为字段名或表名完全匹配才选中。
5. 必须保留用户的过滤语义：如果用户说“姓/姓名/氏名/名字”，WHERE 条件必须作用在姓名类字段上，使用 LIKE 或可参数化的前方/部分一致；不得改写成员工编号、职员编号或其他代码字段。
6. SQL 必须是可直接执行的 Oracle SQL，不能出现 HTML 实体、反斜杠转义、Markdown、JSON 字符串转义。
7. SQL 字符串字面量必须直接使用单引号，并保留用户输入的实际值；不要照抄示例值。
8. SQL 注释只能使用日语；禁止中文注释。SELECT 列别名原则上不要生成，必要时只能使用 ASCII 别名，禁止中文/日文别名。
9. 在 SQL 开头生成 1-2 行 -- 注释，说明用途和参数。
10. 输出 JSON，字段必须符合 GeneratedSQL schema。
"""
    return await state["agent"].execute_task(prompt, GeneratedSQL)


def _domain_hint(requirement: str) -> str:
    text = requirement or ""
    asks_basic_employee = any(word in text for word in ("基本情况", "基本信息", "基本資料", "基本情報")) and any(
        word in text for word in ("员工", "職員", "社員", "姓", "姓名", "氏名")
    )
    if asks_basic_employee:
        return "本次看起来是员工/职员基本信息查询。请让 LLM 在候选表中综合判断最贴近“基本情報/給与基本情報/氏名/職員番号”的表和字段，禁止硬编码固定表。"
    return "按向量候选、模糊文本候选和业务意图综合选择最贴近的表字段；不要被低匹配 SQL 示例带偏。"
