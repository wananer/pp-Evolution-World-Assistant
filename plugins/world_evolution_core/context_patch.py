"""Budget-friendly context patch builder for Evolution World."""
from __future__ import annotations

import re
from typing import Any, Optional

from .agent_assets import render_agent_selection
from .context_capsules import enrich_blocks_with_capsules
from .host_context import render_host_context_sections

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

PLUGIN_NAME = "world_evolution_core"
TIER_T0 = "intended_t0"
TIER_T1 = "intended_t1"

T0_CONTEXT_KINDS = {
    "chapter_state_bridge",
    "focus_character_state",
    "background_character_constraint",
    "chapter_facts",
    "story_graph_route_constraints",
    "continuity_risk",
}

T1_CONTEXT_KINDS = {
    "usage_protocol",
    "plotpilot_native_context_strategy",
    "agent_strategy",
    "local_semantic_memory",
    "style_repetition_guard",
}


def build_context_patch(
    novel_id: str,
    chapter_number: Optional[int],
    characters: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    outline: str = "",
    chapter_summaries: Optional[list[dict[str, Any]]] = None,
    volume_summaries: Optional[list[dict[str, Any]]] = None,
    previous_injections: Optional[list[dict[str, Any]]] = None,
    route_map: Optional[dict[str, Any]] = None,
    semantic_memory: Optional[dict[str, Any]] = None,
    host_context: Optional[dict[str, Any]] = None,
    agent_selection: Optional[dict[str, Any]] = None,
    style_repetition_state: Optional[dict[str, Any]] = None,
    max_characters: int = 8,
    max_facts: int = 5,
) -> dict[str, Any]:
    recent_facts = facts[-max_facts:]
    recent_summaries = (chapter_summaries or [])[-10:]
    recent_volumes = (volume_summaries or [])[-3:]
    selection = _select_characters(characters, max_characters, outline=outline, recent_facts=recent_facts)
    focus_characters = selection["focus"]
    background_characters = selection["background"]
    offstage_characters = selection["offstage"]
    blocks = []

    state_board = _render_state_board(recent_summaries, recent_volumes)
    if state_board:
        blocks.append(
            {
                "id": "evolution_usage_protocol",
                "title": "Evolution 使用方式",
                "kind": "usage_protocol",
                "priority": 78,
                "token_budget": 120,
                "content": _render_usage_protocol(),
                "items": [],
            }
        )
        blocks.append(
            {
                "id": "chapter_state_bridge",
                "title": "章节承接状态",
                "kind": "chapter_state_bridge",
                "priority": 82,
                "token_budget": 520,
                "content": state_board,
                "items": {"chapters": recent_summaries[-3:], "volumes": recent_volumes[-1:]},
            }
        )

    if focus_characters:
        if not state_board:
            blocks.append(
                {
                    "id": "evolution_usage_protocol",
                    "title": "Evolution 使用方式",
                    "kind": "usage_protocol",
                    "priority": 78,
                    "token_budget": 120,
                    "content": _render_usage_protocol(),
                    "items": [],
                }
            )
        blocks.append(
            {
                "id": "focus_characters",
                "title": "本章焦点角色",
                "kind": "focus_character_state",
                "priority": 76,
                "token_budget": 360,
                "content": _render_focus_characters(focus_characters),
                "items": focus_characters,
            }
        )

    if background_characters:
        blocks.append(
            {
                "id": "background_constraints",
                "title": "背景约束角色",
                "kind": "background_character_constraint",
                "priority": 66,
                "token_budget": 260,
                "content": _render_background_constraints(background_characters),
                "items": background_characters,
            }
        )

    if recent_facts:
        blocks.append(
            {
                "id": "recent_facts",
                "title": "近期章节事实",
                "kind": "chapter_facts",
                "priority": 62,
                "token_budget": 520,
                "content": _render_facts(recent_facts),
                "items": recent_facts,
            }
        )

    host_blocks = render_host_context_sections(host_context or {})
    if host_blocks:
        blocks.extend(host_blocks)

    agent_board = render_agent_selection(agent_selection)
    if agent_board:
        blocks.append(
            {
                "id": "evolution_agent_strategy",
                "title": "Evolution 智能体策略",
                "kind": "agent_strategy",
                "priority": 64,
                "token_budget": 420,
                "content": agent_board,
                "items": {
                    "signals": (agent_selection or {}).get("signals") or [],
                    "selected_gene_ids": (agent_selection or {}).get("selected_gene_ids") or [],
                    "selected_capsule_ids": (agent_selection or {}).get("selected_capsule_ids") or [],
                    "rationale": (agent_selection or {}).get("rationale") or "",
                },
            }
        )

    semantic_board = _render_semantic_memory_board(semantic_memory)
    if semantic_board:
        blocks.append(
            {
                "id": "local_semantic_memory",
                "title": "本地语义记忆召回",
                "kind": "local_semantic_memory",
                "priority": 60,
                "token_budget": 420,
                "content": semantic_board,
                "items": (semantic_memory or {}).get("items") or [],
            }
        )

    route_board = _render_route_board(route_map)
    if route_board:
        blocks.append(
            {
                "id": "story_graph_routes",
                "title": "人物路线与世界线图",
                "kind": "story_graph_route_constraints",
                "priority": 58,
                "token_budget": 360,
                "content": route_board,
                "items": {
                    "aggregate": (route_map or {}).get("aggregate") or {},
                    "conflicts": ((route_map or {}).get("conflicts") or [])[-6:],
                    "meetings": ((route_map or {}).get("meetings") or [])[-8:],
                },
            }
        )

    repetition_board = _render_style_repetition_board(style_repetition_state)
    if repetition_board:
        blocks.append(
            {
                "id": "style_repetition_guard",
                "title": "重复表达前置控制",
                "kind": "style_repetition_guard",
                "priority": 57,
                "token_budget": 220,
                "content": repetition_board,
                "items": (style_repetition_state or {}).get("phrases") or [],
            }
        )

    risks = _build_risks(focus_characters, recent_facts, offstage_characters)
    if risks:
        blocks.append(
            {
                "id": "continuity_risks",
                "title": "连续性风险提醒",
                "kind": "continuity_risk",
                "priority": 54,
                "token_budget": 260,
                "content": "\n".join(f"- {item}" for item in risks),
                "items": risks,
            }
        )

    blocks = _apply_injection_tiers(blocks)
    blocks, skipped_blocks = enrich_blocks_with_capsules(
        blocks,
        novel_id=novel_id,
        chapter_number=chapter_number,
        previous_records=previous_injections,
    )

    return {
        "plugin_name": PLUGIN_NAME,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "schema_version": 1,
        "merge_strategy": "append_by_priority",
        "blocks": blocks,
        "skipped_blocks": skipped_blocks,
        "agent_selection": agent_selection or {},
        "host_context_summary": _host_context_summary(host_context),
        "plotpilot_context_usage": dict((host_context or {}).get("plotpilot_context_usage") or {}),
        "estimated_token_budget": sum(int(block.get("token_budget") or 0) for block in blocks),
    }


def render_patch_summary(patch: dict[str, Any]) -> str:
    lines: list[str] = []
    for block in patch.get("blocks") or []:
        content = str(block.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"【{block.get('title') or block.get('id')}】")
        lines.append(content)
    return "\n".join(lines)


def tier_summary(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize explicit Evolution injection tiers for diagnostics and audit."""
    counts = {TIER_T0: 0, TIER_T1: 0, "unknown": 0}
    chars = {TIER_T0: 0, TIER_T1: 0, "unknown": 0}
    block_tiers: list[dict[str, Any]] = []
    for block in blocks:
        tier = _block_tier(block)
        bucket = tier if tier in {TIER_T0, TIER_T1} else "unknown"
        content_chars = len(str(block.get("content") or ""))
        counts[bucket] += 1
        chars[bucket] += content_chars
        block_tiers.append(
            {
                "id": block.get("id"),
                "kind": block.get("kind"),
                "tier": tier or "unknown",
                "chars": content_chars,
            }
        )
    return {
        "t0_block_count": counts[TIER_T0],
        "t1_block_count": counts[TIER_T1],
        "tier_unknown_count": counts["unknown"],
        "t0_chars": chars[TIER_T0],
        "t1_chars": chars[TIER_T1],
        "tier_unknown_chars": chars["unknown"],
        "block_tiers": block_tiers,
    }


def _host_context_summary(context: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        "source": context.get("source"),
        "active_sources": list(context.get("active_sources") or []),
        "degraded_sources": list(context.get("degraded_sources") or []),
        "empty_sources": list(context.get("empty_sources") or []),
        "field_missing_sources": list(context.get("field_missing_sources") or []),
        "source_status": dict(context.get("source_status") or {}),
        "counts": dict(context.get("counts") or {}),
        "before_chapter": context.get("before_chapter"),
        "plotpilot_context_usage": dict(context.get("plotpilot_context_usage") or {}),
    }


def _apply_injection_tiers(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_with_injection_tier(block, _infer_injection_tier(block)) for block in blocks]


def _infer_injection_tier(block: dict[str, Any]) -> str:
    kind = str(block.get("kind") or "")
    block_id = str(block.get("id") or "")
    if kind in T0_CONTEXT_KINDS or block_id in {"chapter_state_bridge", "focus_characters", "background_constraints", "recent_facts", "story_graph_routes", "continuity_risks"}:
        return TIER_T0
    if kind in T1_CONTEXT_KINDS or block_id in {"evolution_usage_protocol", "plotpilot_native_strategy", "evolution_agent_strategy", "local_semantic_memory", "style_repetition_guard"}:
        return TIER_T1
    return TIER_T1


def _with_injection_tier(block: dict[str, Any], tier: str) -> dict[str, Any]:
    enriched = dict(block)
    metadata = dict(enriched.get("metadata") or {})
    metadata["tier"] = tier
    metadata["injection_layer"] = "t0_hard_constraints" if tier == TIER_T0 else "t1_soft_strategy"
    metadata.setdefault("strategy_only", True)
    enriched["tier"] = tier
    enriched["metadata"] = metadata
    return enriched


def _block_tier(block: dict[str, Any]) -> str:
    tier = str(block.get("tier") or "").strip()
    if tier:
        return tier
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return str(metadata.get("tier") or "").strip()


def _select_characters(
    characters: list[dict[str, Any]],
    limit: int,
    *,
    outline: str = "",
    recent_facts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, list[dict[str, Any]]]:
    scored = []
    outline_text = str(outline or "")
    latest_chapter = max([int(fact.get("chapter_number") or 0) for fact in (recent_facts or [])] or [0])
    fact_text = "\n".join(str(fact.get("summary") or "") for fact in (recent_facts or []))

    for card in characters:
        name = str(card.get("name") or "")
        aliases = [str(alias) for alias in (card.get("aliases") or []) if alias]
        terms = [name, *aliases]
        last_seen = int(card.get("last_seen_chapter") or 0)
        score = 0
        reasons: list[str] = []
        if any(term and term in outline_text for term in terms):
            score += 100
            reasons.append("本章大纲明确提及")
        latest_event = (card.get("recent_events") or [])[-1] if card.get("recent_events") else {}
        latest_summary = str(latest_event.get("summary") or "")
        locations = " ".join(str(item) for item in latest_event.get("locations") or [])
        if outline_text and latest_summary and any(token and token in outline_text for token in _extract_context_terms(latest_summary + " " + locations)):
            score += 35
            reasons.append("与本章地点/物件相关")
        if latest_chapter and last_seen == latest_chapter:
            score += 12
            reasons.append("上一有效事实中刚出现")
        elif latest_chapter and last_seen >= latest_chapter - 1:
            score += 6
        if name and name in fact_text:
            score += 4
        enriched = {**card, "injection_relevance": {"score": score, "reasons": reasons or ["近期背景"]}}
        scored.append((score, -last_seen, str(card.get("first_seen_chapter") or ""), name, enriched))

    scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    has_outline = bool(outline_text.strip())
    if has_outline:
        focus = [item[-1] for item in scored if item[0] >= 80][:limit]
        background = [item[-1] for item in scored if 35 <= item[0] < 80][:4]
        offstage = [item[-1] for item in scored if item[0] < 35 and _is_recent(item[-1], latest_chapter)][:4]
        return {"focus": focus, "background": background, "offstage": offstage}
    return {"focus": [item[-1] for item in scored[:limit]], "background": [], "offstage": []}


def _is_recent(card: dict[str, Any], latest_chapter: int) -> bool:
    if not latest_chapter:
        return False
    return int(card.get("last_seen_chapter") or 0) >= latest_chapter - 2


def _extract_context_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"[\u4e00-\u9fffA-Za-z0-9·]{2,14}", text or ""):
        value = match.group(0).strip("的一是在了和与及中")
        if len(value) < 2 or value in seen or _looks_like_generic_context_word(value):
            continue
        seen.add(value)
        terms.append(value)
        if len(terms) >= 12:
            break
    return terms


def _looks_like_generic_context_word(value: str) -> bool:
    return value in {
        "这一章",
        "上一章",
        "下一章",
        "角色",
        "人物",
        "场景",
        "地点",
        "发现",
        "进入",
        "离开",
        "来到",
        "看见",
        "继续",
        "开始",
        "结束",
    }


def _render_usage_protocol() -> str:
    fallback = (
        "以下内容是角色连续性参考，不是本章任务清单；不要逐条复述，也不要为使用这些信息强行安排情节。"
        "章节承接状态是硬约束：下一章开头必须承接上一章结尾；若跳时空，需要先交代过渡。"
        "硬边界用于避免逻辑越界；软倾向只影响选择风格；可变状态可在本章新证据刺激下自然更新。"
        "默认按用户目标控制篇幅，本轮压力测试以约2500字/章为目标；超过3000字应主动收束场景。"
        "避免复用高频模板句，如没有说话、没有回答、声音很轻、深吸一口气、沉默了几秒、像是等。"
    )
    return resolve_prompt(
        "plugin.world_evolution_core.context-usage-protocol",
        {},
        fallback_system="你是 Evolution 上下文压缩器。",
        fallback_user=fallback,
    ).user


def _render_route_board(route_map: Optional[dict[str, Any]]) -> str:
    if not isinstance(route_map, dict):
        return ""
    aggregate = route_map.get("aggregate") if isinstance(route_map.get("aggregate"), dict) else {}
    edges = [item for item in route_map.get("edges") or [] if isinstance(item, dict)]
    conflicts = [item for item in route_map.get("conflicts") or [] if isinstance(item, dict)]
    meetings = [item for item in route_map.get("meetings") or [] if isinstance(item, dict)]
    if not edges and not conflicts:
        return ""
    lines = [
        f"已记录路线边 {aggregate.get('route_edge_count', len(edges))} 条、地点 {aggregate.get('location_count', 0)} 个、交汇 {aggregate.get('meeting_count', len(meetings))} 处。",
        "写下一章时必须先确认上一章人物终点；若改变地点，先写移动、跳时或视角桥接。",
    ]
    if edges:
        lines.append("【最近路线】")
        for edge in edges[-8:]:
            lines.append(
                f"- 第{edge.get('chapter_start')}章｜{edge.get('character')}："
                f"{edge.get('from_location') or '未知'} -> {edge.get('to_location') or '未知'}"
            )
    if meetings:
        lines.append("【路线交汇】")
        for meeting in meetings[-5:]:
            lines.append(f"- 第{meeting.get('chapter_number')}章｜{meeting.get('location')}：{'、'.join(_as_strings(meeting.get('characters')))}")
    if conflicts:
        lines.append("【需要审查的路线风险】")
        for conflict in conflicts[-6:]:
            lines.append(f"- {conflict.get('severity')}｜第{conflict.get('chapter_current')}章｜{conflict.get('message')}｜处理：{_route_conflict_guidance(conflict)}")
    return "\n".join(lines)


def _route_conflict_guidance(conflict: dict[str, Any]) -> str:
    conflict_type = str(conflict.get("type") or "")
    if conflict_type == "repeated_arrival":
        return "承接在场状态；若重新进入，先写离开和再次抵达。"
    if conflict_type == "location_jump_without_bridge":
        return "补一句路线、时间消耗、跳时提示或视角桥接。"
    if conflict_type == "boundary_rollback":
        return "核对上一章终点，不要无解释回到旧状态。"
    return "补足移动链或状态解释。"


def _render_semantic_memory_board(memory: Optional[dict[str, Any]]) -> str:
    if not isinstance(memory, dict):
        return ""
    items = [item for item in memory.get("items") or [] if isinstance(item, dict)]
    if not items:
        return ""
    source = str(memory.get("source") or "local")
    lines = [
        f"来源：{source}。以下为本地知识库/向量库按本章大纲召回的相关事实，只作为连续性证据，不要求逐条复述。"
    ]
    for item in items[:8]:
        chapter = item.get("chapter_number")
        chapter_label = f"第{chapter}章｜" if chapter else ""
        score = item.get("score")
        score_label = f"｜score={float(score):.2f}" if isinstance(score, (int, float)) else ""
        text = _clean_display_text(str(item.get("text") or ""))
        if text:
            lines.append(f"- {chapter_label}{text}{score_label}")
    return "\n".join(lines)


def _render_style_repetition_board(state: Optional[dict[str, Any]]) -> str:
    if not isinstance(state, dict):
        return ""
    phrases = [item for item in state.get("phrases") or [] if isinstance(item, dict)]
    if not phrases:
        return ""
    lines = ["重复表达规避：近3章/本章检测到高频反应模板；下一章优先改用动作、视线、空间调度、物件互动，不要继续机械复用。"]
    for item in phrases[:6]:
        phrase = _clean_display_text(item.get("phrase") or "")
        count = item.get("count")
        guidance = _clean_display_text(item.get("replacement_guidance") or "")
        if phrase:
            lines.append(f"- 「{phrase}」出现{count}次：{guidance or '换成具体行为或场景推进。'}")
    return "\n".join(lines)


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _render_focus_characters(characters: list[dict[str, Any]]) -> str:
    lines = []
    for card in characters:
        latest = (card.get("recent_events") or [])[-1] if card.get("recent_events") else {}
        summary = _clean_display_text(latest.get("summary") or "暂无近期动态")
        reasons = "、".join((card.get("injection_relevance") or {}).get("reasons") or [])
        reason_suffix = f"；相关性：{reasons}" if reasons else ""
        life_parts = _render_life_parts(card)
        life_suffix = f"；{life_parts}" if life_parts else ""
        lines.append(
            f"- {card.get('name')}：状态 {card.get('status') or 'active'}；首次第{card.get('first_seen_chapter')}章，最近第{card.get('last_seen_chapter')}章；{summary}{life_suffix}{reason_suffix}"
        )
    return "\n".join(lines)



def _render_life_parts(card: dict[str, Any]) -> str:
    parts: list[str] = []
    cognitive = card.get("cognitive_state") or {}
    known = _join_limited(cognitive.get("known_facts"), 2)
    unknowns = _join_limited(cognitive.get("unknowns"), 2)
    misbeliefs = _join_limited(cognitive.get("misbeliefs"), 1)
    hard: list[str] = []
    soft: list[str] = []
    mutable: list[str] = []
    if known:
        hard.append(f"已知={known}")
    if unknowns:
        hard.append(f"未知={unknowns}")
    if misbeliefs:
        mutable.append(f"误判={misbeliefs}")
    emotional = (card.get("emotional_arc") or [])[-1:]
    if emotional:
        item = emotional[0]
        emotion = item.get("emotion") or ""
        change = item.get("inner_change") or ""
        if emotion or change:
            mutable.append(f"心路={_clean_display_text(emotion)}{'，' if emotion and change else ''}{_clean_display_text(change)}")
    growth = card.get("growth_arc") or {}
    if growth.get("stage") and growth.get("stage") != "未定":
        mutable.append(f"成长阶段={_clean_display_text(growth.get('stage'))}")
    latest_growth = (growth.get("changes") or [])[-1:]
    if latest_growth:
        mutable.append(f"成长变化={_clean_display_text(latest_growth[0].get('summary'))}")
    limits = _join_limited(card.get("capability_limits"), 2)
    if limits:
        hard.append(f"能力边界={limits}")
    biases = _join_limited(card.get("decision_biases"), 2)
    if biases:
        soft.append(f"决策倾向={biases}")
    if hard:
        parts.append("硬边界（不可无过渡违反）：" + "；".join(hard))
    if soft:
        parts.append("软倾向（可被情境改变）：" + "；".join(soft))
    if mutable:
        parts.append("可变状态（允许随新证据更新）：" + "；".join(mutable))
    appearance = _render_appearance_brief(card.get("appearance"))
    if appearance:
        parts.append("外貌/出场识别：" + appearance)
    attributes = _render_record_brief(card.get("attributes"), 3)
    world_fields = _render_record_brief((card.get("world_profile") or {}).get("fields"), 3)
    if attributes or world_fields:
        parts.append("属性/世界观字段：" + "；".join(item for item in [attributes, world_fields] if item))
    palette = _render_palette_brief(card.get("personality_palette"))
    if palette:
        parts.append("性格调色盘：" + palette)
    return "；".join(part for part in parts if part)


def _join_limited(values: Any, limit: int) -> str:
    if not isinstance(values, list):
        return ""
    return "、".join(_clean_display_text(str(item)) for item in values[-limit:] if str(item).strip())


def _render_appearance_brief(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    items = []
    summary = _clean_display_text(value.get("summary") or "")
    if summary and "待从正文补充" not in summary:
        items.append(summary)
    outfit = _clean_display_text(value.get("current_outfit") or "")
    if outfit:
        items.append(f"当前装束={outfit}")
    features = _join_limited(value.get("features"), 2)
    if features:
        items.append(f"特征={features}")
    return "；".join(items[:3])


def _render_record_brief(records: Any, limit: int) -> str:
    if not isinstance(records, list):
        return ""
    parts = []
    for item in records[:limit]:
        if not isinstance(item, dict):
            continue
        name = _clean_display_text(item.get("name") or "")
        value = _clean_display_text(item.get("value") or "")
        if name and value:
            parts.append(f"{name}={value}")
    return "、".join(parts)


def _render_palette_brief(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    base = _clean_display_text(value.get("base") or "")
    if base:
        parts.append(f"底色={base}")
    presence_mode = _clean_display_text(value.get("presence_mode") or "")
    if presence_mode and presence_mode != "active_scene":
        parts.append(f"在场模式={_presence_mode_label(presence_mode)}")
    pressure = _join_limited(value.get("pressure_triggers"), 1)
    if pressure:
        parts.append(f"本章压力={pressure}")
    relationship = _render_relationship_tone(value.get("relationship_tones"))
    if relationship:
        parts.append(f"关系反应={relationship}")
    signature = _join_limited(value.get("voice_signature"), 1) or _join_limited(value.get("gesture_signature"), 1)
    if signature:
        parts.append(f"声线/动作锚点={signature}")
    costs = _join_limited(value.get("negative_costs"), 1)
    if costs:
        parts.append(f"禁止突变点={costs}")
    main = _join_limited(value.get("main_tones"), 2)
    if main:
        parts.append(f"主色调={main}")
    accents = _join_limited(value.get("accents"), 1)
    if accents:
        parts.append(f"点缀={accents}")
    derivatives = value.get("derivatives") if isinstance(value.get("derivatives"), list) else []
    if derivatives:
        descriptions = []
        for item in derivatives[:1]:
            if isinstance(item, dict) and item.get("description"):
                prefix = _clean_display_text(item.get("tone") or item.get("title") or "衍生")
                descriptions.append(f"{prefix}:{_clean_display_text(item.get('description'))}")
        if descriptions:
            parts.append("行为衍生=" + " / ".join(descriptions))
    return "；".join(parts)


def _render_relationship_tone(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value[:1]:
        if isinstance(item, dict):
            target = _clean_display_text(item.get("target") or "相关对象")
            tone = _clean_display_text(item.get("tone") or "")
            behavior = _clean_display_text(item.get("behavior") or "")
            if tone or behavior:
                parts.append(f"{target}:{tone or behavior}")
        elif str(item or "").strip():
            parts.append(_clean_display_text(item))
    return "、".join(parts)


def _presence_mode_label(value: str) -> str:
    return {
        "remote": "远端",
        "memory_trace": "记忆痕迹",
        "record_only": "记录/遗留信息",
        "system_entity": "系统型实体",
    }.get(value, value)


def _render_background_constraints(characters: list[dict[str, Any]]) -> str:
    lines = []
    for card in characters:
        latest = (card.get("recent_events") or [])[-1] if card.get("recent_events") else {}
        summary = _clean_display_text(latest.get("summary") or "暂无近期动态")
        reasons = "、".join((card.get("injection_relevance") or {}).get("reasons") or [])
        lines.append(f"- {card.get('name')}：与本章有背景关联（{reasons or '地点/物件相关'}），只作为连续性约束；不要因此强制安排出场。近期状态：{summary}")
    return "\n".join(lines)


def _clean_display_text(value: str) -> str:
    text = str(value or "")
    return text.replace("《", "").replace("》", "")


def _render_facts(facts: list[dict[str, Any]]) -> str:
    lines = []
    for fact in facts:
        locations = "、".join(fact.get("locations") or [])
        location_suffix = f" 地点：{locations}" if locations else ""
        summary = _clean_display_text(fact.get("summary") or "")[:220]
        lines.append(f"- 第{fact.get('chapter_number')}章：{summary}{location_suffix}")
    return "\n".join(lines)


def _render_state_board(chapter_summaries: list[dict[str, Any]], volume_summaries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if volume_summaries:
        volume = volume_summaries[-1]
        volume_text = _clean_display_text(volume.get("short_summary") or "")[-420:]
        open_threads = _join_limited(volume.get("open_threads"), 4)
        lines.append(f"最近10章大总结（第{volume.get('chapter_start')}-{volume.get('chapter_end')}章）：{volume_text}")
        if open_threads:
            lines.append(f"大线索未决：{open_threads}")
    if chapter_summaries:
        latest = chapter_summaries[-1]
        carry = latest.get("carry_forward") if isinstance(latest.get("carry_forward"), dict) else {}
        ending = latest.get("ending_state") if isinstance(latest.get("ending_state"), dict) else {}
        lines.append(f"上一章小总结（第{latest.get('chapter_number')}章）：{_clean_display_text(latest.get('short_summary') or '')[:360]}")
        time = _clean_display_text(carry.get("last_known_time") or "")
        locations = _join_limited(carry.get("last_known_locations"), 4)
        characters = _join_limited(carry.get("onscreen_characters"), 6)
        object_states = _render_object_states(carry.get("object_states"))
        open_threads = _join_limited(carry.get("open_threads"), 3)
        pieces = []
        if time:
            pieces.append(f"时间={time}")
        if locations:
            pieces.append(f"地点={locations}")
        if characters:
            pieces.append(f"在场/相关角色={characters}")
        if object_states:
            pieces.append(f"物件状态={object_states}")
        if open_threads:
            pieces.append(f"未决问题={open_threads}")
        ending_excerpt = _clean_display_text(ending.get("excerpt") or "")[:220]
        lines.append("上一章结尾状态：" + ("；".join(pieces) if pieces else "未抽取到明确时间/地点/物件，请优先根据上一章结尾证据承接。"))
        if ending_excerpt:
            lines.append(f"结尾证据：{ending_excerpt}")
        lines.append(str(carry.get("required_next_bridge") or "下一章开头必须承接上一章结尾，避免状态重置。"))
    if len(chapter_summaries) > 1:
        recent = []
        for item in chapter_summaries[-3:-1]:
            recent.append(f"第{item.get('chapter_number')}章：{_clean_display_text(item.get('short_summary') or '')[:180]}")
        if recent:
            lines.append("近期小总结：" + " / ".join(recent))
    return "\n".join(line for line in lines if line)


def _render_object_states(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        obj = _clean_display_text(item.get("object") or "")
        snippet = _clean_display_text(item.get("snippet") or "")
        if obj and snippet:
            parts.append(f"{obj}:{snippet[:80]}")
    return "；".join(parts)


def _build_risks(characters: list[dict[str, Any]], facts: list[dict[str, Any]], background_characters: Optional[list[dict[str, Any]]] = None) -> list[str]:
    risks: list[str] = []
    if characters:
        stale = [card for card in characters if int(card.get("last_seen_chapter") or 0) < int((facts[-1] or {}).get("chapter_number") or 0) - 5] if facts else []
        if stale:
            names = "、".join(str(card.get("name")) for card in stale[:4])
            risks.append(f"这些角色较久未更新，重新登场前建议交代状态：{names}")
        missing_palette = [card for card in characters if _palette_missing(card.get("personality_palette"))]
        if missing_palette:
            names = "、".join(str(card.get("name")) for card in missing_palette[:4])
            risks.append(f"以下重点角色性格调色盘不完整：{names}。不要只写性格标签，先用动作、选择和关系反应推断底色/主色调。")
    if background_characters:
        names = "、".join(str(card.get("name")) for card in background_characters[:4])
        risks.append(f"以下近期角色未被本章大纲明确召回，保持离场/远端状态；除非剧情需要，不要强行安排出场：{names}")
    if facts:
        latest = facts[-1]
        if not latest.get("locations"):
            risks.append("最近章节缺少明确地点，下一章生成前建议确认场景位置。")
    return risks[:4]


def _palette_missing(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    return not str(value.get("base") or "").strip() or not value.get("main_tones") or not value.get("derivatives")
