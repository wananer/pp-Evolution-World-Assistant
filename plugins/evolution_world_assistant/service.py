"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage

from .context_patch import build_context_patch, render_patch_summary
from .preset_converter import convert_st_preset
from .repositories import EvolutionWorldRepository
from .structured_extractor import StructuredExtractorProvider, extract_structured_chapter_facts

PLUGIN_NAME = "evolution_world_assistant"


class EvolutionWorldAssistantService:
    def __init__(
        self,
        storage: Optional[PluginStorage] = None,
        jobs: Optional[PluginJobRegistry] = None,
        repository: Optional[EvolutionWorldRepository] = None,
        extractor_provider: Optional[StructuredExtractorProvider] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)
        self.repository = repository or EvolutionWorldRepository(self.storage)
        self.extractor_provider = extractor_provider

    async def after_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id or not chapter_number or not content:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number/content"}

        started_at = _now()
        start_time = perf_counter()
        trigger_type = str(payload.get("trigger_type") or "auto")
        content_hash = str(payload.get("content_hash") or _hash_text(content))
        dedup_key = self.jobs.build_dedup_key(
            PLUGIN_NAME,
            "after_commit",
            novel_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            trigger_type=trigger_type,
        )
        extraction = await extract_structured_chapter_facts(
            novel_id,
            chapter_number,
            content_hash,
            content,
            _now(),
            provider=self.extractor_provider,
        )
        snapshot = extraction.snapshot
        known_names = [card.get("name") for card in self.repository.list_character_cards(novel_id).get("items", [])]
        for name in known_names:
            if name and name in content and name not in snapshot.characters:
                snapshot.characters.append(name)
        previous_snapshot = self.repository.get_fact_snapshot(novel_id, chapter_number)
        self.repository.save_fact_snapshot(snapshot)
        updated_cards = self.repository.upsert_character_cards(novel_id, snapshot)
        finished_at = _now()
        duration_ms = int((perf_counter() - start_time) * 1000)
        self.repository.append_event(
            novel_id,
            {"type": "chapter_committed", "chapter_number": chapter_number, "content_hash": content_hash, "at": finished_at},
        )
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"{chapter_number}-{content_hash}-{trigger_type}",
                "hook_name": "after_commit",
                "trigger_type": trigger_type,
                "chapter_number": chapter_number,
                "content_hash": content_hash,
                "status": "succeeded",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "input": {"content_length": len(content)},
                "output": {
                    "characters": snapshot.characters,
                    "locations": snapshot.locations,
                    "world_events": snapshot.world_events,
                    "extraction_source": extraction.source,
                    "warnings": extraction.warnings,
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                    "replaced_existing_snapshot": bool(previous_snapshot),
                },
            },
        )
        self.jobs.append(
            PluginJobRecord(
                plugin_name=PLUGIN_NAME,
                hook_name="after_commit",
                novel_id=novel_id,
                chapter_number=chapter_number,
                trigger_type=trigger_type,
                dedup_key=dedup_key,
                content_hash=content_hash,
                status="succeeded",
                input_json={"chapter_number": chapter_number},
                output_json={
                    "facts_path": f"facts/chapter_{chapter_number}.json",
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                },
            )
        )
        return {"ok": True, "data": {"facts": snapshot.to_dict(), "characters_updated": updated_cards, "extraction": extraction.to_dict()}}

    async def before_context_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        patch = self.build_context_patch(novel_id, chapter_number)
        summary = render_patch_summary(patch)
        if not summary:
            return {"ok": True, "skipped": True, "reason": "no evolution state yet"}

        return {
            "ok": True,
            "context_patch": patch,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution World State",
                    "content": summary,
                    "priority": 60,
                    "token_budget": patch.get("estimated_token_budget") or 1200,
                    "metadata": {"novel_id": novel_id, "chapter_number": chapter_number, "patch_schema_version": patch.get("schema_version")},
                }
            ],
        }

    async def manual_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapters = payload.get("chapters") or []
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        if not isinstance(chapters, list) or not chapters:
            cards = self.repository.rebuild_character_cards_from_facts(novel_id)
            self.repository.append_workflow_run(
                novel_id,
                {
                    "run_id": f"rebuild-existing-{_hash_text(_now())}",
                    "hook_name": "manual_rebuild",
                    "trigger_type": "manual",
                    "status": "succeeded",
                    "started_at": _now(),
                    "finished_at": _now(),
                    "input": {"mode": "existing_facts"},
                    "output": {"characters_rebuilt": len(cards)},
                },
            )
            return {"ok": True, "data": {"novel_id": novel_id, "mode": "existing_facts", "characters_rebuilt": len(cards)}}

        rebuilt = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            result = await self.after_commit(
                {
                    "novel_id": novel_id,
                    "chapter_number": chapter.get("chapter_number") or chapter.get("number"),
                    "trigger_type": "manual_rebuild",
                    "payload": {"content": chapter.get("content") or ""},
                }
            )
            if result.get("ok") and not result.get("skipped"):
                rebuilt.append(result["data"]["facts"]["chapter_number"])
        cards = self.repository.rebuild_character_cards_from_facts(novel_id)
        return {"ok": True, "data": {"novel_id": novel_id, "rebuilt_chapters": rebuilt, "characters_rebuilt": len(cards)}}

    async def rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id or not chapter_number:
            return {"ok": False, "error": "missing novel_id/chapter_number"}

        removed = self.repository.delete_fact_snapshot(novel_id, chapter_number)
        cards = self.repository.rebuild_character_cards_from_facts(novel_id)
        event = {
            "type": "chapter_rollback",
            "chapter_number": chapter_number,
            "removed_snapshot": removed,
            "characters_rebuilt": len(cards),
            "at": _now(),
        }
        self.repository.append_event(novel_id, event)
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"rollback-{chapter_number}-{_hash_text(_now())}",
                "hook_name": "rollback",
                "trigger_type": str(payload.get("trigger_type") or "manual"),
                "chapter_number": chapter_number,
                "status": "succeeded",
                "started_at": event["at"],
                "finished_at": _now(),
                "input": {"chapter_number": chapter_number},
                "output": {"removed_snapshot": removed, "characters_rebuilt": len(cards)},
            },
        )
        return {"ok": True, "data": {"novel_id": novel_id, "chapter_number": chapter_number, "removed_snapshot": removed, "characters_rebuilt": len(cards)}}

    def import_st_preset(self, novel_id: str, preset: dict[str, Any]) -> dict[str, Any]:
        converted = convert_st_preset(preset)
        self.repository.save_imported_flows(novel_id, converted)
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"import-flows-{_hash_text(_now())}",
                "hook_name": "import_st_preset",
                "trigger_type": "manual",
                "status": "succeeded",
                "started_at": _now(),
                "finished_at": _now(),
                "input": {"source": converted.get("source")},
                "output": {"flows_imported": len(converted.get("flows") or []), "unsupported": converted.get("unsupported") or []},
            },
        )
        return {"ok": True, "data": converted}

    def list_imported_flows(self, novel_id: str) -> dict[str, Any]:
        return self.repository.list_imported_flows(novel_id)

    def list_runs(self, novel_id: str, limit: int = 50) -> dict[str, Any]:
        return {"items": self.repository.list_workflow_runs(novel_id, limit=limit)}

    def list_events(self, novel_id: str) -> dict[str, Any]:
        return {"items": self.repository.list_events(novel_id)}

    def list_snapshots(self, novel_id: str) -> dict[str, Any]:
        return {"items": self.repository.list_fact_snapshots(novel_id)}

    def list_characters(self, novel_id: str) -> dict[str, Any]:
        return self.repository.list_character_cards(novel_id)

    def get_character(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        return self.repository.get_character_card(novel_id, character_id)

    def list_character_timeline(self, novel_id: str, character_id: str) -> dict[str, Any]:
        card = self.get_character(novel_id, character_id)
        if not card:
            return {"items": []}
        return {"character": card, "items": card.get("recent_events", [])}

    def build_context_patch(self, novel_id: str, chapter_number: Optional[int]) -> dict[str, Any]:
        facts = self.repository.list_fact_snapshots(novel_id, before_chapter=chapter_number)
        characters = self.repository.list_character_cards(novel_id).get("items", [])
        return build_context_patch(novel_id, chapter_number, characters, facts)

    def build_context_summary(self, novel_id: str, chapter_number: Optional[int]) -> str:
        return render_patch_summary(self.build_context_patch(novel_id, chapter_number))


def _extract_content(payload: dict[str, Any]) -> str:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return str(payload.get("content") or nested.get("content") or nested.get("chapter_content") or "").strip()


def _hash_text(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()[:16]


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
