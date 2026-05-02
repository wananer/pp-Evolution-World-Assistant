"""Personality palette helpers for Evolution character cards."""
from __future__ import annotations

from typing import Any

DEFAULT_PALETTE_METAPHOR = "人的性格像调色盘：底色、主色调与点缀共同驱动行为。"
NATIVE_DERIVED_SOURCE = "native_bible_derived"
STRUCTURED_SOURCE = "structured_extraction"

_GENERIC_MENTAL_STATES = {"", "NORMAL", "PRESSURE_LOCKED", "UNKNOWN", "未定", "待观察", "默认"}
_GENERIC_TONES = {"冷静", "谨慎", "警惕", "克制", "热情", "固执", "观察", "试探", "求证", "守序", "主动"}
_PRESENCE_MODES = {"active_scene", "remote", "memory_trace", "record_only", "system_entity"}
_SOURCE_PRIORITY = {
    "": 0,
    "default": 0,
    NATIVE_DERIVED_SOURCE: 20,
    "cast_derived": 20,
    "agent_derived": 40,
    STRUCTURED_SOURCE: 80,
    "manual": 100,
}

_SIGNAL_RULES = {
    "goal_signal": ("目标", "追求", "完成", "推进", "查清", "寻找", "夺回", "证明", "守住", "实现"),
    "risk_signal": ("风险", "危险", "威胁", "失败", "暴露", "失控", "代价", "压力", "恐惧", "担心"),
    "authority_signal": ("规则", "规程", "命令", "上级", "制度", "权威", "审查", "纪律", "职位", "责任"),
    "care_signal": ("保护", "照顾", "牵挂", "承诺", "救", "陪伴", "家人", "同伴", "团队", "学生"),
    "secrecy_signal": ("秘密", "隐瞒", "伪装", "身份", "掩饰", "谎言", "保密", "不能说", "不愿说"),
    "competence_signal": ("技能", "擅长", "分析", "技术", "判断", "检查", "记录", "调查", "训练", "经验"),
    "loss_signal": ("失去", "遗憾", "死亡", "失踪", "背叛", "离开", "创伤", "悔", "亏欠", "牺牲"),
    "boundary_signal": ("不能", "无法", "受限", "边界", "弱点", "缺陷", "代价", "权限", "身体", "资源"),
}


def derive_palette_from_native_character(
    *,
    name: str,
    description: Any = "",
    mental_state: Any = "",
    verbal_tic: Any = "",
    idle_behavior: Any = "",
) -> dict[str, Any]:
    """Derive a conservative non-empty palette from read-only native character fields."""
    clean_name = _clean(name, limit=40)
    fields = {
        "description": _clean(description, limit=400),
        "mental_state": _clean(mental_state, limit=120),
        "verbal_tic": _clean(verbal_tic, limit=120),
        "idle_behavior": _clean(idle_behavior, limit=120),
    }
    text = " ".join(value for value in fields.values() if value)
    if not text:
        return {}

    base = _derive_base(text, fields["mental_state"])
    tones = _derive_main_tones(text)
    accents = _derive_accents(fields)
    derivatives = _derive_derivatives(clean_name, base, tones, fields)
    signals = _detect_signals(text)
    pressure_triggers = _derive_pressure_triggers(signals, fields)
    relationship_tones = _derive_relationship_tones(signals)
    voice_signature = _derive_voice_signature(fields)
    gesture_signature = _derive_gesture_signature(fields)
    negative_costs = _derive_negative_costs(base, signals)
    presence_mode = _derive_presence_mode(text)
    source_refs = [
        {"source_type": "bible_character", "field": key, "character": clean_name}
        for key, value in fields.items()
        if value
    ]
    return {
        "metaphor": DEFAULT_PALETTE_METAPHOR,
        "base": base,
        "main_tones": tones,
        "accents": accents,
        "derivatives": derivatives,
        "pressure_triggers": pressure_triggers,
        "relationship_tones": relationship_tones,
        "voice_signature": voice_signature,
        "gesture_signature": gesture_signature,
        "negative_costs": negative_costs,
        "presence_mode": presence_mode,
        "source": NATIVE_DERIVED_SOURCE,
        "source_refs": source_refs[:6],
    }


def merge_palette_missing_fields(existing: Any, incoming: Any) -> dict[str, Any]:
    """Merge palette fields while preserving richer/manual palettes over derived fallbacks."""
    current = _normalize_palette(existing)
    candidate = _normalize_palette(incoming)
    if not _palette_has_content(candidate):
        return current

    current_priority = _priority(current, incoming=False)
    candidate_priority = _priority(candidate, incoming=True)
    may_replace = candidate_priority > current_priority and str(current.get("source") or "") == NATIVE_DERIVED_SOURCE

    changed = False
    if candidate.get("metaphor") and (not current.get("metaphor") or current.get("metaphor") == DEFAULT_PALETTE_METAPHOR):
        current["metaphor"] = candidate["metaphor"]
        changed = True
    for key in ("base",):
        if candidate.get(key) and (not current.get(key) or may_replace):
            current[key] = candidate[key]
            changed = True
    for key, limit in (
        ("main_tones", 8),
        ("accents", 10),
        ("derivatives", 32),
        ("pressure_triggers", 8),
        ("relationship_tones", 12),
        ("voice_signature", 6),
        ("gesture_signature", 6),
        ("negative_costs", 8),
    ):
        incoming_items = candidate.get(key) if isinstance(candidate.get(key), list) else []
        current_items = current.get(key) if isinstance(current.get(key), list) else []
        if incoming_items and (not current_items or may_replace):
            current[key] = incoming_items[:limit]
            changed = True
    if candidate.get("presence_mode") and (not current.get("presence_mode") or current.get("presence_mode") == "active_scene" or may_replace):
        current["presence_mode"] = candidate["presence_mode"]
        changed = True

    if changed:
        source = str(candidate.get("source") or "").strip()
        if source and (not current.get("source") or may_replace):
            current["source"] = source
        refs = _merge_source_refs(current.get("source_refs"), candidate.get("source_refs"))
        if refs:
            current["source_refs"] = refs
    return current


def palette_missing_fields(palette: Any) -> list[str]:
    data = palette if isinstance(palette, dict) else {}
    missing: list[str] = []
    if not _clean(data.get("base")):
        missing.append("base")
    if not data.get("main_tones"):
        missing.append("main_tones")
    if not data.get("derivatives"):
        missing.append("derivatives")
    return missing


def personality_palette_status(cards: list[dict[str, Any]]) -> dict[str, Any]:
    ignored = [_ignored_non_character_entity(card) for card in cards if _invalid_card(card)]
    ignored = [item for item in ignored if item]
    active = [card for card in cards if not _invalid_card(card)]
    missing_cards = []
    source_counts: dict[str, int] = {}
    presence_mode_counts: dict[str, int] = {}
    generic_tone_count = 0
    pressure_trigger_count = 0
    relationship_tone_count = 0
    signature_count = 0
    depth_total = 0.0
    for card in active:
        palette = card.get("personality_palette") if isinstance(card.get("personality_palette"), dict) else {}
        source = str(palette.get("source") or "unspecified")
        source_counts[source] = source_counts.get(source, 0) + 1
        presence_mode = _clean(palette.get("presence_mode"), limit=40) or "active_scene"
        presence_mode_counts[presence_mode] = presence_mode_counts.get(presence_mode, 0) + 1
        generic_tone_count += _generic_tone_count(palette)
        pressure_trigger_count += len(palette.get("pressure_triggers") if isinstance(palette.get("pressure_triggers"), list) else [])
        relationship_tone_count += len(palette.get("relationship_tones") if isinstance(palette.get("relationship_tones"), list) else [])
        signature_count += len(palette.get("voice_signature") if isinstance(palette.get("voice_signature"), list) else [])
        signature_count += len(palette.get("gesture_signature") if isinstance(palette.get("gesture_signature"), list) else [])
        depth_total += _palette_depth_score(palette)
        missing = palette_missing_fields(palette)
        if missing:
            missing_cards.append(
                {
                    "name": str(card.get("name") or ""),
                    "last_seen_chapter": card.get("last_seen_chapter"),
                    "missing_fields": missing,
                    "source": source,
                }
            )
    complete = len(active) - len(missing_cards)
    return {
        "character_count": len(active),
        "complete_count": complete,
        "missing_count": len(missing_cards),
        "coverage": round(complete / len(active), 4) if active else 0.0,
        "source_counts": dict(sorted(source_counts.items())),
        "depth_score": round(depth_total / len(active), 4) if active else 0.0,
        "generic_tone_count": generic_tone_count,
        "pressure_trigger_count": pressure_trigger_count,
        "relationship_tone_count": relationship_tone_count,
        "signature_count": signature_count,
        "presence_mode_counts": dict(sorted(presence_mode_counts.items())),
        "missing": missing_cards[:12],
        "ignored_non_character_entities": ignored[:12],
        "ignored_non_character_count": len(ignored),
    }


def _derive_base(text: str, mental_state: str) -> str:
    if mental_state and mental_state not in _GENERIC_MENTAL_STATES:
        return mental_state[:40]
    signals = _detect_signals(text)
    if "loss_signal" in signals and "goal_signal" in signals:
        return "由失去感驱动的目标追索"
    if "competence_signal" in signals and "risk_signal" in signals:
        return "以能力处理风险的行动底色"
    if "authority_signal" in signals and "boundary_signal" in signals:
        return "在规则边界内寻找行动空间"
    if "secrecy_signal" in signals and "care_signal" in signals:
        return "用隐瞒承担保护压力"
    if "competence_signal" in signals:
        return "以专业能力确认世界"
    if "goal_signal" in signals:
        return "目标牵引的行动底色"
    if "risk_signal" in signals:
        return "风险感知下的防御底色"
    return "待观察的行动底色"


def _derive_main_tones(text: str) -> list[str]:
    rules = [
        (_SIGNAL_RULES["goal_signal"], "目标牵引：优先确认下一步要达成什么"),
        (_SIGNAL_RULES["risk_signal"], "风险敏感：先识别代价和失控点"),
        (_SIGNAL_RULES["authority_signal"], "规则意识：会用制度、责任或职位解释行动"),
        (_SIGNAL_RULES["care_signal"], "关系照看：关键选择会受保护对象影响"),
        (_SIGNAL_RULES["secrecy_signal"], "信息保留：不会一次交出全部真实意图"),
        (_SIGNAL_RULES["competence_signal"], "能力求证：通过技能、检查或记录建立判断"),
        (_SIGNAL_RULES["loss_signal"], "失去回声：过去损失会改变当前判断"),
        (_SIGNAL_RULES["boundary_signal"], "边界自觉：行动受弱点、权限或资源限制"),
    ]
    tones: list[str] = []
    for keywords, tone in rules:
        if any(keyword in text for keyword in keywords):
            tones.append(tone)
    if not tones:
        tones = ["观察试探：先用小动作和短反馈确认局面"]
    return _dedupe_strings(tones, limit=3)


def _derive_accents(fields: dict[str, str]) -> list[str]:
    accents: list[str] = []
    if fields.get("verbal_tic"):
        accents.append(f"口头锚点：{fields['verbal_tic'][:24]}")
    if fields.get("idle_behavior"):
        accents.append(f"待机动作：{fields['idle_behavior'][:24]}")
    if fields.get("mental_state") and fields["mental_state"] not in _GENERIC_MENTAL_STATES:
        accents.append(f"当前压力：{fields['mental_state'][:24]}")
    return _dedupe_strings(accents, limit=3)


def _derive_derivatives(name: str, base: str, tones: list[str], fields: dict[str, str]) -> list[dict[str, Any]]:
    primary = tones[0] if tones else base
    derivatives = [
        {
            "tone": primary,
            "title": "压力下的默认行动",
            "description": f"{name or '角色'}遇到风险或线索时，先沿着“{base}”底色行动，用{primary}回应，而不是突然切换成无来由的反应。",
            "trigger": "线索、风险或关系压力出现时",
            "visibility": "通过动作选择、说话方式和是否求证体现",
            "future": False,
        }
    ]
    if fields.get("verbal_tic"):
        derivatives.append(
            {
                "tone": "声线锚点",
                "title": "说话方式外显",
                "description": f"对话可围绕“{fields['verbal_tic'][:40]}”形成稳定声线，但不要机械复读。",
                "trigger": "需要表态、质疑或阻止他人时",
                "visibility": "短句、反问或确认式表达",
                "future": False,
            }
        )
    if fields.get("idle_behavior"):
        derivatives.append(
            {
                "tone": "动作锚点",
                "title": "无台词时的行为底纹",
                "description": f"沉默或等待时可用“{fields['idle_behavior'][:40]}”一类动作承接性格，而不是空站场。",
                "trigger": "等待、犹豫、观察环境时",
                "visibility": "手部动作、视线、位置选择",
                "future": False,
            }
        )
    return derivatives[:3]


def _detect_signals(text: str) -> set[str]:
    return {signal for signal, keywords in _SIGNAL_RULES.items() if any(keyword in text for keyword in keywords)}


def _derive_pressure_triggers(signals: set[str], fields: dict[str, str]) -> list[str]:
    triggers: list[str] = []
    if "goal_signal" in signals:
        triggers.append("目标受阻或关键线索被夺走时，优先寻找可执行突破口")
    if "risk_signal" in signals:
        triggers.append("局面有失控代价时，先收缩表达并确认风险来源")
    if "loss_signal" in signals:
        triggers.append("过去损失被重新触发时，容易把情绪转译成行动")
    if "boundary_signal" in signals:
        triggers.append("能力、权限或资源不足时，会暴露补偿性选择")
    if fields.get("mental_state") and fields["mental_state"] not in _GENERIC_MENTAL_STATES:
        triggers.append(f"当前状态被触动时：{fields['mental_state'][:40]}")
    return _dedupe_strings(triggers, limit=4)


def _derive_relationship_tones(signals: set[str]) -> list[dict[str, str]]:
    tones: list[dict[str, str]] = []
    if "care_signal" in signals:
        tones.append({"target": "被保护者/亲近对象", "tone": "保护但可能隐瞒", "behavior": "先替对方挡住风险，再决定披露多少信息"})
    if "authority_signal" in signals:
        tones.append({"target": "权威/制度对象", "tone": "尊重边界但会试探", "behavior": "先承认规则，再寻找规则缝隙"})
    if "secrecy_signal" in signals:
        tones.append({"target": "未完全信任者", "tone": "试探且保留", "behavior": "用问题或局部事实观察对方反应"})
    if "goal_signal" in signals and not tones:
        tones.append({"target": "合作者/阻碍者", "tone": "按目标区分亲疏", "behavior": "能推进目标就靠近，阻断目标就保持距离"})
    return tones[:4]


def _derive_voice_signature(fields: dict[str, str]) -> list[str]:
    if fields.get("verbal_tic"):
        return [f"围绕“{fields['verbal_tic'][:40]}”形成声线锚点，但避免机械复读"]
    return ["表态先落到判断依据或行动选择，少用纯情绪标签"]


def _derive_gesture_signature(fields: dict[str, str]) -> list[str]:
    if fields.get("idle_behavior"):
        return [f"无台词时用“{fields['idle_behavior'][:40]}”一类动作承接性格"]
    return ["沉默时用视线、站位、手部动作显示态度变化"]


def _derive_negative_costs(base: str, signals: set[str]) -> list[str]:
    costs: list[str] = []
    if "competence_signal" in signals:
        costs.append("过度依赖专业判断时，可能忽略他人的情绪和关系信号")
    if "authority_signal" in signals:
        costs.append("过度遵守规则时，可能延误需要立刻承担的选择")
    if "secrecy_signal" in signals:
        costs.append("过度保留信息时，容易制造误会或削弱同伴信任")
    if "goal_signal" in signals:
        costs.append("过度盯住目标时，可能牺牲过程中的柔软反应")
    if not costs and base:
        costs.append(f"过度沿着“{base}”行动时，容易让反应变窄")
    return costs[:3]


def _derive_presence_mode(text: str) -> str:
    if any(term in text for term in ("回忆", "记忆", "梦见", "曾经", "旧照片")):
        return "memory_trace"
    if any(term in text for term in ("录音", "笔记", "档案", "遗书", "记录", "留言", "影像")):
        return "record_only"
    if any(term in text for term in ("系统", "协议", "算法", "AI", "人工智能", "程序", "中枢")):
        return "system_entity"
    if any(term in text for term in ("远程", "通讯", "电话", "视频", "消息")):
        return "remote"
    return "active_scene"


def _normalize_palette(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "metaphor": DEFAULT_PALETTE_METAPHOR,
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
    data = dict(value)
    data["metaphor"] = _clean(data.get("metaphor"), limit=240) or DEFAULT_PALETTE_METAPHOR
    data["base"] = _clean(data.get("base"), limit=40)
    data["main_tones"] = _dedupe_strings(data.get("main_tones") or [], limit=8)
    data["accents"] = _dedupe_strings(data.get("accents") or [], limit=10)
    data["derivatives"] = _normalize_derivatives(data.get("derivatives"))
    data["pressure_triggers"] = _dedupe_strings(data.get("pressure_triggers") or [], limit=8)
    data["relationship_tones"] = _normalize_relationship_tones(data.get("relationship_tones"))
    data["voice_signature"] = _dedupe_strings(data.get("voice_signature") or [], limit=6)
    data["gesture_signature"] = _dedupe_strings(data.get("gesture_signature") or [], limit=6)
    data["negative_costs"] = _dedupe_strings(data.get("negative_costs") or [], limit=8)
    presence_mode = _clean(data.get("presence_mode"), limit=40)
    data["presence_mode"] = presence_mode if presence_mode in _PRESENCE_MODES else "active_scene"
    if data.get("source"):
        data["source"] = _clean(data.get("source"), limit=60)
    if isinstance(data.get("source_refs"), list):
        data["source_refs"] = _merge_source_refs([], data.get("source_refs"))
    return data


def _normalize_derivatives(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if isinstance(item, str):
            record = {"tone": "", "title": "", "description": _clean(item, limit=300), "trigger": "", "visibility": "", "future": False}
        elif isinstance(item, dict):
            record = {
                "tone": _clean(item.get("tone"), limit=40),
                "title": _clean(item.get("title"), limit=60),
                "description": _clean(item.get("description"), limit=300),
                "trigger": _clean(item.get("trigger"), limit=120),
                "visibility": _clean(item.get("visibility"), limit=120),
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
    return result[:32]


def _normalize_relationship_tones(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if isinstance(item, str):
            record = {"target": "相关对象", "tone": _clean(item, limit=80), "behavior": ""}
        elif isinstance(item, dict):
            record = {
                "target": _clean(item.get("target") or item.get("object"), limit=80) or "相关对象",
                "tone": _clean(item.get("tone"), limit=80),
                "behavior": _clean(item.get("behavior") or item.get("description"), limit=160),
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
    return result[:12]


def _priority(palette: dict[str, Any], *, incoming: bool) -> int:
    source = str(palette.get("source") or "").strip()
    if source:
        return _SOURCE_PRIORITY.get(source, 60)
    if _palette_has_content(palette):
        return 80 if incoming else 100
    return 0


def _palette_has_content(palette: dict[str, Any]) -> bool:
    return bool(
        _clean(palette.get("base"))
        or palette.get("main_tones")
        or palette.get("accents")
        or palette.get("derivatives")
        or palette.get("pressure_triggers")
        or palette.get("relationship_tones")
        or palette.get("voice_signature")
        or palette.get("gesture_signature")
        or palette.get("negative_costs")
    )


def _generic_tone_count(palette: dict[str, Any]) -> int:
    count = 0
    for tone in palette.get("main_tones") if isinstance(palette.get("main_tones"), list) else []:
        value = _clean(tone, limit=80)
        if value in _GENERIC_TONES:
            count += 1
    return count


def _palette_depth_score(palette: dict[str, Any]) -> float:
    score = 0.0
    if _clean(palette.get("base")):
        score += 0.15
    if palette.get("main_tones"):
        score += 0.15
    if palette.get("derivatives"):
        score += 0.2
    if palette.get("pressure_triggers"):
        score += 0.15
    if palette.get("relationship_tones"):
        score += 0.15
    if palette.get("voice_signature") or palette.get("gesture_signature"):
        score += 0.1
    if palette.get("negative_costs"):
        score += 0.1
    return min(score, 1.0)


def _merge_source_refs(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*(existing if isinstance(existing, list) else []), *(incoming if isinstance(incoming, list) else [])]:
        if not isinstance(item, dict):
            continue
        record = {
            "source_type": _clean(item.get("source_type"), limit=60),
            "field": _clean(item.get("field"), limit=60),
            "character": _clean(item.get("character"), limit=60),
        }
        if not any(record.values()):
            continue
        key = (record["source_type"], record["field"], record["character"])
        if key in seen:
            continue
        seen.add(key)
        refs.append(record)
    return refs[:8]


def _invalid_card(card: dict[str, Any]) -> bool:
    return str(card.get("status") or "") == "invalid_entity" or str(card.get("entity_type") or "") == "non_person"


def _ignored_non_character_entity(card: dict[str, Any]) -> dict[str, Any]:
    name = _clean(card.get("name"), limit=60)
    if not name:
        return {}
    return {
        "name": name,
        "last_seen_chapter": card.get("last_seen_chapter"),
        "reason": _clean(card.get("invalid_reason"), limit=120) or "filtered_non_character_entity",
    }


def _dedupe_strings(items: Any, *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return result
    for item in items:
        value = _clean(item, limit=160)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result[:limit]


def _clean(value: Any, *, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]
