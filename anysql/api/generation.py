"""
AnySQL API - SQL 生成草稿。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.core.query_expansion import expand_query_for_metadata
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
            query_ja = expand_query_for_metadata(req.requirement)
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
8. SQL 注释只能使用日语；禁止中文注释。SELECT 列别名原则上不要生成，必要时只能使用 ASCII 别名，禁止中文/日文别名。
9. 在 SQL 开头生成 1-2 行 -- 注释，说明用途和参数。
10. 只返回 JSON，不要返回 Markdown。
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
    return sorted(merged, key=lambda item: float(item.get("score") or 0), reverse=True)[:12]


def _domain_hint(requirement: str) -> str:
    text = requirement or ""
    asks_basic_employee = any(word in text for word in ("基本情况", "基本信息", "基本資料", "基本情報")) and any(
        word in text for word in ("员工", "職員", "社員", "姓", "姓名", "氏名")
    )
    if asks_basic_employee:
        return "本次看起来是员工/职员基本信息查询。请让 LLM 在候选表中综合判断最贴近“基本情報/給与基本情報/氏名/職員番号”的表和字段，禁止硬编码固定表。"
    return "按向量候选、模糊文本候选和业务意图综合选择最贴近的表字段；不要被低匹配 SQL 示例带偏。"
