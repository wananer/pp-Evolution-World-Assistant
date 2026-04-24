"""Structured extraction contract and fallback pipeline for Evolution World."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .extractor import extract_chapter_facts
from .models import ChapterFactSnapshot


@dataclass
class StructuredCharacterUpdate:
    name: str
    summary: str = ""
    status: str = "active"
    aliases: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    confidence: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredWorldEvent:
    summary: str
    event_type: str = "scene"
    characters: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    confidence: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredExtractionResult:
    snapshot: ChapterFactSnapshot
    character_updates: list[StructuredCharacterUpdate] = field(default_factory=list)
    world_events: list[StructuredWorldEvent] = field(default_factory=list)
    source: str = "deterministic"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "character_updates": [item.to_dict() for item in self.character_updates],
            "world_events": [item.to_dict() for item in self.world_events],
            "source": self.source,
            "warnings": self.warnings,
        }


class StructuredExtractorProvider(Protocol):
    async def extract(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON-like response matching ``STRUCTURED_EXTRACTION_SCHEMA``."""


STRUCTURED_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "characters", "locations", "world_events"],
    "properties": {
        "summary": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "summary"],
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
            },
        },
        "locations": {"type": "array", "items": {"type": "string"}},
        "world_events": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {"type": "string"},
                    "event_type": {"type": "string"},
                    "characters": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}


async def extract_structured_chapter_facts(
    novel_id: str,
    chapter_number: int,
    content_hash: str,
    content: str,
    at: str,
    provider: StructuredExtractorProvider | None = None,
) -> StructuredExtractionResult:
    fallback = _fallback_result(novel_id, chapter_number, content_hash, content, at)
    if provider is None:
        return fallback

    request = {
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "content": content,
        "schema": STRUCTURED_EXTRACTION_SCHEMA,
        "instruction": "Extract only facts explicitly present in the chapter. Do not infer hidden motives or future events.",
    }
    try:
        raw = await provider.extract(request)
        return _parse_structured_result(novel_id, chapter_number, content_hash, at, raw, fallback)
    except Exception as exc:  # provider failures must not block chapter commits
        fallback.warnings.append(f"structured_provider_failed: {exc}")
        return fallback


def _fallback_result(novel_id: str, chapter_number: int, content_hash: str, content: str, at: str) -> StructuredExtractionResult:
    snapshot = extract_chapter_facts(novel_id, chapter_number, content_hash, content, at)
    return StructuredExtractionResult(
        snapshot=snapshot,
        character_updates=[
            StructuredCharacterUpdate(name=name, summary=_summary_for_name(name, snapshot), locations=snapshot.locations[:5])
            for name in snapshot.characters
        ],
        world_events=[StructuredWorldEvent(summary=event, characters=snapshot.characters, locations=snapshot.locations[:5]) for event in snapshot.world_events],
        source="deterministic",
    )


def _parse_structured_result(
    novel_id: str,
    chapter_number: int,
    content_hash: str,
    at: str,
    raw: dict[str, Any],
    fallback: StructuredExtractionResult,
) -> StructuredExtractionResult:
    if not isinstance(raw, dict):
        fallback.warnings.append("structured_provider_returned_non_object")
        return fallback

    characters = [_parse_character(item) for item in raw.get("characters") or []]
    characters = [item for item in characters if item is not None]
    events = [_parse_event(item) for item in raw.get("world_events") or []]
    events = [item for item in events if item is not None]
    locations = _strings(raw.get("locations")) or fallback.snapshot.locations
    summary = str(raw.get("summary") or fallback.snapshot.summary).strip()[:500]
    character_names = _dedupe([item.name for item in characters] or fallback.snapshot.characters)
    event_summaries = _dedupe([item.summary for item in events] or fallback.snapshot.world_events)

    if not summary or not character_names and not event_summaries:
        fallback.warnings.append("structured_provider_missing_required_facts")
        return fallback

    snapshot = ChapterFactSnapshot(
        novel_id=novel_id,
        chapter_number=chapter_number,
        content_hash=content_hash,
        summary=summary,
        characters=character_names,
        locations=locations,
        world_events=event_summaries,
        at=at,
    )
    return StructuredExtractionResult(
        snapshot=snapshot,
        character_updates=characters or fallback.character_updates,
        world_events=events or fallback.world_events,
        source="structured",
    )


def _parse_character(value: Any) -> StructuredCharacterUpdate | None:
    if isinstance(value, str):
        name = value.strip()
        return StructuredCharacterUpdate(name=name) if name else None
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    return StructuredCharacterUpdate(
        name=name,
        summary=str(value.get("summary") or "").strip()[:240],
        status=str(value.get("status") or "active").strip()[:32] or "active",
        aliases=_strings(value.get("aliases"))[:8],
        locations=_strings(value.get("locations"))[:8],
        confidence=_confidence(value.get("confidence")),
    )


def _parse_event(value: Any) -> StructuredWorldEvent | None:
    if isinstance(value, str):
        summary = value.strip()
        return StructuredWorldEvent(summary=summary) if summary else None
    if not isinstance(value, dict):
        return None
    summary = str(value.get("summary") or "").strip()[:240]
    if not summary:
        return None
    return StructuredWorldEvent(
        summary=summary,
        event_type=str(value.get("event_type") or "scene").strip()[:32] or "scene",
        characters=_strings(value.get("characters"))[:12],
        locations=_strings(value.get("locations"))[:8],
        confidence=_confidence(value.get("confidence")),
    )


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe(str(item).strip() for item in value if str(item).strip())


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, number))


def _summary_for_name(name: str, snapshot: ChapterFactSnapshot) -> str:
    for event in snapshot.world_events:
        if name in event:
            return event
    return snapshot.summary[:180]
