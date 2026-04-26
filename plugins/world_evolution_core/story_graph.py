"""Compact story graph and route-map derivation for Evolution World."""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional


ARRIVAL_WORDS = ("第一次", "才找到", "终于", "抵达", "来到", "进入", "走进", "推开", "刷开")
BRIDGE_WORDS = ("离开", "前往", "赶往", "转移", "穿过", "走向", "回到", "返回", "沿着", "经过", "爬上", "下到")
LOCATION_ALIASES = {
    "塔顶": "三号塔顶层",
    "顶层水箱": "三号塔顶层水箱",
    "地下机房": "三号塔地下机房",
}


def build_story_graph_chapter(
    *,
    novel_id: str,
    chapter_number: int,
    snapshot: dict[str, Any],
    chapter_summary: dict[str, Any],
    timeline_events: list[dict[str, Any]],
    previous_chapters: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    """Build one chapter's graph delta from already extracted Evolution facts."""
    previous_positions = _latest_character_positions(previous_chapters)
    characters = _strings(snapshot.get("characters"))
    state_locations = _dedupe(
        [
            *_locations_from_state(chapter_summary.get("opening_state")),
            *_locations_from_state(chapter_summary.get("chapter_state")),
            *_locations_from_state(chapter_summary.get("ending_state")),
        ]
    )
    chapter_locations = state_locations or _dedupe(_strings(snapshot.get("locations")))
    opening_locations = _locations_from_state(chapter_summary.get("opening_state")) or chapter_locations[:1]
    ending_locations = _locations_from_state(chapter_summary.get("ending_state")) or chapter_locations[-1:] or opening_locations
    opening_location = _canonical_location(opening_locations[0]) if opening_locations else ""
    ending_location = _canonical_location(ending_locations[-1]) if ending_locations else opening_location
    opening_text = str((chapter_summary.get("opening_state") or {}).get("excerpt") or "")
    ending_text = str((chapter_summary.get("ending_state") or {}).get("excerpt") or "")

    entities = [_entity("character", name, chapter_number) for name in characters]
    locations = [_location_node(name, chapter_number) for name in chapter_locations]
    events = _story_events(novel_id, chapter_number, timeline_events, snapshot, at)
    route_edges: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for character in characters:
        previous = previous_positions.get(character) or {}
        previous_location = str(previous.get("location") or "")
        previous_chapter = _int_or_none(previous.get("chapter_number"))

        if previous_location and opening_location and previous_location != opening_location:
            bridge_conflict = _route_bridge_conflict(
                novel_id=novel_id,
                character=character,
                previous_location=previous_location,
                opening_location=opening_location,
                previous_chapter=previous_chapter,
                chapter_number=chapter_number,
                opening_text=opening_text,
            )
            if bridge_conflict:
                conflicts.append(bridge_conflict)
            route_edges.append(
                _route_edge(
                    novel_id=novel_id,
                    chapter_number=chapter_number,
                    character=character,
                    from_location=previous_location,
                    to_location=opening_location,
                    reason="chapter_bridge",
                    evidence=opening_text,
                    confidence=0.55,
                    time_order_start=(chapter_number * 100) - 1,
                    time_order_end=chapter_number * 100,
                )
            )
        elif previous_location and opening_location == previous_location and _has_arrival_reset(opening_text, opening_location):
            conflicts.append(
                _conflict(
                    novel_id,
                    "repeated_arrival",
                    "hard",
                    character,
                    previous_chapter,
                    chapter_number,
                    previous_location,
                    opening_location,
                    f"{character}上一记录已在{opening_location}，本章开头又写成重新抵达/进入，像状态重置。",
                    opening_text,
                )
            )

        if opening_location and ending_location:
            route_edges.append(
                _route_edge(
                    novel_id=novel_id,
                    chapter_number=chapter_number,
                    character=character,
                    from_location=opening_location,
                    to_location=ending_location,
                    reason="chapter_scene",
                    evidence=_compact_evidence(opening_text, ending_text),
                    confidence=0.72,
                    time_order_start=chapter_number * 100,
                    time_order_end=chapter_number * 100 + 90,
                )
            )

    route_edges = _dedupe_route_edges(route_edges)
    conflicts.extend(_meeting_conflicts(novel_id, chapter_number, route_edges))
    vector_capsules = _vector_capsules(novel_id, chapter_number, snapshot, chapter_summary, route_edges, events)
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "entities": entities,
        "locations": locations,
        "events": events,
        "route_edges": route_edges,
        "conflicts": conflicts,
        "vectors": vector_capsules,
        "compression": {
            "mode": "fact_delta_plus_vector_capsules",
            "stored_text_fields": ["summary", "evidence", "text_compact"],
            "full_chapter_text_stored": False,
        },
        "at": at,
    }


def build_global_route_map(novel_id: str, chapters: list[dict[str, Any]]) -> dict[str, Any]:
    """Return UI-ready route graph data for all stored chapter deltas."""
    route_edges = []
    conflicts = []
    locations_by_name: dict[str, dict[str, Any]] = {}
    character_names: list[str] = []
    vectors = []
    events = []
    for chapter in sorted(chapters, key=lambda item: int(item.get("chapter_number") or 0)):
        for location in chapter.get("locations") or []:
            if not isinstance(location, dict):
                continue
            name = _canonical_location(location.get("name"))
            if not name:
                continue
            locations_by_name.setdefault(name, {**location, "name": name, "location_id": _location_id(name)})
        for edge in chapter.get("route_edges") or []:
            if not isinstance(edge, dict):
                continue
            route_edges.append(edge)
            character = str(edge.get("character") or "")
            if character and character not in character_names:
                character_names.append(character)
            for key in ("from_location", "to_location"):
                name = _canonical_location(edge.get(key))
                if name:
                    locations_by_name.setdefault(name, _location_node(name, int(edge.get("chapter_start") or 0)))
        conflicts.extend(item for item in chapter.get("conflicts") or [] if isinstance(item, dict))
        vectors.extend(item for item in chapter.get("vectors") or [] if isinstance(item, dict))
        events.extend(item for item in chapter.get("events") or [] if isinstance(item, dict))

    locations = _assign_layout(list(locations_by_name.values()))
    meetings = _route_meetings(route_edges)
    worldline = _event_worldline(events)
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "nodes": locations,
        "edges": sorted(route_edges, key=lambda item: (int(item.get("time_order_start") or 0), str(item.get("character") or ""))),
        "characters": [{"name": name, "color": _character_color(index)} for index, name in enumerate(character_names)],
        "meetings": meetings,
        "conflicts": sorted(conflicts, key=lambda item: (int(item.get("chapter_current") or 0), str(item.get("type") or ""))),
        "worldline": worldline,
        "vector_index": {
            "mode": "compact_text_capsules",
            "count": len(vectors),
            "items": vectors[-80:],
        },
        "aggregate": {
            "chapter_count": len(chapters),
            "location_count": len(locations),
            "route_edge_count": len(route_edges),
            "meeting_count": len(meetings),
            "conflict_count": len(conflicts),
            "hard_conflict_count": sum(1 for item in conflicts if item.get("severity") == "hard"),
        },
    }


def _story_events(
    novel_id: str,
    chapter_number: int,
    timeline_events: list[dict[str, Any]],
    snapshot: dict[str, Any],
    at: str,
) -> list[dict[str, Any]]:
    events = []
    source_events = timeline_events or [
        {"summary": summary, "characters": snapshot.get("characters") or [], "locations": snapshot.get("locations") or []}
        for summary in _strings(snapshot.get("world_events"))
    ]
    for index, event in enumerate(source_events, start=1):
        if not isinstance(event, dict):
            continue
        summary = str(event.get("summary") or "").strip()
        if not summary:
            continue
        locations = _dedupe(_strings(event.get("locations")) or _strings(snapshot.get("locations")))
        characters = _dedupe(_strings(event.get("characters")) or _strings(event.get("participants")) or _strings(snapshot.get("characters")))
        events.append(
            {
                "event_id": str(event.get("event_id") or _id("evt", novel_id, chapter_number, index, summary)),
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "event_type": str(event.get("event_type") or "scene"),
                "time_label": str(event.get("time_label") or f"第{chapter_number}章"),
                "time_order": int(event.get("time_order") or chapter_number * 100 + index),
                "location_id": _location_id(locations[0]) if locations else "",
                "locations": locations,
                "participants": characters,
                "summary": summary[:240],
                "evidence_ref": str(event.get("evidence_ref") or f"chapter_{chapter_number}"),
                "confidence": _float_or_default(event.get("confidence"), 0.7),
                "at": at,
            }
        )
    return events


def _latest_character_positions(chapters: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for chapter in sorted(chapters, key=lambda item: int(item.get("chapter_number") or 0)):
        for edge in chapter.get("route_edges") or []:
            if not isinstance(edge, dict):
                continue
            character = str(edge.get("character") or "")
            to_location = _canonical_location(edge.get("to_location"))
            if character and to_location:
                positions[character] = {
                    "location": to_location,
                    "chapter_number": int(edge.get("chapter_end") or edge.get("chapter_start") or chapter.get("chapter_number") or 0),
                    "time_order": int(edge.get("time_order_end") or 0),
                }
    return positions


def _route_bridge_conflict(
    *,
    novel_id: str,
    character: str,
    previous_location: str,
    opening_location: str,
    previous_chapter: Optional[int],
    chapter_number: int,
    opening_text: str,
) -> Optional[dict[str, Any]]:
    if not previous_chapter or chapter_number <= previous_chapter:
        return None
    if any(word in opening_text for word in BRIDGE_WORDS):
        return None
    return _conflict(
        novel_id,
        "location_jump_without_bridge",
        "warning",
        character,
        previous_chapter,
        chapter_number,
        previous_location,
        opening_location,
        f"{character}上一记录在{previous_location}，本章开头已在{opening_location}，缺少转场/移动桥段。",
        opening_text,
    )


def _meeting_conflicts(novel_id: str, chapter_number: int, route_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_character: dict[str, set[str]] = {}
    for edge in route_edges:
        character = str(edge.get("character") or "")
        locs = by_character.setdefault(character, set())
        locs.add(str(edge.get("to_location") or ""))
    for character, locs in by_character.items():
        concrete = {loc for loc in locs if loc}
        if len(concrete) > 2:
            conflicts.append(
                _conflict(
                    novel_id,
                    "multi_location_same_chapter",
                    "warning",
                    character,
                    chapter_number,
                    chapter_number,
                    "",
                    "、".join(sorted(concrete)),
                    f"{character}在同一章被记录到多个地点，请确认是否有明确移动链。",
                    "",
                )
            )
    return conflicts


def _route_meetings(route_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], list[str]] = {}
    for edge in route_edges:
        chapter = int(edge.get("chapter_start") or 0)
        location = str(edge.get("to_location") or "")
        character = str(edge.get("character") or "")
        if chapter and location and character:
            groups.setdefault((chapter, location), [])
            if character not in groups[(chapter, location)]:
                groups[(chapter, location)].append(character)
    meetings = []
    for (chapter, location), characters in groups.items():
        if len(characters) < 2:
            continue
        meetings.append(
            {
                "meeting_id": _id("meet", chapter, location, ",".join(sorted(characters))),
                "chapter_number": chapter,
                "location": location,
                "location_id": _location_id(location),
                "characters": sorted(characters),
            }
        )
    return sorted(meetings, key=lambda item: (item["chapter_number"], item["location"]))


def _event_worldline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": str(event.get("event_id") or ""),
            "chapter_number": int(event.get("chapter_number") or 0),
            "time_label": str(event.get("time_label") or ""),
            "summary": str(event.get("summary") or ""),
            "participants": _strings(event.get("participants")),
            "locations": _strings(event.get("locations")),
        }
        for event in sorted(events, key=lambda item: (int(item.get("time_order") or 0), str(item.get("event_id") or "")))[-120:]
    ]


def _vector_capsules(
    novel_id: str,
    chapter_number: int,
    snapshot: dict[str, Any],
    chapter_summary: dict[str, Any],
    route_edges: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    capsules: list[dict[str, Any]] = []
    summary = str(chapter_summary.get("short_summary") or snapshot.get("summary") or "").strip()
    if summary:
        capsules.append(_capsule(novel_id, "chapter_summary", chapter_number, f"第{chapter_number}章：{summary}", ["summary", "chapter"]))
    for event in events[:8]:
        text = f"{event.get('time_label')}｜{event.get('summary')}｜地点:{'、'.join(_strings(event.get('locations')))}｜人物:{'、'.join(_strings(event.get('participants')))}"
        capsules.append(_capsule(novel_id, "world_event", chapter_number, text, ["event", str(event.get("event_type") or "scene")]))
    for edge in route_edges[:12]:
        text = f"第{chapter_number}章路线｜{edge.get('character')}：{edge.get('from_location')} -> {edge.get('to_location')}｜{edge.get('reason')}"
        capsules.append(_capsule(novel_id, "route_edge", chapter_number, text, ["route", str(edge.get("character") or "")]))
    return capsules


def _capsule(novel_id: str, source_type: str, chapter_number: int, text: str, tags: list[str]) -> dict[str, Any]:
    compact = " ".join(str(text or "").split())[:320]
    return {
        "vector_id": _id("vec", novel_id, source_type, chapter_number, compact),
        "novel_id": novel_id,
        "source_type": source_type,
        "source_id": f"chapter_{chapter_number}",
        "chapter_number": chapter_number,
        "text_compact": compact,
        "tags": [tag for tag in tags if tag],
        "embedding": None,
        "embedding_status": "pending_optional_embedding",
    }


def _route_edge(
    *,
    novel_id: str,
    chapter_number: int,
    character: str,
    from_location: str,
    to_location: str,
    reason: str,
    evidence: str,
    confidence: float,
    time_order_start: int,
    time_order_end: int,
) -> dict[str, Any]:
    from_location = _canonical_location(from_location)
    to_location = _canonical_location(to_location)
    return {
        "edge_id": _id("route", novel_id, chapter_number, character, from_location, to_location, reason, time_order_start),
        "novel_id": novel_id,
        "character": character,
        "character_id": _entity_id("character", character),
        "from_location": from_location,
        "from_location_id": _location_id(from_location),
        "to_location": to_location,
        "to_location_id": _location_id(to_location),
        "chapter_start": chapter_number,
        "chapter_end": chapter_number,
        "time_order_start": time_order_start,
        "time_order_end": time_order_end,
        "reason": reason,
        "transport": "unspecified",
        "evidence_ref": _compact_evidence(evidence),
        "confidence": confidence,
    }


def _conflict(
    novel_id: str,
    conflict_type: str,
    severity: str,
    character: str,
    chapter_previous: Optional[int],
    chapter_current: int,
    previous_location: str,
    current_location: str,
    message: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "conflict_id": _id("conflict", novel_id, conflict_type, character, chapter_previous or 0, chapter_current, previous_location, current_location),
        "type": conflict_type,
        "severity": severity,
        "character": character,
        "chapter_previous": chapter_previous,
        "chapter_current": chapter_current,
        "previous_location": previous_location,
        "current_location": current_location,
        "message": message,
        "evidence": _compact_evidence(evidence),
    }


def _entity(entity_type: str, name: str, chapter_number: int) -> dict[str, Any]:
    return {
        "entity_id": _entity_id(entity_type, name),
        "type": entity_type,
        "name": name,
        "aliases": [],
        "first_seen_chapter": chapter_number,
    }


def _location_node(name: Any, chapter_number: int) -> dict[str, Any]:
    canonical = _canonical_location(name)
    return {
        "location_id": _location_id(canonical),
        "name": canonical,
        "parent": _infer_parent_location(canonical),
        "map_layer": _infer_map_layer(canonical),
        "x": None,
        "y": None,
        "z": _infer_z(canonical),
        "first_seen_chapter": chapter_number,
    }


def _assign_layout(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen: set[str] = set()
    for location in sorted(locations, key=lambda item: str(item.get("name") or "")):
        name = str(location.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(dict(location))
    count = max(len(unique), 1)
    for index, location in enumerate(unique):
        if location.get("x") is None:
            column = index % 4
            row = index // 4
            location["x"] = round(0.12 + column * 0.25, 3)
            location["y"] = round(0.16 + row * min(0.18, 0.7 / max((count + 3) // 4, 1)), 3)
    return unique


def _locations_from_state(state: Any) -> list[str]:
    if not isinstance(state, dict):
        return []
    return [_canonical_location(item) for item in _strings(state.get("locations")) if _canonical_location(item)]


def _canonical_location(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return LOCATION_ALIASES.get(text, text)


def _infer_parent_location(location: str) -> str:
    for marker in ("三号塔", "二号塔", "C区", "宿舍区", "雾港学院"):
        if marker in location and location != marker:
            return marker
    return ""


def _infer_map_layer(location: str) -> str:
    if any(token in location for token in ("地下", "机房", "地下二层")):
        return "underground"
    if any(token in location for token in ("顶层", "塔顶", "水箱")):
        return "upper"
    return "ground"


def _infer_z(location: str) -> int:
    if _infer_map_layer(location) == "underground":
        return -1
    if _infer_map_layer(location) == "upper":
        return 1
    return 0


def _has_arrival_reset(text: str, location: str) -> bool:
    return bool(location and location in text and any(word in text for word in ARRIVAL_WORDS))


def _dedupe_route_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[tuple[str, str, str, int, str]] = set()
    for edge in edges:
        key = (
            str(edge.get("character") or ""),
            str(edge.get("from_location") or ""),
            str(edge.get("to_location") or ""),
            int(edge.get("time_order_start") or 0),
            str(edge.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return result


def _compact_evidence(*values: Any) -> str:
    text = " ".join(str(value or "").strip() for value in values if str(value or "").strip())
    return " ".join(text.split())[:240]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen: set[str] = set()
    for value in values:
        text = _canonical_location(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _location_id(name: str) -> str:
    return _id("loc", _canonical_location(name))


def _entity_id(entity_type: str, name: str) -> str:
    return _id(entity_type[:4] or "ent", name)


def _id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}_{sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _character_color(index: int) -> str:
    palette = ["#2563eb", "#dc2626", "#059669", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4f46e5"]
    return palette[index % len(palette)]
