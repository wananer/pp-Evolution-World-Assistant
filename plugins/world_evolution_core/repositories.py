"""Database-backed repositories for Evolution World plugin state."""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.plugin_storage import PluginStorage

from .models import ChapterFactSnapshot, CharacterCard

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

    def delete_fact_snapshot(self, novel_id: str, chapter_number: int) -> bool:
        return self._delete_scope(["novels", novel_id, "facts", f"chapter_{chapter_number}.json"])

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
        items = []
        rows = self.storage.list_json(
            PLUGIN_NAME,
            ["novels", novel_id, "facts"],
            limit=limit,
            reverse=limit is not None,
            before_chapter=before_chapter,
        )
        for data in rows:
            if not isinstance(data, dict):
                continue
            chapter_number = _int_or_none(data.get("chapter_number"))
            if before_chapter and (not chapter_number or chapter_number >= before_chapter):
                continue
            items.append(data)
        return sorted(items, key=lambda item: int(item.get("chapter_number") or 0))

    def upsert_character_cards(self, novel_id: str, snapshot: ChapterFactSnapshot, character_updates: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        updates_by_name = {item.get("name"): item for item in (character_updates or []) if item.get("name")}
        updated = []
        for name in snapshot.characters:
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
        self.storage.delete_json_prefix(PLUGIN_NAME, ["novels", novel_id, "characters"])
        prepared_cards = [self._prepare_character_card(card) for card in cards if isinstance(card, dict)]
        for card in prepared_cards:
            self.storage.write_json(
                PLUGIN_NAME,
                ["novels", novel_id, "characters", f"{card['character_id']}.json"],
                card,
            )
        self._write_character_index(novel_id, prepared_cards)

    def write_character_card(self, novel_id: str, card: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_character_card(card)
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "characters", f"{prepared['character_id']}.json"],
            prepared,
        )
        self._upsert_character_index_entry(novel_id, prepared)
        return prepared

    def list_character_cards(
        self,
        novel_id: str,
        *,
        limit: Optional[int] = None,
        recent_first: bool = False,
    ) -> dict[str, Any]:
        cards = [
            _ensure_character_defaults(dict(item))
            for item in self.storage.list_json(
                PLUGIN_NAME,
                ["novels", novel_id, "characters"],
                limit=limit,
                reverse=recent_first,
            )
            if isinstance(item, dict)
        ]
        if cards:
            if not recent_first:
                cards = sorted(cards, key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
            return {"items": cards}
        legacy = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters.json"], default={"items": []})
        return legacy if isinstance(legacy, dict) and isinstance(legacy.get("items"), list) else {"items": []}

    def list_character_index(self, novel_id: str) -> dict[str, Any]:
        index = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "characters_index.json"], default={"items": []})
        if isinstance(index, dict) and isinstance(index.get("items"), list):
            return index
        cards = self.list_character_cards(novel_id).get("items", [])
        return {"items": [_character_index_entry(card) for card in cards]}

    def list_relevant_character_cards(self, novel_id: str, text: str = "", *, limit: int = RECENT_CONTEXT_CHARACTER_LIMIT) -> dict[str, Any]:
        by_id: dict[str, dict[str, Any]] = {}
        for card in self.list_character_cards(novel_id, limit=limit, recent_first=True).get("items", []):
            if card.get("character_id"):
                by_id[str(card["character_id"])] = card
        text = str(text or "")
        if text:
            for entry in self.list_character_index(novel_id).get("items", []):
                names = [entry.get("name"), *(entry.get("aliases") or [])]
                if any(str(name or "").strip() and str(name).strip() in text for name in names):
                    card = self.get_character_card(novel_id, str(entry.get("character_id") or entry.get("name") or ""))
                    if card and card.get("character_id"):
                        by_id[str(card["character_id"])] = card
        cards = list(by_id.values())
        return {"items": sorted(cards, key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))}

    def get_character_card(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        try:
            card = self.storage.read_json(
                PLUGIN_NAME,
                ["novels", novel_id, "characters", f"{character_id}.json"],
                default=None,
            )
        except ValueError:
            card = None
        if isinstance(card, dict):
            return _ensure_character_defaults(card)
        for card in self.list_character_cards(novel_id)["items"]:
            if card.get("character_id") == character_id or card.get("name") == character_id:
                return card
        return None

    def append_event(self, novel_id: str, event: dict[str, Any]) -> None:
        self.storage.append_jsonl(PLUGIN_NAME, ["novels", novel_id, "events.jsonl"], event)

    def list_events(self, novel_id: str) -> list[dict[str, Any]]:
        return self.storage.read_jsonl(PLUGIN_NAME, ["novels", novel_id, "events.jsonl"])


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

    def _prepare_character_card(self, card: dict[str, Any]) -> dict[str, Any]:
        prepared = _ensure_character_defaults(dict(card))
        if not prepared.get("character_id"):
            prepared["character_id"] = _slug(str(prepared.get("name") or "character"))
        prepared["character_id"] = _safe_record_id(str(prepared["character_id"]))
        return prepared

    def _write_character_index(self, novel_id: str, cards: list[dict[str, Any]]) -> None:
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "characters_index.json"],
            {"items": [_character_index_entry(card) for card in cards]},
        )

    def _upsert_character_index_entry(self, novel_id: str, card: dict[str, Any]) -> None:
        index = self.list_character_index(novel_id)
        entry = _character_index_entry(card)
        items = [item for item in index.get("items", []) if item.get("character_id") != entry["character_id"]]
        items.append(entry)
        items.sort(key=lambda item: (item.get("first_seen_chapter") or 0, item.get("name") or ""))
        self.storage.write_json(PLUGIN_NAME, ["novels", novel_id, "characters_index.json"], {"items": items})


def _ensure_character_defaults(card: dict[str, Any]) -> dict[str, Any]:
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
    if not isinstance(incoming, dict):
        return
    metaphor = str(incoming.get("metaphor") or "").strip()
    if metaphor:
        current["metaphor"] = metaphor[:240]
    base = str(incoming.get("base") or "").strip()
    if base:
        current["base"] = base[:40]
    current["main_tones"] = _merge_limited_strings(current.get("main_tones") or [], incoming.get("main_tones") or [], limit=8)
    current["accents"] = _merge_limited_strings(current.get("accents") or [], incoming.get("accents") or [], limit=10)
    current["derivatives"] = _merge_derivatives(current.get("derivatives") or [], incoming.get("derivatives") or [], limit=32)


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
    return "c_" + sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_record_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return _slug("character")
    try:
        PluginStorage._safe_segment(text)
        return text
    except ValueError:
        return _slug(text)


def _character_index_entry(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": card.get("character_id") or _slug(str(card.get("name") or "character")),
        "name": card.get("name") or "",
        "aliases": list(card.get("aliases") or [])[:8],
        "first_seen_chapter": card.get("first_seen_chapter"),
        "last_seen_chapter": card.get("last_seen_chapter"),
        "status": card.get("status") or "active",
    }


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
