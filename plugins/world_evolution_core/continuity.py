"""Chapter state summaries and deterministic continuity checks."""
from __future__ import annotations

import re
from typing import Any


DEFAULT_CHARACTERS = ["沈砚", "顾岚", "陆行舟", "沈澜", "顾珩", "圣像"]
TRACKED_OBJECTS = ["黑匣子", "临时平板", "临时卡片", "访客卡", "臂章", "金属盒子", "探测器", "读取器"]
LOCATION_MARKERS = [
    "C307",
    "宿舍区",
    "C区避难点",
    "监察处",
    "礼堂",
    "旧设备区",
    "E-07电梯井",
    "电梯井",
    "废弃储藏室",
    "观测平台",
    "主楼顶层",
    "塔顶",
    "天线阵列",
    "C3节点",
    "档案库",
    "设备间",
    "潮汐机房",
    "机械工坊",
    "实验楼",
    "水箱",
    "雾港学院",
    "雾港",
    "学院",
]
BROAD_LOCATIONS = {"学院", "雾港", "宿舍区"}
ARRIVAL_WORDS = ("才找到", "第一次找到", "终于找到", "找到", "走到", "来到", "抵达", "进入", "推开", "刷开")
LEAVE_WORDS = ("离开", "走出", "退回", "回到", "转身朝", "前往")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s+|\n+")
_TIME_RE = re.compile(
    r"(?:\d{1,2}:\d{2}|[零一二三四五六七八九十两0-9]+(?:分钟|小时|天|年)前|"
    r"十年前|三天前|今天|明天|昨天|昨晚|今晚|夜间|早上|清晨|上午|中午|下午|傍晚|晚上|"
    r"演习(?:开始|结束|期间)?|第[一二三四五六七八九十0-9]+(?:段|章|天))"
)


def build_chapter_summary(novel_id: str, chapter_number: int, content: str, at: str) -> dict[str, Any]:
    """Build a compact chapter summary for future context injection."""
    sentences = _sentences(content)
    opening = _window(content, head=True)
    ending = _window(content, head=False)
    opening_state = extract_state(opening, full_text=content)
    ending_state = extract_state(ending, full_text=content)
    chapter_state = extract_state(content, full_text=content)
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "summary_type": "chapter",
        "short_summary": _short_summary(sentences),
        "opening_state": opening_state,
        "ending_state": ending_state,
        "chapter_state": chapter_state,
        "carry_forward": _carry_forward(ending_state, sentences),
        "open_threads": _open_threads(sentences),
        "at": at,
    }


def build_volume_summary(novel_id: str, volume_index: int, chapter_summaries: list[dict[str, Any]], at: str) -> dict[str, Any]:
    """Build a larger summary for each 10-chapter block."""
    chapters = sorted(chapter_summaries, key=lambda item: int(item.get("chapter_number") or 0))
    start = int(chapters[0].get("chapter_number") or 0) if chapters else (volume_index - 1) * 10 + 1
    end = int(chapters[-1].get("chapter_number") or 0) if chapters else volume_index * 10
    unresolved: list[str] = []
    places: list[str] = []
    characters: list[str] = []
    lines = []
    for item in chapters:
        chapter_number = item.get("chapter_number")
        summary = _clean(item.get("short_summary") or "")
        if summary:
            lines.append(f"第{chapter_number}章：{summary}")
        carry = item.get("carry_forward") if isinstance(item.get("carry_forward"), dict) else {}
        unresolved.extend(_as_strings(carry.get("open_threads")))
        ending = item.get("ending_state") if isinstance(item.get("ending_state"), dict) else {}
        places.extend(_as_strings(ending.get("locations")))
        characters.extend(_as_strings(ending.get("characters")))
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "summary_type": "volume",
        "volume_index": volume_index,
        "chapter_start": start,
        "chapter_end": end,
        "short_summary": "；".join(lines)[-1200:],
        "main_locations": _dedupe(places)[-8:],
        "main_characters": _dedupe(characters)[-12:],
        "open_threads": _dedupe(unresolved)[-10:],
        "at": at,
    }


def analyze_chapter_transitions(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    states = []
    conflicts: list[dict[str, Any]] = []
    memory: dict[str, Any] = {"objects": {}, "visited_locations": set(), "flags": set()}
    sorted_chapters = sorted(chapters, key=lambda item: int(item.get("chapter_number") or 0))
    previous: dict[str, Any] | None = None
    for chapter in sorted_chapters:
        content = str(chapter.get("content") or "")
        chapter_number = int(chapter.get("chapter_number") or 0)
        state = build_chapter_summary("", chapter_number, content, "")
        states.append(state)
        if previous:
            conflicts.extend(_compare_adjacent(previous, state, memory))
        conflicts.extend(_compare_memory(state, memory))
        _update_memory(memory, state)
        previous = state
    return {
        "schema_version": 1,
        "states": states,
        "conflicts": conflicts,
        "aggregate": {
            "conflict_count": len(conflicts),
            "hard_conflict_count": sum(1 for item in conflicts if item.get("severity") == "hard"),
            "warning_count": sum(1 for item in conflicts if item.get("severity") == "warning"),
        },
    }


def extract_state(text: str, *, full_text: str = "") -> dict[str, Any]:
    compact = _clean(text)
    source = full_text or text
    return {
        "excerpt": compact[:360],
        "time_markers": _dedupe(_TIME_RE.findall(compact))[-6:],
        "locations": _extract_locations(compact),
        "characters": _extract_characters(compact, source),
        "object_states": _extract_object_states(compact),
        "actions": _extract_actions(compact),
    }


def _compare_adjacent(previous: dict[str, Any], current: dict[str, Any], memory: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    prev_chapter = int(previous.get("chapter_number") or 0)
    curr_chapter = int(current.get("chapter_number") or 0)
    prev_end = previous.get("ending_state") or {}
    curr_open = current.get("opening_state") or {}
    prev_text = str(prev_end.get("excerpt") or "")
    curr_text = str(curr_open.get("excerpt") or "")
    prev_locs = set(_as_strings(prev_end.get("locations")))
    curr_locs = set(_as_strings(curr_open.get("locations")))

    for loc in sorted((prev_locs | set(memory.get("visited_locations") or set())) & curr_locs):
        if loc in BROAD_LOCATIONS:
            continue
        if _has_arrival_reset(curr_text, loc):
            conflicts.append(
                _conflict(
                    "repeated_arrival",
                    "hard",
                    prev_chapter,
                    curr_chapter,
                    f"第{curr_chapter}章开头把{loc}写成重新/首次抵达，但前文已经到过该地点。",
                    prev_text,
                    curr_text,
                )
            )

    if "演习结束" in prev_text and ("演习期间" in curr_text or "等待广播通知演习结束" in curr_text):
        conflicts.append(
            _conflict(
                "time_rollback",
                "hard",
                prev_chapter,
                curr_chapter,
                "上一章已出现演习结束，下一章开头又回到演习进行中。",
                prev_text,
                curr_text,
            )
        )

    if prev_locs & curr_locs and any(word in curr_text for word in ("刷卡", "推开防火门", "走进", "进入")):
        conflicts.append(
            _conflict(
                "scene_reentry_needs_bridge",
                "warning",
                prev_chapter,
                curr_chapter,
                "相邻章节在同一地点重复进入，应补时间/视角/位置桥接，否则会像状态重置。",
                prev_text,
                curr_text,
            )
        )
    return conflicts


def _compare_memory(current: dict[str, Any], memory: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    chapter_number = int(current.get("chapter_number") or 0)
    opening = current.get("opening_state") or {}
    whole = current.get("chapter_state") or {}
    text = str(opening.get("excerpt") or "")
    object_text = " ".join(str(item.get("snippet") or "") for item in whole.get("object_states") or [] if isinstance(item, dict))
    object_memory = memory.get("objects") if isinstance(memory.get("objects"), dict) else {}

    black_box_state = str(object_memory.get("黑匣子") or "")
    if "锁进" in black_box_state and "抽屉" in black_box_state and "从帆布包里取出黑匣子" in object_text:
        conflicts.append(
            _conflict(
                "object_teleport",
                "hard",
                int(memory.get("last_object_chapter", {}).get("黑匣子") or chapter_number - 1),
                chapter_number,
                "黑匣子前文被锁进抽屉，后文直接从帆布包取出，缺少取回桥段。",
                black_box_state,
                object_text,
            )
        )

    if "entered_archive" in (memory.get("flags") or set()) and "档案库门口" in text and "非授权人员禁止进入" in text:
        conflicts.append(
            _conflict(
                "permission_state_reset",
                "hard",
                int(memory.get("entered_archive_chapter") or chapter_number - 1),
                chapter_number,
                "前文已经进入档案库，后文开头又回到档案库门口刷卡失败。",
                "前文状态：已进入档案库。",
                text,
            )
        )
    return conflicts


def _update_memory(memory: dict[str, Any], state: dict[str, Any]) -> None:
    chapter_number = int(state.get("chapter_number") or 0)
    for key in ("opening_state", "ending_state"):
        section = state.get(key) if isinstance(state.get(key), dict) else {}
        for loc in _as_strings(section.get("locations")):
            memory.setdefault("visited_locations", set()).add(loc)
        text = str(section.get("excerpt") or "")
        if "走进档案库" in text or "进入档案库" in text or "调阅" in text and "档案" in text:
            memory.setdefault("flags", set()).add("entered_archive")
            memory["entered_archive_chapter"] = chapter_number
        for item in section.get("object_states") or []:
            if not isinstance(item, dict):
                continue
            obj = str(item.get("object") or "")
            snippet = str(item.get("snippet") or "")
            if obj and snippet and _is_object_stateful(snippet):
                memory.setdefault("objects", {})[obj] = snippet
                memory.setdefault("last_object_chapter", {})[obj] = chapter_number


def _conflict(kind: str, severity: str, previous_chapter: int, current_chapter: int, message: str, previous_evidence: str, current_evidence: str) -> dict[str, Any]:
    return {
        "type": kind,
        "severity": severity,
        "previous_chapter": previous_chapter,
        "current_chapter": current_chapter,
        "message": message,
        "previous_evidence": _clean(previous_evidence)[:220],
        "current_evidence": _clean(current_evidence)[:220],
    }


def _short_summary(sentences: list[str]) -> str:
    if not sentences:
        return ""
    chosen = sentences[:2]
    if len(sentences) > 2:
        chosen.append(sentences[-1])
    return _clean(" ".join(chosen))[:520]


def _carry_forward(ending_state: dict[str, Any], sentences: list[str]) -> dict[str, Any]:
    return {
        "last_known_time": _last(ending_state.get("time_markers")),
        "last_known_locations": _as_strings(ending_state.get("locations"))[-4:],
        "onscreen_characters": _as_strings(ending_state.get("characters"))[-8:],
        "object_states": ending_state.get("object_states") or [],
        "open_threads": _open_threads(sentences),
        "required_next_bridge": "下一章开头必须承接上一章结尾；若跳过时间或地点，需先交代过渡，避免重复首次抵达、重复开门、物件瞬移。",
    }


def _open_threads(sentences: list[str]) -> list[str]:
    signals = ("?", "？", "为什么", "怎么", "不知道", "还不知道", "没有回答", "谁", "什么", "伏笔", "警告", "等你")
    result = [sentence for sentence in sentences[-8:] if any(signal in sentence for signal in signals)]
    return [_clean(item)[:160] for item in result[-4:]]


def _sentences(content: str) -> list[str]:
    return [_clean(item) for item in _SENTENCE_SPLIT_RE.split(str(content or "")) if _clean(item)]


def _window(content: str, *, head: bool, limit: int = 520) -> str:
    text = str(content or "").strip()
    return text[:limit] if head else text[-limit:]


def _extract_locations(text: str) -> list[str]:
    found = [marker for marker in LOCATION_MARKERS if marker in text]
    generic = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_-]{0,8}(?:学院|档案库|宿舍|礼堂|机房|楼顶|塔顶|平台|电梯井|设备间|工坊|避难点|水箱)", text)
    generic = [_clean_location(item) for item in generic]
    generic = [item for item in generic if item and item not in found]
    return _dedupe([*found, *generic])[:10]


def _clean_location(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("但", "然后", "已经", "主楼", "根据", "发件人是", "大多穿着深灰色的"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    for marker in sorted(LOCATION_MARKERS, key=len, reverse=True):
        if text != marker and text.endswith(marker):
            return marker
    return text


def _extract_characters(text: str, source: str) -> list[str]:
    names = [name for name in DEFAULT_CHARACTERS if name in text or name in source]
    return _dedupe(names)


def _extract_object_states(text: str) -> list[dict[str, str]]:
    states = []
    sentences = _sentences(text)
    for obj in TRACKED_OBJECTS:
        for sentence in sentences:
            if obj in sentence:
                states.append({"object": obj, "snippet": sentence[:180]})
                break
    return states[:8]


def _extract_actions(text: str) -> list[str]:
    actions = []
    for sentence in _sentences(text):
        if any(word in sentence for word in (*ARRIVAL_WORDS, *LEAVE_WORDS, "解锁", "播放", "发热", "锁进", "取出")):
            actions.append(sentence[:140])
    return actions[:8]


def _has_arrival_reset(text: str, location: str) -> bool:
    if location not in text:
        return False
    if f"擅自进入{location}" in text:
        return False
    patterns = [
        f"才找到{location}",
        f"第一次找到{location}",
        f"终于找到{location}",
        f"进入{location}",
        f"走进{location}",
        f"来到{location}",
        f"抵达{location}",
        f"推开{location}",
    ]
    if any(pattern in text for pattern in patterns):
        return True
    for marker in (f"{location}门口", f"{location}的门", f"{location}门禁"):
        position = text.find(marker)
        if position >= 0 and any(token in text[position : position + 80] for token in ("刷卡", "进门", "推开", "没有弹开", "禁止进入")):
            return True
    return False


def _is_object_stateful(text: str) -> bool:
    return any(
        token in text
        for token in ("锁进", "放在", "取出", "塞进", "收进", "掏出", "交给", "递给", "插上", "播放", "解锁", "发热", "掉在")
    )


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _last(value: Any) -> str:
    items = _as_strings(value)
    return items[-1] if items else ""
