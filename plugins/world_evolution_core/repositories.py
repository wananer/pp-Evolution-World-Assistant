"""Database-backed repositories for Evolution World plugin state."""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.plugin_storage import PluginStorage

from .agent_assets import default_genes, summarize_agent_status
from .agent_status_summary import (
    active_gene_versions,
    agent_api_usage_from_control_cards,
    agent_orchestration_summary,
    auto_evolution_summary,
    context_injection_tier_summary,
    knowledge_base_summary,
    native_context_alignment,
    normalize_host_context_summary,
    normalize_planning_alignment,
)
from .models import ChapterFactSnapshot, CharacterCard
from .personality_palette import merge_palette_missing_fields, personality_palette_status

PLUGIN_NAME = "world_evolution_core"
RECENT_CONTEXT_FACT_LIMIT = 12
RECENT_CONTEXT_CHARACTER_LIMIT = 80


class EvolutionWorldRepository:
    def __init__(self, storage: Optional[PluginStorage] = None) -> None:
        self.storage = storage or PluginStorage()

    def save_fact_snapshot(self, snapshot: ChapterFactSnapshot) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", snapshot.novel_id, "facts", f"chapter_{snapshot.chapter_number}.json"],
            snapshot.to_dict(),
        )
        self._upsert_fact_index_entry(snapshot.novel_id, snapshot.to_dict())

    def delete_fact_snapshot(self, novel_id: str, chapter_number: int) -> bool:
        removed = self._delete_scope(["novels", novel_id, "facts", f"chapter_{chapter_number}.json"])
        if removed:
            self._remove_fact_index_entry(novel_id, chapter_number)
        return removed

    def save_chapter_summary(self, novel_id: str, chapter_number: int, summary: dict[str, Any]) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "summaries", "chapters", f"chapter_{chapter_number}.json"],
            summary,
        )

    def delete_chapter_summary(self, novel_id: str, chapter_number: int) -> bool:
        return self._delete_scope(["novels", novel_id, "summaries", "chapters", f"chapter_{chapter_number}.json"])

    def list_chapter_summaries(self, novel_id: str, before_chapter: Optional[int] = None, limit: int = 10) -> list[dict[str, Any]]:
        items = []
        for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "summaries", "chapters"]):
            if not isinstance(data, dict):
                continue
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and chapter_number and chapter_number >= before_chapter:
                continue
            items.append(data)
        items = sorted(items, key=lambda item: int(item.get("chapter_number") or 0))
        return items[-limit:] if limit > 0 else items

    def save_volume_summary(self, novel_id: str, volume_index: int, summary: dict[str, Any]) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "summaries", "volumes", f"volume_{volume_index}.json"],
            summary,
        )

    def list_volume_summaries(self, novel_id: str, before_chapter: Optional[int] = None, limit: int = 3) -> list[dict[str, Any]]:
        items = []
        for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "summaries", "volumes"]):
            if not isinstance(data, dict):
                continue
            end = _int_or_none(data.get("chapter_end"))
            if before_chapter and end and end >= before_chapter:
                continue
            items.append(data)
        items = sorted(items, key=lambda item: int(item.get("chapter_end") or 0))
        return items[-limit:] if limit > 0 else items

    def get_fact_snapshot(self, novel_id: str, chapter_number: int) -> dict[str, Any] | None:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "facts", f"chapter_{chapter_number}.json"],
            default=None,
        )
        return data if isinstance(data, dict) else None

    def list_fact_snapshots(
        self,
        novel_id: str,
        before_chapter: Optional[int] = None,
        *,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        indexed = self._list_fact_index(novel_id)
        if indexed:
            selected = []
            for entry in indexed:
                chapter_number = _int_or_none(entry.get("chapter_number"))
                if not chapter_number:
                    continue
                if before_chapter and chapter_number >= before_chapter:
                    continue
                selected.append(entry)
            selected.sort(key=lambda item: int(item.get("chapter_number") or 0))
            if limit is not None and limit > 0:
                selected = selected[-limit:]
            items = []
            for entry in selected:
                chapter_number = _int_or_none(entry.get("chapter_number"))
                if not chapter_number:
                    continue
                data = self.get_fact_snapshot(novel_id, chapter_number)
                if isinstance(data, dict):
                    items.append(data)
            return items

        items = []
        for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "facts"]):
            if not isinstance(data, dict):
                continue
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and (not chapter_number or chapter_number >= before_chapter):
                continue
            items.append(data)
        items = sorted(items, key=lambda item: int(item.get("chapter_number") or 0))
        if limit is not None and limit > 0:
            return items[-limit:]
        return items

    def upsert_character_cards(self, novel_id: str, snapshot: ChapterFactSnapshot, character_updates: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        updates_by_name = {item.get("name"): item for item in (character_updates or []) if item.get("name")}
        updated = []
        for name in snapshot.characters:
            if not _is_valid_character_entity(name):
                self.write_character_card(
                    novel_id,
                    {
                        "character_id": _slug(name),
                        "name": name,
                        "first_seen_chapter": snapshot.chapter_number,
                        "last_seen_chapter": snapshot.chapter_number,
                        "status": "invalid_entity",
                        "entity_type": "non_person",
                        "invalid_reason": "filtered_non_character_entity",
                    },
                )
                continue
            current = self.get_character_card(novel_id, name) or CharacterCard(
                character_id=_slug(name),
                name=name,
                first_seen_chapter=snapshot.chapter_number,
                last_seen_chapter=snapshot.chapter_number,
            ).to_dict()
            current = _ensure_character_defaults(current)
            current["last_seen_chapter"] = max(int(current.get("last_seen_chapter") or 0), snapshot.chapter_number)
            current.setdefault("recent_events", [])
            update = updates_by_name.get(name) or {}
            _merge_canonical_identity(current, update)
            _merge_character_life_state(current, update, snapshot.chapter_number)
            event_summary = _character_event_summary(name, snapshot)
            if event_summary:
                current["recent_events"].append(
                    {
                        "chapter_number": snapshot.chapter_number,
                        "summary": event_summary,
                        "locations": snapshot.locations[:5],
                        "inner_change": update.get("inner_change") or "",
                        "knowledge_delta": update.get("knowledge_delta") or "",
                    }
                )
                current["recent_events"] = current["recent_events"][-8:]
            self.write_character_card(novel_id, current)
            updated.append(current)
        return updated

    def record_invalid_character_candidates(self, novel_id: str, names: list[str], *, chapter_number: int) -> None:
        for name in _dedupe_strings(names):
            if not name or _is_valid_character_entity(name):
                continue
            self.write_character_card(
                novel_id,
                {
                    "character_id": _slug(name),
                    "name": name,
                    "first_seen_chapter": chapter_number,
                    "last_seen_chapter": chapter_number,
                    "status": "invalid_entity",
                    "entity_type": "non_person",
                    "invalid_reason": "filtered_non_character_entity",
                },
            )

    def merge_character_updates(self, novel_id: str, character_updates: list[dict[str, Any]], *, chapter_number: Optional[int] = None) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        for update in character_updates:
            if not isinstance(update, dict):
                continue
            name = str(update.get("name") or "").strip()
            if not name:
                continue
            current = self.get_character_card(novel_id, name)
            if not current or _is_invalid_character_entry(current):
                continue
            current = _ensure_character_defaults(current)
            _merge_canonical_identity(current, update)
            _merge_character_life_state(
                current,
                update,
                chapter_number or _int_or_none(current.get("last_seen_chapter")) or 0,
            )
            updated.append(self.write_character_card(novel_id, current))
        return updated

    def rebuild_character_cards_from_facts(self, novel_id: str) -> list[dict[str, Any]]:
        existing_by_name = {
            card.get("name"): _ensure_character_defaults(dict(card))
            for card in self.list_character_cards(novel_id).get("items", [])
            if card.get("name")
        }
        by_name: dict[str, dict[str, Any]] = {}
        for fact in self.list_fact_snapshots(novel_id):
            snapshot = _snapshot_from_dict(fact)
            for name in snapshot.characters:
                if not _is_valid_character_entity(name):
                    invalid = _ensure_character_defaults(
                        {
                            "character_id": _slug(name),
                            "name": name,
                            "first_seen_chapter": snapshot.chapter_number,
                            "last_seen_chapter": snapshot.chapter_number,
                            "status": "invalid_entity",
                            "entity_type": "non_person",
                            "invalid_reason": "filtered_non_character_entity",
                        }
                    )
                    by_name[name] = invalid
                    continue
                current = by_name.get(name) or _rebuild_seed_card(existing_by_name.get(name), name, snapshot.chapter_number)
                current["first_seen_chapter"] = min(
                    int(current.get("first_seen_chapter") or snapshot.chapter_number),
                    snapshot.chapter_number,
                )
                current["last_seen_chapter"] = max(
                    int(current.get("last_seen_chapter") or 0),
                    snapshot.chapter_number,
                )
                event_summary = _character_event_summary(name, snapshot)
                if event_summary:
                    current.setdefault("recent_events", []).append(
                        {
                            "chapter_number": snapshot.chapter_number,
                            "summary": event_summary,
                            "locations": snapshot.locations[:5],
                        }
                    )
                by_name[name] = current
        next_cards = sorted(by_name.values(), key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
        for card in next_cards:
            card["recent_events"] = sorted(card.get("recent_events") or [], key=lambda item: item.get("chapter_number") or 0)[-8:]
        self.write_character_cards(novel_id, next_cards)
        return next_cards

    def write_character_cards(self, novel_id: str, cards: list[dict[str, Any]]) -> None:
        normalized = [self._prepare_character_card(dict(card)) for card in cards]
        normalized.sort(key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
        for card in normalized:
            self.write_character_card(novel_id, card)
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "characters_index.json"],
            {"items": [_character_index_entry(card) for card in normalized]},
        )
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], {"items": normalized})

    def write_character_card(self, novel_id: str, card: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_character_card(card)
        character_id = str(prepared.get("character_id") or _slug(str(prepared.get("name") or "")))
        prepared["character_id"] = character_id
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "characters", f"{character_id}.json"], prepared)
        self._upsert_character_index_entry(novel_id, prepared)
        return prepared

    def list_character_cards(self, novel_id: str) -> dict[str, Any]:
        return self._list_character_cards(novel_id, include_invalid=False)

    def list_all_character_cards(self, novel_id: str) -> dict[str, Any]:
        return self._list_character_cards(novel_id, include_invalid=True)

    def _list_character_cards(self, novel_id: str, *, include_invalid: bool) -> dict[str, Any]:
        index = self.list_character_index(novel_id).get("items", [])
        if index:
            items = []
            for entry in index:
                card = self.get_character_card(novel_id, str(entry.get("character_id") or entry.get("name") or ""))
                if card and (include_invalid or not _is_invalid_character_entry(card)):
                    items.append(card)
            items.sort(key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
            return {"items": items}
        legacy = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], default={"items": []})
        items = legacy.get("items") if isinstance(legacy, dict) else []
        if isinstance(items, list) and items:
            self.write_character_cards(novel_id, [item for item in items if isinstance(item, dict)])
            return {
                "items": [
                    item
                    for item in items
                    if isinstance(item, dict) and (include_invalid or not _is_invalid_character_entry(item))
                ]
            }
        return {"items": []}

    def list_character_index(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters_index.json"], default={"items": []})
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = [item for item in data["items"] if isinstance(item, dict)]
            items.sort(key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
            return {"items": items}
        return {"items": []}

    def list_relevant_character_cards(self, novel_id: str, text: str = "", *, limit: int = RECENT_CONTEXT_CHARACTER_LIMIT) -> dict[str, Any]:
        index = self.list_character_index(novel_id).get("items", [])
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in [*(entry for entry in index if text and _card_is_mentioned(entry, text)), *index[-limit:]]:
            if _is_invalid_character_entry(entry):
                continue
            key = str(entry.get("character_id") or entry.get("name") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            selected.append(entry)
        selected = selected[-limit:] if limit > 0 else selected
        items = []
        for entry in selected:
            card = self.get_character_card(novel_id, str(entry.get("character_id") or entry.get("name") or ""))
            if card and not _is_invalid_character_entry(card):
                items.append(card)
        return {"items": items}

    def get_character_card(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        lookup = str(character_id or "").strip()
        if not lookup:
            return None
        direct = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters", f"{_slug(lookup)}.json"], default=None)
        if isinstance(direct, dict):
            return _ensure_character_defaults(direct)
        for entry in self.list_character_index(novel_id).get("items", []):
            aliases = [entry.get("name"), entry.get("character_id"), *(entry.get("aliases") or [])]
            if lookup in {str(item or "") for item in aliases}:
                data = self.storage.read_json(
                    PLUGIN_NAME,
                    ["novels", novel_id, "characters", f"{entry.get('character_id')}.json"],
                    default=None,
                )
                return _ensure_character_defaults(data) if isinstance(data, dict) else None
        legacy = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], default={"items": []})
        for card in legacy.get("items", []) if isinstance(legacy, dict) else []:
            if card.get("character_id") == lookup or card.get("name") == lookup:
                self.write_character_card(novel_id, card)
                return _ensure_character_defaults(card)
        return None

    def append_event(self, novel_id: str, event: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "events.jsonl"], event)

    def list_events(self, novel_id: str) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "events.jsonl"])

    def save_timeline_events(self, novel_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            event_id = str(event.get("event_id") or _slug(str(event.get("summary") or "")))
            chapter_number = int(event.get("chapter_number") or 0)
            if not event_id or chapter_number <= 0:
                continue
            self.storage.write_json(
                PLUGIN_NAME,
                ["novels", novel_id, "timeline", "events", f"chapter_{chapter_number}", f"{event_id}.json"],
                event,
            )

    def list_timeline_events(self, novel_id: str, before_chapter: Optional[int] = None, limit: int = 24) -> list[dict[str, Any]]:
        items = []
        for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "timeline", "events"]):
            if not isinstance(data, dict):
                continue
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and chapter_number and chapter_number >= before_chapter:
                continue
            items.append(data)
        items = sorted(items, key=lambda item: (int(item.get("chapter_number") or 0), int(item.get("scene_order") or 0), str(item.get("event_id") or "")))
        return items[-limit:] if limit > 0 else items

    def delete_timeline_events_for_chapter(self, novel_id: str, chapter_number: int) -> int:
        removed = 0
        for event in self.list_timeline_events(novel_id, limit=0):
            if int(event.get("chapter_number") or 0) != chapter_number:
                continue
            event_id = str(event.get("event_id") or _slug(str(event.get("summary") or "")))
            if self._delete_scope(["novels", novel_id, "timeline", "events", f"chapter_{chapter_number}", f"{event_id}.json"]):
                removed += 1
        return removed

    def save_continuity_constraints(self, novel_id: str, constraints: list[dict[str, Any]]) -> None:
        for constraint in constraints:
            constraint_id = str(constraint.get("constraint_id") or _slug(str(constraint.get("rule") or "")))
            if not constraint_id:
                continue
            self.storage.write_json(
                PLUGIN_NAME,
                ["novels", novel_id, "timeline", "constraints", f"{constraint_id}.json"],
                constraint,
            )

    def list_continuity_constraints(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        items = [data for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "timeline", "constraints"]) if isinstance(data, dict)]
        items = sorted(items, key=lambda item: (str(item.get("subject") or ""), str(item.get("type") or ""), str(item.get("constraint_id") or "")))
        return items[-limit:] if limit > 0 else items

    def delete_continuity_constraints_for_chapter(self, novel_id: str, chapter_number: int) -> int:
        removed = 0
        for constraint in self.list_continuity_constraints(novel_id, limit=0):
            constraint_chapter = int(constraint.get("chapter_number") or constraint.get("created_or_updated_chapter") or 0)
            if constraint_chapter != chapter_number:
                continue
            constraint_id = str(constraint.get("constraint_id") or _slug(str(constraint.get("rule") or "")))
            if self._delete_scope(["novels", novel_id, "timeline", "constraints", f"{constraint_id}.json"]):
                removed += 1
        return removed

    def save_story_graph_chapter(self, novel_id: str, chapter_number: int, graph: dict[str, Any]) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "story_graph", "chapters", f"chapter_{chapter_number}.json"],
            graph,
        )
        self._upsert_story_graph_index_entry(novel_id, graph)

    def delete_story_graph_chapter(self, novel_id: str, chapter_number: int) -> bool:
        removed = self._delete_scope(["novels", novel_id, "story_graph", "chapters", f"chapter_{chapter_number}.json"])
        if removed:
            self._remove_story_graph_index_entry(novel_id, chapter_number)
        return removed

    def get_story_graph_chapter(self, novel_id: str, chapter_number: int) -> dict[str, Any] | None:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "story_graph", "chapters", f"chapter_{chapter_number}.json"],
            default=None,
        )
        return data if isinstance(data, dict) else None

    def list_story_graph_chapters(self, novel_id: str, before_chapter: Optional[int] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
        indexed = self._list_story_graph_index(novel_id)
        selected = []
        if indexed:
            for entry in indexed:
                chapter_number = _int_or_none(entry.get("chapter_number"))
                if not chapter_number:
                    continue
                if before_chapter and chapter_number >= before_chapter:
                    continue
                selected.append(entry)
            selected.sort(key=lambda item: int(item.get("chapter_number") or 0))
            if limit is not None and limit > 0:
                selected = selected[-limit:]
            chapters = []
            for entry in selected:
                chapter_number = _int_or_none(entry.get("chapter_number"))
                if not chapter_number:
                    continue
                data = self.get_story_graph_chapter(novel_id, chapter_number)
                if isinstance(data, dict):
                    chapters.append(data)
            return chapters

        items = []
        for data in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "story_graph", "chapters"]):
            if not isinstance(data, dict):
                continue
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and chapter_number and chapter_number >= before_chapter:
                continue
            items.append(data)
        items = sorted(items, key=lambda item: int(item.get("chapter_number") or 0))
        if limit is not None and limit > 0:
            return items[-limit:]
        return items

    def list_route_conflicts(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        conflicts = []
        for chapter in self.list_story_graph_chapters(novel_id):
            conflicts.extend(item for item in chapter.get("conflicts") or [] if isinstance(item, dict))
        conflicts = sorted(conflicts, key=lambda item: (int(item.get("chapter_current") or 0), str(item.get("type") or "")))
        return conflicts[-limit:] if limit > 0 else conflicts

    def append_review_record(self, novel_id: str, record: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "timeline", "review_records.jsonl"], record)

    def list_review_records(self, novel_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "timeline", "review_records.jsonl"], limit=limit)

    def upsert_review_candidate(self, novel_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id:
            raise ValueError("review candidate id is required")
        prepared = dict(candidate)
        prepared["novel_id"] = novel_id
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "review_candidates", f"{candidate_id}.json"],
            prepared,
        )
        return prepared

    def get_review_candidate(self, novel_id: str, candidate_id: str) -> dict[str, Any] | None:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "review_candidates", f"{candidate_id}.json"],
            default=None,
        )
        return data if isinstance(data, dict) else None

    def list_review_candidates(
        self,
        novel_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        items = [
            item
            for item in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "review_candidates"])
            if isinstance(item, dict)
        ]
        if status:
            items = [item for item in items if str(item.get("status") or "") == status]
        items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        return items[-limit:] if limit > 0 else items

    def append_context_injection_record(self, novel_id: str, record: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "context", "injection_records.jsonl"], record)

    def list_context_injection_records(self, novel_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "context", "injection_records.jsonl"], limit=limit)

    def append_context_control_card_record(self, novel_id: str, record: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "context", "control_cards.jsonl"], record)

    def list_context_control_card_records(self, novel_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "context", "control_cards.jsonl"], limit=limit)

    def upsert_agent_knowledge_document(self, novel_id: str, document: dict[str, Any]) -> None:
        doc_id = str(document.get("id") or "")
        if not doc_id:
            return
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "knowledge", "documents", f"{doc_id}.json"], document)

    def list_agent_knowledge_documents(self, novel_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        items = [
            item
            for item in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "agent", "knowledge", "documents"])
            if isinstance(item, dict)
        ]
        items.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")))
        return items[-limit:] if limit > 0 else items

    def upsert_agent_knowledge_chunk(self, novel_id: str, chunk: dict[str, Any]) -> None:
        chunk_id = str(chunk.get("chunk_id") or "")
        if not chunk_id:
            return
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "knowledge", "chunks", f"{chunk_id}.json"], chunk)

    def list_agent_knowledge_chunks(self, novel_id: str, limit: int = 10000) -> list[dict[str, Any]]:
        items = [
            item
            for item in self.storage.list_json(PLUGIN_NAME, ["novels", novel_id, "agent", "knowledge", "chunks"])
            if isinstance(item, dict)
        ]
        items.sort(key=lambda item: (int(item.get("chapter_number") or 0), str(item.get("source_type") or ""), str(item.get("chunk_id") or "")))
        return items[-limit:] if limit > 0 else items

    def clear_agent_knowledge(self, novel_id: str) -> dict[str, int]:
        documents = 0
        chunks = 0
        for document in self.list_agent_knowledge_documents(novel_id, limit=0):
            doc_id = str(document.get("id") or "")
            if doc_id and self._delete_scope(["novels", novel_id, "agent", "knowledge", "documents", f"{doc_id}.json"]):
                documents += 1
        for chunk in self.list_agent_knowledge_chunks(novel_id, limit=0):
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and self._delete_scope(["novels", novel_id, "agent", "knowledge", "chunks", f"{chunk_id}.json"]):
                chunks += 1
        return {"documents": documents, "chunks": chunks}

    def append_agent_decision_record(self, novel_id: str, record: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "decisions.jsonl"], record)

    def list_agent_decision_records(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "decisions.jsonl"], limit=limit)

    def append_gene_version(self, novel_id: str, version: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "gene_versions.jsonl"], version)

    def list_gene_versions(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "gene_versions.jsonl"], limit=limit)

    def get_settings(self) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["settings.json"], default={})
        return data if isinstance(data, dict) else {}

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["settings.json"], settings)

    def build_review_evidence(
        self,
        novel_id: str,
        content: str = "",
        *,
        before_chapter: Optional[int] = None,
        limit: int = 12,
    ) -> dict[str, list[dict[str, Any]]]:
        text = str(content or "")
        cards = self.list_relevant_character_cards(novel_id, text, limit=limit).get("items", [])
        mentioned_cards = [card for card in cards if _card_is_mentioned(card, text)]
        events = self.list_timeline_events(novel_id, before_chapter=before_chapter, limit=60)
        constraints = self.list_continuity_constraints(novel_id)
        route_conflicts = self.list_route_conflicts(novel_id)
        route_constraints = [_route_conflict_as_constraint(item) for item in route_conflicts]
        constraints = [*constraints, *route_constraints]
        if text:
            relevant_events = [event for event in events if _record_mentions(event, text)]
            relevant_constraints = [constraint for constraint in constraints if _record_mentions(constraint, text)]
        else:
            relevant_events = events
            relevant_constraints = constraints
        if not relevant_events:
            relevant_events = events[-limit:]
        if not relevant_constraints:
            relevant_constraints = constraints[:limit]
        return {
            "characters": mentioned_cards or cards[-limit:],
            "events": relevant_events[-limit:],
            "constraints": relevant_constraints[:limit],
            "route_conflicts": route_conflicts[-limit:],
        }

    def save_prehistory_worldline(self, novel_id: str, worldline: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "prehistory", "worldline.json"], worldline)

    def get_prehistory_worldline(self, novel_id: str) -> dict[str, Any] | None:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "prehistory", "worldline.json"],
            default=None,
        )
        return data if isinstance(data, dict) else None

    def build_story_planning_evidence(
        self,
        novel_id: str,
        *,
        purpose: str = "story_planning",
        limit: int = 8,
    ) -> dict[str, Any]:
        worldline = self.get_prehistory_worldline(novel_id)
        if not worldline:
            return {}
        eras = list(worldline.get("eras") or [])[-limit:]
        seeds = list(worldline.get("foreshadow_seeds") or [])[:limit]
        forces = list(worldline.get("forces") or [])[:limit]
        guidance = list(worldline.get("planning_guidance") or [])[:limit]
        return {
            "purpose": purpose,
            "worldline": worldline,
            "eras": eras,
            "foreshadow_seeds": seeds,
            "forces": forces,
            "planning_guidance": guidance,
        }


    def list_agent_genes(self, novel_id: str) -> list[dict[str, Any]]:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "agent", "genes.json"],
            default=None,
        )
        stored = data.get("items") if isinstance(data, dict) else None
        by_id: dict[str, dict[str, Any]] = {str(gene.get("id") or ""): gene for gene in default_genes() if gene.get("id")}
        for gene in stored or []:
            if isinstance(gene, dict) and gene.get("id"):
                by_id[str(gene["id"])] = gene
        return sorted(by_id.values(), key=lambda item: (-int(item.get("priority") or 0), str(item.get("id") or "")))

    def save_agent_genes(self, novel_id: str, genes: list[dict[str, Any]]) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "agent", "genes.json"],
            {"schema_version": 1, "items": [item for item in genes if isinstance(item, dict)]},
        )

    def list_agent_capsules(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        records = self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "capsules.jsonl"])
        by_id: dict[str, dict[str, Any]] = {}
        for capsule in records:
            capsule_id = str(capsule.get("id") or "")
            if capsule_id:
                by_id[capsule_id] = capsule
        items = [item for item in by_id.values() if not _is_invalid_capsule(item)]
        items.sort(key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("id") or "")))
        return items[-limit:] if limit > 0 else items

    def append_agent_capsule(self, novel_id: str, capsule: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "capsules.jsonl"], capsule)

    def append_agent_reflection(self, novel_id: str, reflection: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "reflections.jsonl"], reflection)

    def list_agent_reflections(self, novel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "reflections.jsonl"], limit=limit)

    def append_agent_gene_candidate(self, novel_id: str, candidate: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "gene_candidates.jsonl"], candidate)

    def list_agent_gene_candidates(self, novel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        records = self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "gene_candidates.jsonl"])
        by_id: dict[str, dict[str, Any]] = {}
        for candidate in records:
            candidate_id = str(candidate.get("id") or "")
            if candidate_id:
                by_id[candidate_id] = candidate
        items = list(by_id.values())
        items.sort(key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("id") or "")))
        return items[-limit:] if limit > 0 else items

    def save_agent_memory_index(self, novel_id: str, index: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "memory_index.json"], index)

    def get_agent_memory_index(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "agent", "memory_index.json"], default={})
        return data if isinstance(data, dict) else {}

    def save_style_repetition_state(self, novel_id: str, state: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "style", "repetition_state.json"], state)

    def get_style_repetition_state(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "style", "repetition_state.json"], default={})
        return data if isinstance(data, dict) else {}

    def save_host_context_summary(self, novel_id: str, summary: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "host_context_summary.json"], summary)

    def get_host_context_summary(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "agent", "host_context_summary.json"], default={})
        return normalize_host_context_summary(data if isinstance(data, dict) else {})

    def save_semantic_recall_summary(self, novel_id: str, summary: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "semantic_recall_summary.json"], summary)

    def get_semantic_recall_summary(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "agent", "semantic_recall_summary.json"], default={})
        return data if isinstance(data, dict) else {}

    def save_planning_alignment(self, novel_id: str, alignment: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "agent", "planning_alignment.json"], alignment)

    def get_planning_alignment(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "agent", "planning_alignment.json"], default={})
        return normalize_planning_alignment(data if isinstance(data, dict) else {})

    def append_agent_event(self, novel_id: str, event: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "events.jsonl"], event)

    def list_agent_events(self, novel_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "events.jsonl"], limit=limit)

    def append_agent_selection_record(self, novel_id: str, record: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "selection_records.jsonl"], record)

    def list_agent_selection_records(self, novel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "agent", "selection_records.jsonl"], limit=limit)

    def get_agent_status(self, novel_id: str) -> dict[str, Any]:
        host_context_summary = self.get_host_context_summary(novel_id)
        return summarize_agent_status(
            genes=self.list_agent_genes(novel_id),
            capsules=self.list_agent_capsules(novel_id),
            events=self.list_agent_events(novel_id),
            selections=self.list_agent_selection_records(novel_id),
            reflections=self.list_agent_reflections(novel_id),
            candidates=self.list_agent_gene_candidates(novel_id),
            memory_index=self.get_agent_memory_index(novel_id),
            host_context_summary=host_context_summary,
            semantic_recall_summary=self.get_semantic_recall_summary(novel_id),
            agent_api_usage=agent_api_usage_from_control_cards(self.list_context_control_card_records(novel_id, limit=500)),
            planning_alignment=self.get_planning_alignment(novel_id),
            native_context_alignment=native_context_alignment(host_context_summary),
            context_injection_summary=context_injection_tier_summary(self.list_context_injection_records(novel_id, limit=1)),
            agent_orchestration=agent_orchestration_summary(self.list_agent_decision_records(novel_id, limit=200)),
            knowledge_base=knowledge_base_summary(self.list_agent_knowledge_documents(novel_id), self.list_agent_knowledge_chunks(novel_id)),
            auto_evolution=auto_evolution_summary(self.list_gene_versions(novel_id, limit=200)),
            active_gene_versions=active_gene_versions(self.list_agent_genes(novel_id)),
            personality_palette_status=personality_palette_status(self.list_all_character_cards(novel_id).get("items", [])),
        )

    def save_diagnostics_snapshot(self, novel_id: str, snapshot: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "diagnostics", "latest.json"], snapshot)
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "diagnostics", "history.jsonl"], snapshot)

    def get_diagnostics_snapshot(self, novel_id: str) -> dict[str, Any]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "diagnostics", "latest.json"], default={})
        return data if isinstance(data, dict) else {}

    def save_imported_flows(self, novel_id: str, converted: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "imported_flows.json"], converted)

    def list_imported_flows(self, novel_id: str) -> dict[str, Any]:
        return self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "imported_flows.json"], default={"schema_version": 1, "flows": [], "unsupported": []})

    def append_workflow_run(self, novel_id: str, run: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "runs.jsonl"], run)

    def list_workflow_runs(self, novel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "runs.jsonl"], limit=limit)

    def _delete_scope(self, scope: list[str]) -> bool:
        try:
            return self.storage.delete_json(PLUGIN_NAME, scope)
        except ValueError:
            return False

    def _list_fact_index(self, novel_id: str) -> list[dict[str, Any]]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "facts_index.json"], default={"items": []})
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = [item for item in data["items"] if isinstance(item, dict)]
            items.sort(key=lambda item: int(item.get("chapter_number") or 0))
            return items
        return []

    def _write_fact_index(self, novel_id: str, items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda item: int(item.get("chapter_number") or 0))
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "facts_index.json"], {"items": items})

    def _upsert_fact_index_entry(self, novel_id: str, snapshot: dict[str, Any]) -> None:
        chapter_number = _int_or_none(snapshot.get("chapter_number"))
        if not chapter_number:
            return
        entries = [item for item in self._list_fact_index(novel_id) if _int_or_none(item.get("chapter_number")) != chapter_number]
        entries.append(
            {
                "chapter_number": chapter_number,
                "content_hash": str(snapshot.get("content_hash") or ""),
                "summary": str(snapshot.get("summary") or "")[:180],
                "characters": [str(item) for item in snapshot.get("characters") or []][:12],
                "locations": [str(item) for item in snapshot.get("locations") or []][:12],
            }
        )
        self._write_fact_index(novel_id, entries)

    def _remove_fact_index_entry(self, novel_id: str, chapter_number: int) -> None:
        entries = [item for item in self._list_fact_index(novel_id) if _int_or_none(item.get("chapter_number")) != chapter_number]
        self._write_fact_index(novel_id, entries)

    def _list_story_graph_index(self, novel_id: str) -> list[dict[str, Any]]:
        data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "story_graph_index.json"], default={"items": []})
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = [item for item in data["items"] if isinstance(item, dict)]
            items.sort(key=lambda item: int(item.get("chapter_number") or 0))
            return items
        return []

    def _write_story_graph_index(self, novel_id: str, items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda item: int(item.get("chapter_number") or 0))
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "story_graph_index.json"], {"items": items})

    def _upsert_story_graph_index_entry(self, novel_id: str, graph: dict[str, Any]) -> None:
        chapter_number = _int_or_none(graph.get("chapter_number"))
        if not chapter_number:
            return
        entries = [item for item in self._list_story_graph_index(novel_id) if _int_or_none(item.get("chapter_number")) != chapter_number]
        entries.append(
            {
                "chapter_number": chapter_number,
                "location_count": len(graph.get("locations") or []),
                "route_edge_count": len(graph.get("route_edges") or []),
                "conflict_count": len(graph.get("conflicts") or []),
                "vector_count": len(graph.get("vectors") or []),
            }
        )
        self._write_story_graph_index(novel_id, entries)

    def _remove_story_graph_index_entry(self, novel_id: str, chapter_number: int) -> None:
        entries = [item for item in self._list_story_graph_index(novel_id) if _int_or_none(item.get("chapter_number")) != chapter_number]
        self._write_story_graph_index(novel_id, entries)

    def _prepare_character_card(self, card: dict[str, Any]) -> dict[str, Any]:
        prepared = _ensure_character_defaults(dict(card))
        name = str(prepared.get("name") or "").strip()
        prepared["name"] = name
        prepared["character_id"] = str(prepared.get("character_id") or _slug(name))
        if not _is_valid_character_entity(name) and str(prepared.get("status") or "") != "invalid_entity":
            prepared["status"] = "invalid_entity"
            prepared["entity_type"] = "non_person"
            prepared["invalid_reason"] = "filtered_non_character_entity"
        prepared["recent_events"] = sorted(prepared.get("recent_events") or [], key=lambda item: item.get("chapter_number") or 0)[-8:]
        return prepared

    def _upsert_character_index_entry(self, novel_id: str, card: dict[str, Any]) -> None:
        entry = _character_index_entry(card)
        if not entry.get("character_id"):
            return
        entries = [
            item
            for item in self.list_character_index(novel_id).get("items", [])
            if item.get("character_id") != entry["character_id"] and item.get("name") != entry.get("name")
        ]
        entries.append(entry)
        entries.sort(key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "characters_index.json"], {"items": entries})


def _ensure_character_defaults(card: dict[str, Any]) -> dict[str, Any]:
    card.setdefault("aliases", [])
    card.setdefault("cognitive_state", {"known_facts": [], "unknowns": [], "misbeliefs": []})
    card.setdefault("emotional_arc", [])
    card.setdefault("growth_arc", {"stage": "未定", "changes": []})
    card.setdefault("capability_limits", [])
    card.setdefault("decision_biases", [])
    card.setdefault("appearance", _default_appearance())
    card.setdefault("attributes", [])
    card.setdefault("world_profile", {"schema_name": "通用角色档案", "fields": []})
    card.setdefault("personality_palette", _default_personality_palette())
    return card


_NON_CHARACTER_NAMES = {
    "金属牌",
    "方向",
    "查询记录",
    "记录",
    "编号",
    "债务",
    "契约",
    "防火门",
    "黑色书籍",
    "书籍",
    "访客卡",
    "臂章",
    "钥匙",
    "章节标题",
    "标题",
    "线索",
    "真相",
    "秘密",
    "记忆",
    "沉默",
    "主线",
}

_NON_CHARACTER_TOKENS = (
    "记录",
    "方向",
    "信息",
    "编号",
    "防火门",
    "金属",
    "箱",
    "匣",
    "书籍",
    "钥匙",
    "教程",
    "章节",
    "标题",
    "线索",
    "真相",
    "秘密",
    "记忆",
    "警报",
    "下方",
)

_NON_CHARACTER_SUFFIXES = ("之谜", "真相", "记录", "线索", "计划", "任务", "报告", "下方", "区域")


def _is_valid_character_entity(name: str) -> bool:
    value = str(name or "").strip()
    if not value or value in _NON_CHARACTER_NAMES:
        return False
    if any(token in value for token in _NON_CHARACTER_TOKENS):
        return False
    if any(value.endswith(suffix) for suffix in _NON_CHARACTER_SUFFIXES):
        return False
    if value.startswith("第") and ("章" in value or "幕" in value):
        return False
    if 6 < len(value) and not any(token in value for token in ("·", "氏", "家", "队", "团")):
        return False
    return True


def _is_invalid_character_entry(card: dict[str, Any]) -> bool:
    return str(card.get("status") or "") == "invalid_entity" or str(card.get("entity_type") or "") == "non_person"


def _is_invalid_capsule(capsule: dict[str, Any]) -> bool:
    issue_type = str(capsule.get("source_issue_type") or "")
    if not issue_type.startswith("evolution_route_"):
        return False
    text = " ".join(
        [
            str(capsule.get("summary") or ""),
            str(capsule.get("guidance") or ""),
            str(capsule.get("evidence") or ""),
        ]
    )
    return any(fragment in text for fragment in ("个信息站", "但他咬牙站", "老板专门", "道防火门", "老板专门"))


def _merge_canonical_identity(card: dict[str, Any], update: dict[str, Any]) -> None:
    canonical_id = str(update.get("canonical_character_id") or "").strip()
    if canonical_id:
        card["canonical_character_id"] = canonical_id
        if not str(card.get("character_id") or "").strip().startswith("c_"):
            card["character_id"] = canonical_id
    for key in ("canonical_source", "profile_source"):
        value = str(update.get(key) or "").strip()
        if value:
            card[key] = value
    aliases = [str(item).strip() for item in update.get("aliases") or [] if str(item).strip()]
    if aliases:
        card["aliases"] = _dedupe_strings([*(card.get("aliases") or []), *aliases])
    summary = str(update.get("summary") or "").strip()
    if summary and not str(card.get("summary") or "").strip():
        card["summary"] = summary[:360]


def _rebuild_seed_card(existing: Optional[dict[str, Any]], name: str, chapter_number: int) -> dict[str, Any]:
    if existing:
        current = _ensure_character_defaults(dict(existing))
        current["first_seen_chapter"] = chapter_number
        current["last_seen_chapter"] = chapter_number
        current["recent_events"] = []
        return current
    return _ensure_character_defaults(
        CharacterCard(
            character_id=_slug(name),
            name=name,
            first_seen_chapter=chapter_number,
            last_seen_chapter=chapter_number,
        ).to_dict()
    )


def _merge_character_life_state(card: dict[str, Any], update: dict[str, Any], chapter_number: int) -> None:
    _merge_appearance(card, update.get("appearance"))
    card["attributes"] = _merge_records(card.get("attributes") or [], update.get("attributes") or [], limit=24)
    _merge_world_profile(card, update.get("world_profile"))
    _merge_personality_palette(card, update.get("personality_palette"))

    cognitive = card.setdefault("cognitive_state", {"known_facts": [], "unknowns": [], "misbeliefs": []})
    for key in ("known_facts", "unknowns", "misbeliefs"):
        cognitive[key] = _merge_limited_strings(cognitive.get(key) or [], update.get(key) or [], limit=10)
    if update.get("inner_change") or update.get("emotion"):
        card.setdefault("emotional_arc", []).append(
            {
                "chapter_number": chapter_number,
                "emotion": str(update.get("emotion") or "").strip(),
                "inner_change": str(update.get("inner_change") or "").strip(),
            }
        )
        card["emotional_arc"] = card["emotional_arc"][-8:]
    growth = card.setdefault("growth_arc", {"stage": "未定", "changes": []})
    if update.get("growth_stage"):
        growth["stage"] = str(update.get("growth_stage"))[:80]
    if update.get("growth_change"):
        growth.setdefault("changes", []).append({"chapter_number": chapter_number, "summary": str(update.get("growth_change"))[:160]})
        growth["changes"] = growth["changes"][-8:]
    card["capability_limits"] = _merge_limited_strings(card.get("capability_limits") or [], update.get("capability_limits") or [], limit=10)
    card["decision_biases"] = _merge_limited_strings(card.get("decision_biases") or [], update.get("decision_biases") or [], limit=8)


def _default_appearance() -> dict[str, Any]:
    return {"summary": "待从正文补充外貌描写", "features": [], "style": [], "current_outfit": "", "marks": []}


def _default_personality_palette() -> dict[str, Any]:
    return {
        "metaphor": "人的性格像调色盘：底色、主色调与点缀共同驱动行为。",
        "base": "",
        "main_tones": [],
        "accents": [],
        "derivatives": [],
        "pressure_triggers": [],
        "relationship_tones": [],
        "voice_signature": [],
        "gesture_signature": [],
        "negative_costs": [],
        "presence_mode": "active_scene",
    }


def _merge_appearance(card: dict[str, Any], incoming: Any) -> None:
    current = card.setdefault("appearance", _default_appearance())
    if not isinstance(incoming, dict):
        return
    summary = str(incoming.get("summary") or "").strip()
    if summary:
        current["summary"] = summary[:240]
    outfit = str(incoming.get("current_outfit") or "").strip()
    if outfit:
        current["current_outfit"] = outfit[:160]
    for key, limit in (("features", 12), ("style", 12), ("marks", 12)):
        current[key] = _merge_limited_strings(current.get(key) or [], incoming.get(key) or [], limit=limit)


def _merge_world_profile(card: dict[str, Any], incoming: Any) -> None:
    current = card.setdefault("world_profile", {"schema_name": "通用角色档案", "fields": []})
    if not isinstance(incoming, dict):
        return
    schema_name = str(incoming.get("schema_name") or "").strip()
    if schema_name:
        current["schema_name"] = schema_name[:80]
    current["fields"] = _merge_records(current.get("fields") or [], incoming.get("fields") or [], limit=24)


def _merge_personality_palette(card: dict[str, Any], incoming: Any) -> None:
    current = card.setdefault("personality_palette", _default_personality_palette())
    card["personality_palette"] = merge_palette_missing_fields(current, incoming)


def _merge_records(existing: list[Any], incoming: list[Any], *, limit: int) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*existing, *incoming]:
        record = _normalize_record(item)
        if not record:
            continue
        key = (record.get("category") or "", record.get("name") or "")
        if key in seen:
            for index, existing_record in enumerate(result):
                existing_key = (existing_record.get("category") or "", existing_record.get("name") or "")
                if existing_key == key:
                    result[index] = {**existing_record, **{k: v for k, v in record.items() if v}}
                    break
            continue
        seen.add(key)
        result.append(record)
    return result[-limit:]


def _normalize_record(item: Any) -> Optional[dict[str, str]]:
    if isinstance(item, str):
        name, _, value = item.partition(":")
        record = {"name": name.strip() or "属性", "value": value.strip() or item.strip(), "category": "", "description": ""}
    elif isinstance(item, dict):
        record = {
            "name": str(item.get("name") or "").strip()[:40],
            "value": str(item.get("value") or "").strip()[:120],
            "category": str(item.get("category") or "").strip()[:40],
            "description": str(item.get("description") or "").strip()[:180],
        }
    else:
        return None
    if not record["name"] or not record["value"]:
        return None
    return record


def _merge_derivatives(existing: list[Any], incoming: list[Any], *, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*existing, *incoming]:
        record = _normalize_derivative(item)
        if not record:
            continue
        key = (record.get("tone") or "", record.get("title") or "", record.get("description") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result[-limit:]


def _normalize_derivative(item: Any) -> Optional[dict[str, Any]]:
    if isinstance(item, str):
        record = {"tone": "", "title": "", "description": item.strip()[:300], "trigger": "", "visibility": "", "future": False}
    elif isinstance(item, dict):
        record = {
            "tone": str(item.get("tone") or "").strip()[:40],
            "title": str(item.get("title") or "").strip()[:60],
            "description": str(item.get("description") or "").strip()[:300],
            "trigger": str(item.get("trigger") or "").strip()[:120],
            "visibility": str(item.get("visibility") or "").strip()[:120],
            "future": bool(item.get("future")),
        }
    else:
        return None
    if not record["description"]:
        return None
    return record


def _merge_limited_strings(existing: list[Any], incoming: list[Any], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value[:160])
    return result[-limit:]


def _snapshot_from_dict(data: dict[str, Any]) -> ChapterFactSnapshot:
    return ChapterFactSnapshot(
        novel_id=str(data.get("novel_id") or ""),
        chapter_number=int(data.get("chapter_number") or 0),
        content_hash=str(data.get("content_hash") or ""),
        summary=str(data.get("summary") or ""),
        characters=[str(item) for item in data.get("characters") or []],
        locations=[str(item) for item in data.get("locations") or []],
        world_events=[str(item) for item in data.get("world_events") or []],
        at=str(data.get("at") or ""),
        schema_version=int(data.get("schema_version") or 1),
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _slug(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "c_empty"
    return "c_" + sha256(text.encode("utf-8")).hexdigest()[:24]


def _character_index_entry(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": str(card.get("character_id") or ""),
        "name": str(card.get("name") or ""),
        "aliases": [str(item) for item in card.get("aliases") or []],
        "first_seen_chapter": _int_or_none(card.get("first_seen_chapter")) or 0,
        "last_seen_chapter": _int_or_none(card.get("last_seen_chapter")) or 0,
        "status": str(card.get("status") or "active"),
        "entity_type": str(card.get("entity_type") or "person"),
        "invalid_reason": str(card.get("invalid_reason") or ""),
        "canonical_character_id": str(card.get("canonical_character_id") or ""),
        "canonical_source": str(card.get("canonical_source") or ""),
        "recent_events": list(card.get("recent_events") or [])[-3:],
    }


def _card_is_mentioned(card: dict[str, Any], text: str) -> bool:
    names = [card.get("name"), *(card.get("aliases") or [])]
    return any(str(name or "").strip() and str(name).strip() in text for name in names)


def _dedupe_strings(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _record_mentions(record: dict[str, Any], text: str) -> bool:
    if not text:
        return False
    terms: list[str] = []
    for key in ("summary", "subject", "rule", "location"):
        value = str(record.get(key) or "").strip()
        if value:
            terms.extend(_split_match_terms(value))
    for key in ("participants", "characters", "locations"):
        for item in record.get(key) or []:
            value = str(item or "").strip()
            if value:
                terms.append(value)
    return any(term and term in text for term in terms)


def _route_conflict_as_constraint(conflict: dict[str, Any]) -> dict[str, Any]:
    subject = str(conflict.get("character") or "")
    current_location = str(conflict.get("current_location") or "")
    previous_location = str(conflict.get("previous_location") or "")
    return {
        "constraint_id": str(conflict.get("conflict_id") or ""),
        "type": "route_conflict",
        "subject": subject,
        "location": current_location or previous_location,
        "rule": str(conflict.get("message") or ""),
        "severity": str(conflict.get("severity") or "warning"),
        "source": "story_graph",
        "chapter_number": conflict.get("chapter_current"),
        "participants": [subject] if subject else [],
        "locations": [item for item in [previous_location, current_location] if item],
    }


def _split_match_terms(value: Any) -> list[str]:
    separators = "，。；、：:（）()【】[]《》 \n\t"
    current = str(value or "")
    for sep in separators:
        current = current.replace(sep, "|")
    terms = [part.strip() for part in current.split("|") if len(part.strip()) >= 2]
    if len(current) >= 4:
        terms.extend(current[index : index + 4] for index in range(0, max(len(current) - 3, 0), 4))
    return list(dict.fromkeys(terms))


def _character_event_summary(name: str, snapshot: ChapterFactSnapshot) -> str:
    for event in snapshot.world_events:
        if name in event:
            return event[:180]
    if snapshot.summary:
        marker = snapshot.summary.find(name)
        if marker >= 0:
            start = max(0, marker - 24)
            end = min(len(snapshot.summary), marker + 120)
            return snapshot.summary[start:end]
    return f"第{snapshot.chapter_number}章出现，地点：{'、'.join(snapshot.locations[:3]) or '未标注'}"
