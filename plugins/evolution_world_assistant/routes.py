"""HTTP API for the PlotPilot Evolution World Assistant plugin."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .service import EvolutionWorldAssistantService

router = APIRouter(prefix="/api/v1/plugins/evolution-world", tags=["plugins:evolution-world"])
_service = EvolutionWorldAssistantService()


@router.get("/status")
async def get_status():
    return {
        "plugin_name": "evolution_world_assistant",
        "version": "0.1.0",
        "status": "installed",
        "phase": "plotpilot-adapter-skeleton",
    }


@router.get("/novels/{novel_id}/characters")
async def list_characters(novel_id: str):
    state = _service.storage.read_json(
        "evolution_world_assistant",
        ["novels", novel_id, "characters.json"],
        default={"items": []},
    )
    return state


@router.post("/novels/{novel_id}/chapters/{chapter_number}/rerun")
async def rerun_chapter(novel_id: str, chapter_number: int, payload: dict | None = None):
    body = payload or {}
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required for skeleton rerun")
    return await _service.after_commit(
        {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "trigger_type": "manual",
            "payload": {"content": content},
        }
    )


@router.post("/novels/{novel_id}/rebuild")
async def rebuild_novel(novel_id: str):
    return await _service.manual_rebuild({"novel_id": novel_id, "trigger_type": "manual"})
