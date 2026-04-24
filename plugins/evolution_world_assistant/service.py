"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage

from .extractor import extract_chapter_facts
from .repositories import EvolutionWorldRepository

PLUGIN_NAME = "evolution_world_assistant"


class EvolutionWorldAssistantService:
    def __init__(
        self,
        storage: Optional[PluginStorage] = None,
        jobs: Optional[PluginJobRegistry] = None,
        repository: Optional[EvolutionWorldRepository] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)
        self.repository = repository or EvolutionWorldRepository(self.storage)

    async def after_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id or not chapter_number or not content:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number/content"}

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
        snapshot = extract_chapter_facts(novel_id, chapter_number, content_hash, content, _now())
        known_names = [card.get("name") for card in self.repository.list_character_cards(novel_id).get("items", [])]
        for name in known_names:
            if name and name in content and name not in snapshot.characters:
                snapshot.characters.append(name)
        self.repository.save_fact_snapshot(snapshot)
        updated_cards = self.repository.upsert_character_cards(novel_id, snapshot)
        self.repository.append_event(
            novel_id,
            {"type": "chapter_committed", "chapter_number": chapter_number, "content_hash": content_hash, "at": _now()},
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
        return {"ok": True, "data": {"facts": snapshot.to_dict(), "characters_updated": updated_cards}}

    async def before_context_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        summary = self.build_context_summary(novel_id, chapter_number)
        if not summary:
            return {"ok": True, "skipped": True, "reason": "no evolution state yet"}

        return {
            "ok": True,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution World State",
                    "content": summary,
                    "priority": 60,
                    "token_budget": 1200,
                    "metadata": {"novel_id": novel_id, "chapter_number": chapter_number},
                }
            ],
        }

    async def manual_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapters = payload.get("chapters") or []
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        if not isinstance(chapters, list) or not chapters:
            return {"ok": True, "skipped": True, "reason": "chapters payload is required for rebuild", "data": {"novel_id": novel_id}}

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
        return {"ok": True, "data": {"novel_id": novel_id, "rebuilt_chapters": rebuilt}}

    async def rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id or not chapter_number:
            return {"ok": False, "error": "missing novel_id/chapter_number"}
        return {"ok": True, "skipped": True, "reason": "rollback deletion is reserved for Phase 2", "data": {"novel_id": novel_id, "chapter_number": chapter_number}}

    def list_characters(self, novel_id: str) -> dict[str, Any]:
        return self.repository.list_character_cards(novel_id)

    def get_character(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        return self.repository.get_character_card(novel_id, character_id)

    def list_character_timeline(self, novel_id: str, character_id: str) -> dict[str, Any]:
        card = self.get_character(novel_id, character_id)
        if not card:
            return {"items": []}
        return {"character": card, "items": card.get("recent_events", [])}

    def build_context_summary(self, novel_id: str, chapter_number: Optional[int]) -> str:
        facts = self.repository.list_fact_snapshots(novel_id, before_chapter=chapter_number)
        if not facts:
            return ""
        lines: list[str] = []
        characters = self.repository.list_character_cards(novel_id).get("items", [])
        if characters:
            lines.append("【动态角色状态】")
            for card in characters[:10]:
                lines.append(
                    f"- {card.get('name')}：首次第{card.get('first_seen_chapter')}章，最近第{card.get('last_seen_chapter')}章出现。"
                )
        lines.append("【近期章节事实】")
        for fact in facts[-5:]:
            chapter = fact.get("chapter_number")
            summary = fact.get("summary") or ""
            locations = "、".join(fact.get("locations") or [])
            suffix = f" 地点：{locations}" if locations else ""
            lines.append(f"- 第{chapter}章：{summary}{suffix}")
        return "\n".join(lines)


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
