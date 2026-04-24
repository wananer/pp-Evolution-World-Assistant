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
    max_characters: int = 8,
    max_facts: int = 5,
) -> dict[str, Any]:
    selected_characters = _select_characters(characters, max_characters)
    recent_facts = facts[-max_facts:]
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

    risks = _build_risks(selected_characters, recent_facts)
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


def _select_characters(characters: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    def sort_key(card: dict[str, Any]):
        return (-(int(card.get("last_seen_chapter") or 0)), int(card.get("first_seen_chapter") or 0), str(card.get("name") or ""))

    return sorted(characters, key=sort_key)[:limit]


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


def _build_risks(characters: list[dict[str, Any]], facts: list[dict[str, Any]]) -> list[str]:
    risks: list[str] = []
    if characters:
        stale = [card for card in characters if int(card.get("last_seen_chapter") or 0) < int((facts[-1] or {}).get("chapter_number") or 0) - 5] if facts else []
        if stale:
            names = "、".join(str(card.get("name")) for card in stale[:4])
            risks.append(f"这些角色较久未更新，重新登场前建议交代状态：{names}")
    if facts:
        latest = facts[-1]
        if not latest.get("locations"):
            risks.append("最近章节缺少明确地点，下一章生成前建议确认场景位置。")
    return risks[:4]
