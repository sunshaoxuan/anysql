"""Knowledge gap review APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.storage.rag_repository import KnowledgeGapRepository, RagRepository
from anysql.storage.repositories import ProductRepository

router = APIRouter(prefix="/api/knowledge-gaps", tags=["knowledge-gaps"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.get("")
async def list_knowledge_gaps(product: str = "", status: str = "", limit: int = 100):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="knowledge gaps require database mode")
    with storage.database.session() as session:
        product_id = None
        if product:
            product_row = ProductRepository(session).get_by_code(product)
            if not product_row:
                raise HTTPException(status_code=404, detail=f"产品不存在: {product}")
            product_id = product_row.id
        return KnowledgeGapRepository(session).list(product_id=product_id, status=status or None, limit=limit)


@router.get("/{gap_id}")
async def get_knowledge_gap(gap_id: str):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="knowledge gaps require database mode")
    with storage.database.session() as session:
        repo = KnowledgeGapRepository(session)
        gap = repo.get(gap_id)
        if not gap:
            raise HTTPException(status_code=404, detail=f"knowledge gap not found: {gap_id}")
        return repo.to_dict(gap)


@router.post("/{gap_id}/approve")
async def approve_knowledge_gap(gap_id: str, payload: dict | None = None):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="knowledge gaps require database mode")
    reviewed_by = str((payload or {}).get("reviewed_by") or "system")
    with storage.database.session() as session:
        repo = KnowledgeGapRepository(session)
        gap = repo.approve(gap_id, reviewed_by=reviewed_by)
        candidates = repo.candidates(gap_id, status="approved")
        rag = RagRepository(session)
        node_count = 0
        for candidate in candidates:
            node_count += rag.upsert_candidate_node(gap.product_id, candidate)
        indexed = await rag.index_nodes(
            gap.product_id,
            state["llm"],
            state["config"].llm.embed_model,
            facets={"business_term", "intent_candidate", "table_field_evidence", "predicate_pattern"},
        )
        return {"status": "approved", "gap": repo.to_dict(gap), "node_count": node_count, "indexed_count": indexed}


@router.post("/{gap_id}/reject")
async def reject_knowledge_gap(gap_id: str, payload: dict | None = None):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="knowledge gaps require database mode")
    reviewed_by = str((payload or {}).get("reviewed_by") or "system")
    with storage.database.session() as session:
        repo = KnowledgeGapRepository(session)
        gap = repo.reject(gap_id, reviewed_by=reviewed_by)
        return {"status": "rejected", "gap": repo.to_dict(gap)}


@router.post("/{gap_id}/rerun")
async def rerun_knowledge_gap(gap_id: str):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="knowledge gaps require database mode")
    with storage.database.session() as session:
        repo = KnowledgeGapRepository(session)
        gap = repo.get(gap_id)
        if not gap:
            raise HTTPException(status_code=404, detail=f"knowledge gap not found: {gap_id}")
        gap.status = "queued"
        session.flush()
    if storage.queue:
        storage.queue.enqueue("anysql.worker_tasks.knowledge_gap_analysis", gap_id)
    return {"status": "queued", "gap_id": gap_id}
