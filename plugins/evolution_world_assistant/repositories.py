"""Sidecar repositories for Evolution World plugin state."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union, Tuple

from plugins.platform.plugin_storage import PluginStorage

from .models import ChapterFactSnapshot, CharacterCard

PLUGIN_NAME = "evolution_world_assistant"


class EvolutionWorldRepository:
    def __init__(self, storage: Optional[PluginStorage] = None) -> None:
        self.storage = storage or PluginStorage()

    def save_fact_snapshot(self, snapshot: ChapterFactSnapshot) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", snapshot.novel_id, "facts", f"chapter_{snapshot.chapter_number}.json"],
            snapshot.to_dict(),
        )

    def delete_fact_snapshot(self, novel_id: str, chapter_number: int) -> bool:
        return self._delete_scope(["novels", novel_id, "facts", f"chapter_{chapter_number}.json"])

    def get_fact_snapshot(self, novel_id: str, chapter_number: int) -> dict[str, Any] | None:
        data = self.storage.read_json(
            PLUGIN_NAME,
            ["novels", novel_id, "facts", f"chapter_{chapter_number}.json"],
            default=None,
        )
        return data if isinstance(data, dict) else None

    def list_fact_snapshots(self, novel_id: str, before_chapter: Optional[int] = None) -> list[dict[str, Any]]:
        facts_root = self.storage.root / PLUGIN_NAME / "novels" / novel_id / "facts"
        if not facts_root.exists():
            return []
        items = []
        for path in sorted(facts_root.glob("chapter_*.json"), key=_chapter_sort_key):
            data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "facts", path.name], default={})
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and chapter_number and chapter_number >= before_chapter:
                continue
            items.append(data)
        return items

    def upsert_character_cards(self, novel_id: str, snapshot: ChapterFactSnapshot) -> list[dict[str, Any]]:
        cards = self.list_character_cards(novel_id)["items"]
        by_name = {card.get("name"): card for card in cards}
        updated = []
        for name in snapshot.characters:
            current = by_name.get(name) or CharacterCard(
                character_id=_slug(name),
                name=name,
                first_seen_chapter=snapshot.chapter_number,
                last_seen_chapter=snapshot.chapter_number,
            ).to_dict()
            current["last_seen_chapter"] = max(int(current.get("last_seen_chapter") or 0), snapshot.chapter_number)
            current.setdefault("recent_events", [])
            event_summary = _character_event_summary(name, snapshot)
            if event_summary:
                current["recent_events"].append(
                    {
                        "chapter_number": snapshot.chapter_number,
                        "summary": event_summary,
                        "locations": snapshot.locations[:5],
                    }
                )
                current["recent_events"] = current["recent_events"][-8:]
            by_name[name] = current
            updated.append(current)
        next_cards = sorted(by_name.values(), key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
        self.write_character_cards(novel_id, next_cards)
        return updated

    def rebuild_character_cards_from_facts(self, novel_id: str) -> list[dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for fact in self.list_fact_snapshots(novel_id):
            snapshot = _snapshot_from_dict(fact)
            for name in snapshot.characters:
                current = by_name.get(name) or CharacterCard(
                    character_id=_slug(name),
                    name=name,
                    first_seen_chapter=snapshot.chapter_number,
                    last_seen_chapter=snapshot.chapter_number,
                ).to_dict()
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
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], {"items": cards})

    def list_character_cards(self, novel_id: str) -> dict[str, Any]:
        return self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], default={"items": []})

    def get_character_card(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        for card in self.list_character_cards(novel_id)["items"]:
            if card.get("character_id") == character_id or card.get("name") == character_id:
                return card
        return None

    def append_event(self, novel_id: str, event: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "events.jsonl"], event)

    def list_events(self, novel_id: str) -> list[dict[str, Any]]:
        path = self.storage.root / PLUGIN_NAME / "novels" / novel_id / "events.jsonl"
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                import json

                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items


    def save_imported_flows(self, novel_id: str, converted: dict[str, Any]) -> None:
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "imported_flows.json"], converted)

    def list_imported_flows(self, novel_id: str) -> dict[str, Any]:
        return self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "imported_flows.json"], default={"schema_version": 1, "flows": [], "unsupported": []})

    def append_workflow_run(self, novel_id: str, run: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "runs.jsonl"], run)

    def list_workflow_runs(self, novel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        path = self.storage.root / PLUGIN_NAME / "novels" / novel_id / "runs.jsonl"
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                import json

                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items[-limit:]

    def _delete_scope(self, scope: list[str]) -> bool:
        try:
            path = self.storage._path(PLUGIN_NAME, scope)
        except ValueError:
            return False
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        return True


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


def _chapter_sort_key(path: Path):
    return _int_or_none(path.stem.replace("chapter_", "")) or 0


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _slug(value: str) -> str:
    return "c_" + str(abs(hash(value)))


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
