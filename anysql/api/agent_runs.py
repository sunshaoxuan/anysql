"""Agent run audit APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from anysql.storage.rag_repository import AgentRunRepository

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])


def _get_app_state():
    from anysql.main import app_state
    return app_state


@router.get("/{run_id}")
async def get_agent_run(run_id: str):
    state = _get_app_state()
    storage = state.get("storage")
    if not (storage and storage.is_database_mode):
        raise HTTPException(status_code=400, detail="agent runs require database mode")
    with storage.database.session() as session:
        run = AgentRunRepository(session).get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"agent run not found: {run_id}")
        return run
