"""Budget-friendly context patch builder for Evolution World."""
from __future__ import annotations

from typing import Any, Optional

PLUGIN_NAME = "evolution_world_assistant"


def build_context_patch(
    novel_id: str,
    chapter_number: Optional[int],
    characters: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    outline: str = "",
    max_characters: int = 8,
    max_facts: int = 5,
) -> dict[str, Any]:
    recent_facts = facts[-max_facts:]
    selection = _select_characters(characters, max_characters, outline=outline, recent_facts=recent_facts)
    selected_characters = selection["selected"]
    background_characters = selection["background"]
    blocks = []

    if selected_characters:
        blocks.append(
            {
                "id": "dynamic_characters",
                "title": "动态角色状态",
                "kind": "character_state",
                "priority": 72,
                "token_budget": 420,
                "content": _render_characters(selected_characters),
                "items": selected_characters,
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

    risks = _build_risks(selected_characters, recent_facts, background_characters)
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

    return {
        "plugin_name": PLUGIN_NAME,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "schema_version": 1,
        "merge_strategy": "append_by_priority",
        "blocks": blocks,
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
        selected = [item[-1] for item in scored if item[0] >= 35][:limit]
        background = [item[-1] for item in scored if item[-1] not in selected and item[0] > 0]
        return {"selected": selected, "background": background[:4]}
    return {"selected": [item[-1] for item in scored[:limit]], "background": []}


def _extract_context_terms(text: str) -> list[str]:
    terms: list[str] = []
    for marker in ["黑塔", "雾城", "星港", "城门", "钥匙", "罗盘", "白鸦", "旧案", "密门"]:
        if marker in text:
            terms.append(marker)
    return terms


def _render_characters(characters: list[dict[str, Any]]) -> str:
    lines = []
    for card in characters:
        latest = (card.get("recent_events") or [])[-1] if card.get("recent_events") else {}
        summary = latest.get("summary") or "暂无近期动态"
        lines.append(
            f"- {card.get('name')}：状态 {card.get('status') or 'active'}；首次第{card.get('first_seen_chapter')}章，最近第{card.get('last_seen_chapter')}章；{summary}"
        )
    return "\n".join(lines)


def _render_facts(facts: list[dict[str, Any]]) -> str:
    lines = []
    for fact in facts:
        locations = "、".join(fact.get("locations") or [])
        location_suffix = f" 地点：{locations}" if locations else ""
        lines.append(f"- 第{fact.get('chapter_number')}章：{fact.get('summary') or ''}{location_suffix}")
    return "\n".join(lines)


def _build_risks(characters: list[dict[str, Any]], facts: list[dict[str, Any]], background_characters: Optional[list[dict[str, Any]]] = None) -> list[str]:
    risks: list[str] = []
    if characters:
        stale = [card for card in characters if int(card.get("last_seen_chapter") or 0) < int((facts[-1] or {}).get("chapter_number") or 0) - 5] if facts else []
        if stale:
            names = "、".join(str(card.get("name")) for card in stale[:4])
            risks.append(f"这些角色较久未更新，重新登场前建议交代状态：{names}")
    if background_characters:
        names = "、".join(str(card.get("name")) for card in background_characters[:4])
        risks.append(f"以下近期角色未被本章大纲明确召回，除非剧情需要，不要强行安排出场：{names}")
    if facts:
        latest = facts[-1]
        if not latest.get("locations"):
            risks.append("最近章节缺少明确地点，下一章生成前建议确认场景位置。")
    return risks[:4]
