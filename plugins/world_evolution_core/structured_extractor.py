"""Structured extraction contract and fallback pipeline for Evolution World."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from domain.ai.services.llm_service import LLMService
else:
    LLMService = Any

try:
    from infrastructure.ai.prompt_resolver import resolve_prompt
except Exception:
    def resolve_prompt(_node_key: str, _variables: dict[str, Any], *, fallback_system: str = "", fallback_user: str = "") -> Any:
        class PromptResolutionFallback:
            system = fallback_system
            user = fallback_user

            def to_prompt(self) -> Any:
                return self

        return PromptResolutionFallback()

from .extractor import extract_chapter_facts
from .models import ChapterFactSnapshot


@dataclass
class StructuredCharacterUpdate:
    name: str
    summary: str = ""
    status: str = "active"
    aliases: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    appearance: dict[str, Any] = field(default_factory=dict)
    attributes: list[dict[str, Any]] = field(default_factory=list)
    world_profile: dict[str, Any] = field(default_factory=dict)
    personality_palette: dict[str, Any] = field(default_factory=dict)
    known_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    misbeliefs: list[str] = field(default_factory=list)
    emotion: str = ""
    inner_change: str = ""
    growth_stage: str = ""
    growth_change: str = ""
    capability_limits: list[str] = field(default_factory=list)
    decision_biases: list[str] = field(default_factory=list)
    confidence: float = 0.7
    confidence_explicit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredWorldEvent:
    summary: str
    event_type: str = "scene"
    characters: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    misbeliefs: list[str] = field(default_factory=list)
    emotion: str = ""
    inner_change: str = ""
    growth_stage: str = ""
    growth_change: str = ""
    capability_limits: list[str] = field(default_factory=list)
    decision_biases: list[str] = field(default_factory=list)
    confidence: float = 0.7
    confidence_explicit: bool = False

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


class LLMStructuredExtractorProvider:
    """LLM-backed provider for rich Evolution character/world extraction."""

    def __init__(self, llm_service: LLMService | None = None, *, max_tokens: int = 2200) -> None:
        self.llm_service = llm_service
        self.max_tokens = max_tokens

    async def extract(self, request: dict[str, Any]) -> dict[str, Any]:
        from domain.ai.services.llm_service import GenerationConfig

        llm = self.llm_service or _create_active_llm_service()
        prompt = _build_structured_extraction_prompt(request)
        result = await llm.generate(
            prompt,
            GenerationConfig(
                max_tokens=self.max_tokens,
                temperature=0.1,
                response_format={"type": "json_object"},
            ),
        )
        data, errors = _parse_llm_json(result.content)
        if data is None:
            raise ValueError("structured extraction JSON parse failed: " + "; ".join(errors[:4]))
        return data


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
                    "appearance": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "features": {"type": "array", "items": {"type": "string"}},
                            "style": {"type": "array", "items": {"type": "string"}},
                            "current_outfit": {"type": "string"},
                            "marks": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "value"],
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "category": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "world_profile": {
                        "type": "object",
                        "properties": {
                            "schema_name": {"type": "string"},
                            "fields": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["name", "value"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "value": {"type": "string"},
                                        "category": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "personality_palette": {
                        "type": "object",
                        "properties": {
                            "metaphor": {"type": "string"},
                            "base": {"type": "string"},
                            "main_tones": {"type": "array", "items": {"type": "string"}},
                            "accents": {"type": "array", "items": {"type": "string"}},
                            "pressure_triggers": {"type": "array", "items": {"type": "string"}},
                            "relationship_tones": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "target": {"type": "string"},
                                        "tone": {"type": "string"},
                                        "behavior": {"type": "string"},
                                    },
                                },
                            },
                            "voice_signature": {"type": "array", "items": {"type": "string"}},
                            "gesture_signature": {"type": "array", "items": {"type": "string"}},
                            "negative_costs": {"type": "array", "items": {"type": "string"}},
                            "presence_mode": {
                                "type": "string",
                                "enum": ["active_scene", "remote", "memory_trace", "record_only", "system_entity"],
                            },
                            "derivatives": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["tone", "description"],
                                    "properties": {
                                        "tone": {"type": "string"},
                                        "title": {"type": "string"},
                                        "description": {"type": "string"},
                                        "trigger": {"type": "string"},
                                        "visibility": {"type": "string"},
                                        "future": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                    "known_facts": {"type": "array", "items": {"type": "string"}},
                    "unknowns": {"type": "array", "items": {"type": "string"}},
                    "misbeliefs": {"type": "array", "items": {"type": "string"}},
                    "emotion": {"type": "string"},
                    "inner_change": {"type": "string"},
                    "growth_stage": {"type": "string"},
                    "growth_change": {"type": "string"},
                    "capability_limits": {"type": "array", "items": {"type": "string"}},
                    "decision_biases": {"type": "array", "items": {"type": "string"}},
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
                    "known_facts": {"type": "array", "items": {"type": "string"}},
                    "unknowns": {"type": "array", "items": {"type": "string"}},
                    "misbeliefs": {"type": "array", "items": {"type": "string"}},
                    "emotion": {"type": "string"},
                    "inner_change": {"type": "string"},
                    "growth_stage": {"type": "string"},
                    "growth_change": {"type": "string"},
                    "capability_limits": {"type": "array", "items": {"type": "string"}},
                    "decision_biases": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}


def _create_active_llm_service() -> LLMService:
    from infrastructure.ai.provider_factory import LLMProviderFactory

    return LLMProviderFactory().create_active_provider()


def _build_structured_extraction_prompt(request: dict[str, Any]) -> Any:
    schema = request.get("schema") or STRUCTURED_EXTRACTION_SCHEMA
    chapter_number = request.get("chapter_number")
    content = str(request.get("content") or "")
    system = (
        "你是 PlotPilot Evolution 的结构化事实抽取器。"
        "你只输出 JSON 对象，不写解释、不写 Markdown。"
        "必须从正文中抽取明确事实，禁止把书名、地点、物件、形容词短语、章节名误当人物。"
    )
    user = f"""请阅读第 {chapter_number} 章正文，输出符合 schema 的 JSON。

硬规则：
1. characters 只包含正文中明确出场或被明确提及的人物。
2. 每个人物必须尽量补全 appearance、attributes、world_profile、personality_palette。
3. 性格调色盘不是标签列表，而是行为模型：
   - base 是底色，表示深层驱动力。
   - main_tones 是主色调，表示高频可见行为倾向。
   - accents 是点缀，表示在特定关系/压力下显露的次级特质。
   - derivatives 写具体衍生行为：触发条件、可见表现、限制、反面代价。
4. 如果正文证据不足，可以给出“暂未定型/待观察”的保守调色盘，但不能留空。
5. known_facts/unknowns/misbeliefs 必须符合角色视角，不允许让角色知道未在场信息。
6. world_events 只记录本章发生的事实，不要总结未来。

JSON schema:
{json.dumps(schema, ensure_ascii=False)}

正文：
{content[:12000]}
"""
    return resolve_prompt(
        "plugin.world_evolution_core.structured-extraction",
        {
            "chapter_number": chapter_number,
            "schema": json.dumps(schema, ensure_ascii=False),
            "content": content[:12000],
        },
        fallback_system=system,
        fallback_user=user,
    ).to_prompt()


def _parse_llm_json(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    cleaned = _sanitize_llm_json(raw)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data, []
        return None, [f"root is {type(data).__name__}, expected object"]
    except json.JSONDecodeError as exc:
        errors = [str(exc)]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data, []
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
    return None, errors


def _sanitize_llm_json(raw: str) -> str:
    text = str(raw or "").strip().lstrip("\ufeff")
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text).strip()


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
        "instruction": (
            "Extract only facts explicitly present in the chapter. Track each character's appearance, open-ended attributes, "
            "world-specific profile fields, cognition, emotion, growth, capability limits, and personality palette. "
            "For personality_palette, model people as colors: base is the underlying color, main_tones are dominant colors, "
            "accents are smaller visible traits, and derivatives explain concrete behavior patterns caused by each color. "
            "Also extract generic writing-useful fields when explicit evidence exists: pressure_triggers, relationship_tones, "
            "voice_signature, gesture_signature, negative_costs, and presence_mode. "
            "Do not infer hidden motives, omniscient knowledge, or future events unless the text explicitly frames a future tendency."
        ),
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
            StructuredCharacterUpdate(
                name=name,
                summary=_summary_for_name(name, snapshot),
                locations=snapshot.locations[:5],
                appearance=_default_appearance(),
                attributes=_default_attributes(),
                world_profile=_default_world_profile(),
                personality_palette=_default_personality_palette(),
                known_facts=[_summary_for_name(name, snapshot)] if _summary_for_name(name, snapshot) else [],
                unknowns=["未明确知道其他角色未在场经历"],
                capability_limits=["只能依据已见、已听、已推理的信息行动"],
                decision_biases=["会受当前目标和情绪影响，不应表现为全知全能"],
            )
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
        appearance=_parse_appearance(value.get("appearance")),
        attributes=_parse_records(value.get("attributes"))[:18],
        world_profile=_parse_world_profile(value.get("world_profile")),
        personality_palette=_parse_personality_palette(value.get("personality_palette")),
        known_facts=_strings(value.get("known_facts"))[:12],
        unknowns=_strings(value.get("unknowns"))[:12],
        misbeliefs=_strings(value.get("misbeliefs"))[:8],
        emotion=str(value.get("emotion") or "").strip()[:80],
        inner_change=str(value.get("inner_change") or "").strip()[:180],
        growth_stage=str(value.get("growth_stage") or "").strip()[:80],
        growth_change=str(value.get("growth_change") or "").strip()[:180],
        capability_limits=_strings(value.get("capability_limits"))[:10],
        decision_biases=_strings(value.get("decision_biases"))[:8],
        confidence=_confidence(value.get("confidence")),
        confidence_explicit="confidence" in value,
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
        known_facts=_strings(value.get("known_facts"))[:12],
        unknowns=_strings(value.get("unknowns"))[:12],
        misbeliefs=_strings(value.get("misbeliefs"))[:8],
        emotion=str(value.get("emotion") or "").strip()[:80],
        inner_change=str(value.get("inner_change") or "").strip()[:180],
        growth_stage=str(value.get("growth_stage") or "").strip()[:80],
        growth_change=str(value.get("growth_change") or "").strip()[:180],
        capability_limits=_strings(value.get("capability_limits"))[:10],
        decision_biases=_strings(value.get("decision_biases"))[:8],
        confidence=_confidence(value.get("confidence")),
        confidence_explicit="confidence" in value,
    )


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe(str(item).strip() for item in value if str(item).strip())


def _parse_appearance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "summary": str(value.get("summary") or "").strip()[:240],
        "features": _strings(value.get("features"))[:10],
        "style": _strings(value.get("style"))[:10],
        "current_outfit": str(value.get("current_outfit") or "").strip()[:160],
        "marks": _strings(value.get("marks"))[:10],
    }


def _parse_records(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, str):
            name, _, raw_value = item.partition(":")
            record = {"name": name.strip() or "属性", "value": raw_value.strip() or item.strip(), "category": "", "description": ""}
        elif isinstance(item, dict):
            record = {
                "name": str(item.get("name") or "").strip()[:40],
                "value": str(item.get("value") or "").strip()[:120],
                "category": str(item.get("category") or "").strip()[:40],
                "description": str(item.get("description") or "").strip()[:180],
            }
        else:
            continue
        if not record["name"] or not record["value"]:
            continue
        key = (record["category"], record["name"])
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _parse_world_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "schema_name": str(value.get("schema_name") or "").strip()[:80],
        "fields": _parse_records(value.get("fields"))[:18],
    }


def _parse_personality_palette(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    palette = {
        "metaphor": str(value.get("metaphor") or "").strip()[:240],
        "base": str(value.get("base") or "").strip()[:40],
        "main_tones": _strings(value.get("main_tones"))[:6],
        "accents": _strings(value.get("accents"))[:8],
        "derivatives": _parse_palette_derivatives(value.get("derivatives"))[:24],
        "pressure_triggers": _strings(value.get("pressure_triggers"))[:8],
        "relationship_tones": _parse_relationship_tones(value.get("relationship_tones"))[:12],
        "voice_signature": _strings(value.get("voice_signature"))[:6],
        "gesture_signature": _strings(value.get("gesture_signature"))[:6],
        "negative_costs": _strings(value.get("negative_costs"))[:8],
    }
    presence_mode = str(value.get("presence_mode") or "").strip()
    if presence_mode in {"active_scene", "remote", "memory_trace", "record_only", "system_entity"}:
        palette["presence_mode"] = presence_mode
    if (
        palette["base"]
        or palette["main_tones"]
        or palette["accents"]
        or palette["derivatives"]
        or palette["pressure_triggers"]
        or palette["relationship_tones"]
        or palette["voice_signature"]
        or palette["gesture_signature"]
        or palette["negative_costs"]
    ):
        palette["source"] = "structured_extraction"
    return palette


def _parse_relationship_tones(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if isinstance(item, str):
            record = {"target": "相关对象", "tone": item.strip()[:80], "behavior": ""}
        elif isinstance(item, dict):
            record = {
                "target": str(item.get("target") or item.get("object") or "相关对象").strip()[:80],
                "tone": str(item.get("tone") or "").strip()[:80],
                "behavior": str(item.get("behavior") or item.get("description") or "").strip()[:160],
            }
        else:
            continue
        if not record["tone"] and not record["behavior"]:
            continue
        key = (record["target"], record["tone"], record["behavior"])
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _parse_palette_derivatives(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if isinstance(item, str):
            record = {"tone": "", "title": "", "description": item.strip()[:260], "trigger": "", "visibility": "", "future": False}
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
            continue
        if not record["description"]:
            continue
        key = (record["tone"], record["title"], record["description"])
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _default_appearance() -> dict[str, Any]:
    return {"summary": "待从正文补充外貌描写", "features": [], "style": [], "current_outfit": "", "marks": []}


def _default_attributes() -> list[dict[str, str]]:
    return [{"name": "状态", "value": "active", "category": "基础", "description": "默认角色状态；可由具体世界观替换为修为、职业、阵营、能力值等。"}]


def _default_world_profile() -> dict[str, Any]:
    return {"schema_name": "通用角色档案", "fields": []}


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
