"""HTTP API for the PlotPilot Evolution World Assistant plugin."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from .service import EvolutionWorldAssistantService
from .structured_extractor import LLMStructuredExtractorProvider

router = APIRouter(prefix="/api/v1/plugins/evolution-world", tags=["plugins:evolution-world"])
_service = EvolutionWorldAssistantService(extractor_provider=LLMStructuredExtractorProvider())


@router.get("/status")
async def get_status():
    return {
        "plugin_name": "world_evolution_core",
        "version": "0.1.2",
        "status": "installed",
        "phase": "agentic-evolution-phase-1",
        "capabilities": [
            "after_commit",
            "before_context_build",
            "before_chapter_review",
            "after_chapter_review",
            "character_cards",
            "manual_rebuild",
            "structured_extraction",
            "deterministic_fallback",
            "rollback",
            "st_preset_import",
            "chapter_review",
            "timeline_events",
            "continuity_constraints",
            "prehistory_worldline",
            "story_planning_context",
            "story_graph",
            "global_route_map",
            "route_conflict_detection",
            "compact_vector_capsules",
            "agentic_evolution",
            "gep_assets",
            "agent_capsules",
            "agent_api",
            "agent_api_custom_provider",
            "host_context_reader",
            "host_context_injection",
            "host_context_review_evidence",
            "plotpilot_native_context_adapter",
            "multi_collection_semantic_recall",
            "semantic_keyword_fallback",
            "diagnostics",
            "risk_review",
        ],
    }


@router.get("/settings")
async def get_settings():
    return {"ok": True, "settings": _service.get_settings(safe=True)}


@router.put("/settings")
async def update_settings(payload: dict):
    return {"ok": True, "settings": _service.update_settings(payload or {})}


@router.post("/settings/models")
async def fetch_api2_models(payload: dict):
    return _service.deprecated_api2_response()


@router.post("/settings/test")
async def test_api2_connection(payload: dict):
    return _service.deprecated_api2_response()


@router.post("/settings/agent/models")
async def fetch_agent_models(payload: dict):
    try:
        return await _service.fetch_agent_models(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/settings/agent/test")
async def test_agent_connection(payload: dict):
    return await _service.test_agent_connection(payload or {})


@router.get("/novels/{novel_id}/characters")
async def list_characters(novel_id: str):
    return _service.list_characters(novel_id)


@router.get("/novels/{novel_id}/characters/{character_id}")
async def get_character(novel_id: str, character_id: str):
    card = _service.get_character(novel_id, character_id)
    if not card:
        raise HTTPException(status_code=404, detail="character not found")
    return card


@router.get("/novels/{novel_id}/characters/{character_id}/timeline")
async def get_character_timeline(novel_id: str, character_id: str):
    timeline = _service.list_character_timeline(novel_id, character_id)
    if not timeline["items"] and not timeline.get("character"):
        raise HTTPException(status_code=404, detail="character not found")
    return timeline


@router.get("/novels/{novel_id}/imported-flows")
async def list_imported_flows(novel_id: str):
    return _service.list_imported_flows(novel_id)


@router.post("/novels/{novel_id}/import/st-preset")
async def import_st_preset(novel_id: str, payload: dict):
    return _service.import_st_preset(novel_id, payload or {})


@router.get("/novels/{novel_id}/runs")
async def list_runs(novel_id: str, limit: int = 50):
    return _service.list_runs(novel_id, limit=limit)


@router.get("/novels/{novel_id}/snapshots")
async def list_snapshots(novel_id: str):
    return _service.list_snapshots(novel_id)


@router.get("/novels/{novel_id}/events")
async def list_events(novel_id: str):
    return _service.list_events(novel_id)


@router.get("/novels/{novel_id}/timeline/events")
async def list_timeline_events(novel_id: str, before_chapter: Optional[int] = None, limit: int = 50):
    return _service.list_timeline_events(novel_id, before_chapter=before_chapter, limit=limit)


@router.get("/novels/{novel_id}/timeline/constraints")
async def list_continuity_constraints(novel_id: str, limit: int = 80):
    return _service.list_continuity_constraints(novel_id, limit=limit)


@router.get("/novels/{novel_id}/story-graph/chapters")
async def list_story_graph_chapters(novel_id: str, limit: int = 50):
    return _service.list_story_graph_chapters(novel_id, limit=limit)


@router.get("/novels/{novel_id}/routes/global")
async def get_global_route_map(novel_id: str):
    return _service.get_global_route_map(novel_id)


@router.get("/novels/{novel_id}/routes/conflicts")
async def list_route_conflicts(novel_id: str, limit: int = 80):
    return _service.list_route_conflicts(novel_id, limit=limit)


@router.get("/novels/{novel_id}/prehistory/worldline")
async def get_prehistory_worldline(novel_id: str):
    worldline = _service.repository.get_prehistory_worldline(novel_id)
    if not worldline:
        raise HTTPException(status_code=404, detail="prehistory worldline not found")
    return worldline


@router.get("/novels/{novel_id}/timeline/review-records")
async def list_review_records(novel_id: str, limit: int = 30):
    return _service.list_review_records(novel_id, limit=limit)


@router.get("/novels/{novel_id}/agent/status")
async def get_agent_status(novel_id: str):
    return _service.get_agent_status(novel_id)


@router.get("/novels/{novel_id}/diagnostics")
async def get_diagnostics(novel_id: str):
    return _service.get_diagnostics(novel_id)


@router.post("/novels/{novel_id}/chapters/{chapter_number}/review")
async def review_chapter(novel_id: str, chapter_number: int, payload: Optional[dict] = None):
    body = payload or {}
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required for review")
    return _service.review_chapter(
        {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "trigger_type": "manual",
            "payload": {"content": content},
        }
    )


@router.post("/novels/{novel_id}/chapters/{chapter_number}/rollback")
async def rollback_chapter(novel_id: str, chapter_number: int, payload: Optional[dict] = None):
    return await _service.rollback({"novel_id": novel_id, "chapter_number": chapter_number, "trigger_type": "manual", **(payload or {})})


@router.post("/novels/{novel_id}/chapters/{chapter_number}/rerun")
async def rerun_chapter(novel_id: str, chapter_number: int, payload: Optional[dict] = None):
    body = payload or {}
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required for rerun")
    return await _service.after_commit(
        {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "trigger_type": "manual",
            "payload": {"content": content},
        }
    )


@router.post("/novels/{novel_id}/rebuild")
async def rebuild_novel(novel_id: str, payload: Optional[dict] = None):
    return await _service.manual_rebuild({"novel_id": novel_id, "trigger_type": "manual", **(payload or {})})
