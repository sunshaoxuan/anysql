"""
AnySQL API - SQL 生成草稿。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from anysql.core.sql_cleaner import clean_generated_sql
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
            query_ja = await pipeline._normalize_query_to_japanese(req.requirement)
            with storage.database.session() as session:
                product = ProductRepository(session).get_by_code(req.product)
                vector = PGVectorRepository(session, state["llm"], config.llm.embed_model)
                candidates = await vector.search(query_ja, req.product, req.top_k)
                metadata_hits = await vector.search_metadata(query_ja, product.id, 8)
                metadata_hits = _merge_metadata_hits(metadata_hits, vector.search_metadata_text(f"{req.requirement} {query_ja}", product.id, 10))
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
            domain_hint = _domain_hint(req.requirement, metadata_hits)
            deterministic = _deterministic_basic_employee_sql(req.product, req.requirement, metadata_hits)
            if deterministic:
                generated = deterministic
                record = pipeline._draft_record(req.product, req.requirement, generated)
            else:
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

相似SQL知识:
{chr(10).join(examples) if examples else "No similar SQL knowledge found."}

要求:
1. 不得编造 Metadata 中不存在的表名或字段名。
2. 中文“员工/职员/社員”通常对应 職員番号/社員番号；“姓名/姓/名字”通常优先匹配 氏名/漢字氏名/CNAMEKNJ。
3. 对 UPDS 的“员工基本情况/基本情報/給与基本情報”优先使用 XKKIHON；联携/取込场景才使用 BTKIHON；不要在没有明确要求 master/マスタ 时优先使用 MAST_*/MAESTRO_*。
4. 如果按姓名查询，优先使用 Metadata 中带 漢字氏名/氏名 注释的字段，例如 CNAMEKNJ。
5. SQL 必须是可直接执行的 Oracle SQL，不能出现 HTML 实体、反斜杠转义、Markdown、JSON 字符串转义。
6. SQL 字符串字面量必须直接使用单引号，例如 LIKE '松下%'。
7. 在 SQL 开头生成 1-2 行 -- 注释，说明用途和参数。
8. 只返回 JSON，不要返回 Markdown。
"""
                generated = await state["agent"].execute_task(prompt, GeneratedSQL)
                generated.sql = clean_generated_sql(generated.sql)
                record = pipeline._draft_record(req.product, req.requirement, generated)
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
