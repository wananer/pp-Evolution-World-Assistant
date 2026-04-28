"""Canonical character source adapter for Evolution World.

Evolution stores its own writable state in PluginStorage, but character names
should be grounded by PlotPilot's native Bible/Cast/Knowledge sources when they
exist. This module keeps that access read-only through the plugin platform
database facade.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from plugins.platform.host_database import ReadOnlyHostDatabase


@dataclass
class CanonicalCharacter:
    character_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    source: str = "host"
    confidence: float = 0.95

    def names_for_match(self) -> list[str]:
        return _dedupe([self.name, *self.aliases])

    def to_update(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "canonical_character_id": self.character_id,
            "canonical_source": self.source,
            "profile_source": self.source,
            "aliases": self.aliases,
            "summary": self.description,
            "confidence": self.confidence,
        }


@dataclass
class CharacterCalibrationResult:
    characters: list[str]
    character_updates: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    ignored_candidates: list[str] = field(default_factory=list)
    canonical_count: int = 0


def load_canonical_characters(host_database: ReadOnlyHostDatabase | None, novel_id: str) -> list[CanonicalCharacter]:
    if host_database is None or not novel_id:
        return []

    by_name: dict[str, CanonicalCharacter] = {}
    for item in _load_bible_characters(host_database, novel_id):
        _merge_character(by_name, item)
    for item in _load_cast_characters(host_database, novel_id):
        _merge_character(by_name, item)
    for item in _load_triple_characters(host_database, novel_id):
        _merge_character(by_name, item)

    return sorted(by_name.values(), key=lambda item: (item.source != "bible", item.name))


def calibrate_extracted_characters(
    *,
    content: str,
    snapshot_characters: list[str],
    character_updates: list[dict[str, Any]],
    canonical_characters: list[CanonicalCharacter],
) -> CharacterCalibrationResult:
    if not canonical_characters:
        return CharacterCalibrationResult(
            characters=_dedupe(snapshot_characters),
            character_updates=character_updates,
        )

    canonical_by_term: dict[str, CanonicalCharacter] = {}
    for character in canonical_characters:
        for term in character.names_for_match():
            canonical_by_term[term] = character

    mentioned: list[CanonicalCharacter] = []
    for character in canonical_characters:
        if any(term and term in content for term in character.names_for_match()):
            mentioned.append(character)

    normalized_names: list[str] = []
    ignored_candidates: list[str] = []
    for raw_name in snapshot_characters:
        name = str(raw_name or "").strip()
        if not name:
            continue
        match = canonical_by_term.get(name)
        if match is not None:
            normalized_names.append(match.name)
        else:
            ignored_candidates.append(name)

    normalized_names.extend(character.name for character in mentioned)
    final_names = _dedupe(normalized_names)

    updates_by_name: dict[str, dict[str, Any]] = {}
    for update in character_updates:
        raw_name = str(update.get("name") or "").strip()
        match = canonical_by_term.get(raw_name)
        if match is None:
            continue
        merged = {**update, **match.to_update(), "name": match.name}
        updates_by_name[match.name] = _merge_update(updates_by_name.get(match.name), merged)

    for character in mentioned:
        updates_by_name[character.name] = _merge_update(
            updates_by_name.get(character.name),
            character.to_update(),
        )

    warnings: list[str] = []
    if ignored_candidates:
        warnings.append(
            "canonical_character_filter_ignored: "
            + "、".join(_dedupe(ignored_candidates)[:12])
        )

    return CharacterCalibrationResult(
        characters=final_names,
        character_updates=[updates_by_name[name] for name in final_names if name in updates_by_name],
        warnings=warnings,
        ignored_candidates=_dedupe(ignored_candidates),
        canonical_count=len(canonical_characters),
    )


def canonicalize_names_in_records(records: list[Any], canonical_characters: list[CanonicalCharacter]) -> list[Any]:
    if not canonical_characters:
        return records
    canonical_by_term: dict[str, CanonicalCharacter] = {}
    for character in canonical_characters:
        for term in character.names_for_match():
            canonical_by_term[term] = character

    normalized = []
    for record in records:
        if not isinstance(record, dict):
            normalized.append(record)
            continue
        copy = dict(record)
        if isinstance(copy.get("characters"), list):
            copy["characters"] = _canonicalize_name_list(copy["characters"], canonical_by_term)
        normalized.append(copy)
    return normalized


def _load_bible_characters(host_database: ReadOnlyHostDatabase, novel_id: str) -> list[CanonicalCharacter]:
    rows = _safe_fetch_all(
        host_database,
        """
        SELECT id, name, description, mental_state, verbal_tic, idle_behavior
        FROM bible_characters
        WHERE novel_id = ?
        ORDER BY id
        """,
        (novel_id,),
    )
    items: list[CanonicalCharacter] = []
    for row in rows:
        name = _clean_name(row.get("name"))
        if not name:
            continue
        description = _join_nonempty(
            row.get("description"),
            _labeled("精神状态", row.get("mental_state")),
            _labeled("口癖", row.get("verbal_tic")),
            _labeled("待机行为", row.get("idle_behavior")),
        )
        items.append(
            CanonicalCharacter(
                character_id=str(row.get("id") or name),
                name=name,
                description=description,
                source="bible",
                confidence=0.99,
            )
        )
    return items


def _load_cast_characters(host_database: ReadOnlyHostDatabase, novel_id: str) -> list[CanonicalCharacter]:
    rows = _safe_fetch_all(
        host_database,
        "SELECT data FROM cast_snapshots WHERE novel_id = ? LIMIT 1",
        (novel_id,),
    )
    if not rows:
        return []
    try:
        data = json.loads(str(rows[0].get("data") or "{}"))
    except json.JSONDecodeError:
        return []
    characters = data.get("characters") if isinstance(data, dict) else []
    if not isinstance(characters, list):
        return []

    items: list[CanonicalCharacter] = []
    for raw in characters:
        if not isinstance(raw, dict):
            continue
        name = _clean_name(raw.get("name"))
        if not name:
            continue
        aliases = [_clean_name(item) for item in raw.get("aliases") or []]
        aliases = [item for item in aliases if item and item != name]
        description = _join_nonempty(raw.get("role"), raw.get("traits"), raw.get("note"))
        items.append(
            CanonicalCharacter(
                character_id=str(raw.get("id") or name),
                name=name,
                aliases=aliases,
                description=description,
                source="cast",
                confidence=0.94,
            )
        )
    return items


def _load_triple_characters(host_database: ReadOnlyHostDatabase, novel_id: str) -> list[CanonicalCharacter]:
    rows = _safe_fetch_all(
        host_database,
        """
        SELECT subject, object, description, confidence, subject_entity_id, object_entity_id
        FROM triples
        WHERE novel_id = ? AND entity_type = 'character'
        ORDER BY updated_at DESC
        LIMIT 200
        """,
        (novel_id,),
    )
    items: list[CanonicalCharacter] = []
    for row in rows:
        for value, entity_id in (
            (row.get("subject"), row.get("subject_entity_id")),
            (row.get("object"), row.get("object_entity_id")),
        ):
            name = _clean_name(value)
            if not name or not _looks_like_triple_character_name(name):
                continue
            items.append(
                CanonicalCharacter(
                    character_id=str(entity_id or name),
                    name=name,
                    description=str(row.get("description") or ""),
                    source="triples",
                    confidence=_safe_confidence(row.get("confidence"), default=0.82),
                )
            )
    return items


def _safe_fetch_all(host_database: ReadOnlyHostDatabase, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        return host_database.fetch_all(sql, params)
    except Exception:
        return []


def _merge_character(by_name: dict[str, CanonicalCharacter], incoming: CanonicalCharacter) -> None:
    existing = by_name.get(incoming.name)
    if existing is None:
        by_name[incoming.name] = incoming
        return
    existing.aliases = _dedupe([*existing.aliases, *incoming.aliases])
    if not existing.description and incoming.description:
        existing.description = incoming.description
    if incoming.source == "bible" and existing.source != "bible":
        existing.character_id = incoming.character_id
        existing.source = incoming.source
        existing.confidence = incoming.confidence


def _merge_update(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return dict(incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "aliases":
            merged[key] = _dedupe([*(merged.get(key) or []), *(value or [])])
        elif value and not merged.get(key):
            merged[key] = value
        elif key in {"canonical_character_id", "canonical_source", "profile_source"} and value:
            merged[key] = value
    return merged


def _canonicalize_name_list(values: list[Any], canonical_by_term: dict[str, CanonicalCharacter]) -> list[str]:
    result: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if not name:
            continue
        match = canonical_by_term.get(name)
        if match is not None:
            result.append(match.name)
    return _dedupe(result)


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    return name if _looks_like_character_name(name) else ""


def _looks_like_character_name(name: str) -> bool:
    if not name or len(name) < 2 or len(name) > 12:
        return False
    if any(token in name for token in ("教程", "章节", "文字", "系统", "说明", "地点", "世界观")):
        return False
    if re.search(r"[0-9_]", name):
        return False
    if re.search(r"[。！？!?，,；;：:\s]", name):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", name))


_COMMON_SURNAME_CHARS = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅齐康伍余顾孟黄穆萧尹姚邵汪祁毛狄米贝明计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵季贾路江童颜郭梅盛林钟徐邱骆高夏蔡田胡凌霍虞万卢莫房解应宗丁宣邓郁杭洪左石崔龚程邢裴陆荣翁荀甄曲封储段侯全班秋仲伊宫宁仇甘祖武符刘景龙叶黎白蒲卓蔺池乔闻党翟谭姬申冉雍桑尚温庄晏柴瞿阎慕连习鱼古易廖终居衡步耿满弘匡文寇广东欧沃利蔚越师巩聂晁勾敖融冷辛阚简饶曾沙养鞠丰关查游权益桓岳帅况"
)


def _looks_like_triple_character_name(name: str) -> bool:
    if not _looks_like_character_name(name):
        return False
    if name in {"师父", "老人", "男人", "女人", "少年", "少女", "管理员", "官员", "值守员"}:
        return False
    if any(
        token in name
        for token in (
            "和",
            "与",
            "及",
            "的",
            "实验",
            "议会",
            "禁区",
            "禁航区",
            "设备",
            "结构",
            "档案",
            "数据",
            "纸",
            "文件",
            "日志",
            "遗信",
            "深处",
        )
    ):
        return False
    if name.endswith(("之门", "老人", "文件", "日志", "遗信", "深处", "设备", "结构")):
        return False
    if name.startswith(("阿", "老")) and 2 <= len(name) <= 3:
        return True
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name) and name[0] in _COMMON_SURNAME_CHARS)


def _safe_confidence(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _labeled(label: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "NORMAL":
        return ""
    return f"{label}: {text}"


def _join_nonempty(*items: Any) -> str:
    return "；".join(str(item).strip() for item in items if str(item or "").strip())[:360]


def _dedupe(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
