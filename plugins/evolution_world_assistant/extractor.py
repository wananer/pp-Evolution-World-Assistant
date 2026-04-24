"""Lightweight deterministic fact extractor.

This is intentionally conservative. It only extracts explicit-ish names and
places from committed text so Phase 1 remains fact-driven before LLM extraction
is introduced.
"""
from __future__ import annotations

import re

from .models import ChapterFactSnapshot

_CJK_NAME_RE = re.compile(r"(?:主角|少年|少女|男子|女子|老人|导师|师父|师傅|皇帝|将军|队长|船长|医生|侦探|骑士|公主|王子|[\u4e00-\u9fff]{2,4})(?=说|道|问|答|想|看|走|抵达|来到|进入|离开|出现|失踪|醒来|死|笑|哭|，|。|、)")
_QUOTED_NAME_RE = re.compile(r"[《“‘]([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_·]{1,24})[》”’]")
_LOCATION_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_·]{1,8}(?:城|镇|村|山|谷|宫|殿|塔|港|湖|河|海|岛|森林|学院|基地|星|站|街|巷|门|府))")
_LOCATION_PREFIX_RE = re.compile(r"^(?:抵达|来到|进入|离开|前往|返回|经过|穿过|导师发现|发现)+")
_EVENT_SPLIT_RE = re.compile(r"[。！？!?\n]+")

_STOP_NAMES = {"主角", "少年", "少女", "男子", "女子", "老人"}
_BAD_NAME_FRAGMENTS = ("的", "了", "在", "并", "和")


def extract_chapter_facts(novel_id: str, chapter_number: int, content_hash: str, content: str, at: str) -> ChapterFactSnapshot:
    summary = _summary(content)
    characters = _dedupe(_extract_characters(content))[:12]
    locations = _dedupe(_normalize_location(match.group(1)) for match in _LOCATION_RE.finditer(content))[:12]
    events = _dedupe(_extract_events(content))[:8]
    return ChapterFactSnapshot(
        novel_id=novel_id,
        chapter_number=chapter_number,
        content_hash=content_hash,
        summary=summary,
        characters=characters,
        locations=locations,
        world_events=events,
        at=at,
    )


def _summary(content: str, limit: int = 500) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    return compact[:limit]


def _extract_characters(content: str):
    for match in _QUOTED_NAME_RE.finditer(content):
        name = match.group(1).strip()
        if _valid_name(name):
            yield name
    for match in _CJK_NAME_RE.finditer(content):
        name = match.group(0).strip()
        if _valid_name(name) and not name.endswith(("城", "镇", "村")):
            yield name


def _extract_events(content: str):
    for sentence in _EVENT_SPLIT_RE.split(content):
        sentence = sentence.strip()
        if len(sentence) < 8:
            continue
        if any(token in sentence for token in ("抵达", "来到", "进入", "离开", "发现", "失踪", "死亡", "袭击", "结盟", "背叛", "开战", "爆发")):
            yield sentence[:160]


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_location(value: str) -> str:
    return _LOCATION_PREFIX_RE.sub("", str(value or "").strip())


def _valid_name(value: str) -> bool:
    name = str(value or "").strip()
    if not name or name in _STOP_NAMES:
        return False
    return not any(fragment in name for fragment in _BAD_NAME_FRAGMENTS)
