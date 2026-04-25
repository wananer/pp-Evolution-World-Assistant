"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage

from .context_patch import build_context_patch, render_patch_summary
from .preset_converter import convert_st_preset
from .repositories import RECENT_CONTEXT_FACT_LIMIT, EvolutionWorldRepository
from .structured_extractor import StructuredExtractorProvider, extract_structured_chapter_facts

PLUGIN_NAME = "world_evolution_core"


class EvolutionWorldAssistantService:
    def __init__(
        self,
        storage: Optional[PluginStorage] = None,
        jobs: Optional[PluginJobRegistry] = None,
        repository: Optional[EvolutionWorldRepository] = None,
        extractor_provider: Optional[StructuredExtractorProvider] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)
        self.repository = repository or EvolutionWorldRepository(self.storage)
        self.extractor_provider = extractor_provider

    async def after_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id or not chapter_number or not content:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number/content"}

        started_at = _now()
        start_time = perf_counter()
        trigger_type = str(payload.get("trigger_type") or "auto")
        content_hash = str(payload.get("content_hash") or _hash_text(content))
        dedup_key = self.jobs.build_dedup_key(
            PLUGIN_NAME,
            "after_commit",
            novel_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            trigger_type=trigger_type,
        )
        extraction = await extract_structured_chapter_facts(
            novel_id,
            chapter_number,
            content_hash,
            content,
            _now(),
            provider=self.extractor_provider,
        )
        snapshot = extraction.snapshot
        known_names = [card.get("name") for card in self.repository.list_character_index(novel_id).get("items", [])]
        for name in known_names:
            if name and name in content and name not in snapshot.characters:
                snapshot.characters.append(name)
        previous_snapshot = self.repository.get_fact_snapshot(novel_id, chapter_number)
        self.repository.save_fact_snapshot(snapshot)
        updated_cards = self.repository.upsert_character_cards(
            novel_id,
            snapshot,
            [item.to_dict() for item in extraction.character_updates],
        )
        finished_at = _now()
        duration_ms = int((perf_counter() - start_time) * 1000)
        self.repository.append_event(
            novel_id,
            {"type": "chapter_committed", "chapter_number": chapter_number, "content_hash": content_hash, "at": finished_at},
        )
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"{chapter_number}-{content_hash}-{trigger_type}",
                "hook_name": "after_commit",
                "trigger_type": trigger_type,
                "chapter_number": chapter_number,
                "content_hash": content_hash,
                "status": "succeeded",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "input": {"content_length": len(content)},
                "output": {
                    "characters": snapshot.characters,
                    "locations": snapshot.locations,
                    "world_events": snapshot.world_events,
                    "extraction_source": extraction.source,
                    "warnings": extraction.warnings,
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                    "replaced_existing_snapshot": bool(previous_snapshot),
                },
            },
        )
        self.jobs.append(
            PluginJobRecord(
                plugin_name=PLUGIN_NAME,
                hook_name="after_commit",
                novel_id=novel_id,
                chapter_number=chapter_number,
                trigger_type=trigger_type,
                dedup_key=dedup_key,
                content_hash=content_hash,
                status="succeeded",
                input_json={"chapter_number": chapter_number},
                output_json={
                    "facts_path": f"facts/chapter_{chapter_number}.json",
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                },
            )
        )
        return {"ok": True, "data": {"facts": snapshot.to_dict(), "characters_updated": updated_cards, "extraction": extraction.to_dict()}}

    def before_context_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        outline = str((payload.get("payload") or {}).get("outline") or payload.get("outline") or "")
        patch = self.build_context_patch(novel_id, chapter_number, outline=outline)
        summary = render_patch_summary(patch)
        if not summary:
            return {"ok": True, "skipped": True, "reason": "no evolution state yet"}

        return {
            "ok": True,
            "context_patch": patch,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution World State",
                    "content": summary,
                    "priority": 60,
                    "token_budget": patch.get("estimated_token_budget") or 1200,
                    "metadata": {"novel_id": novel_id, "chapter_number": chapter_number, "patch_schema_version": patch.get("schema_version")},
                }
            ],
        }

    async def manual_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapters = payload.get("chapters") or []
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        if not isinstance(chapters, list) or not chapters:
            cards = self.repository.rebuild_character_cards_from_facts(novel_id)
            self.repository.append_workflow_run(
                novel_id,
                {
                    "run_id": f"rebuild-existing-{_hash_text(_now())}",
                    "hook_name": "manual_rebuild",
                    "trigger_type": "manual",
                    "status": "succeeded",
                    "started_at": _now(),
                    "finished_at": _now(),
                    "input": {"mode": "existing_facts"},
                    "output": {"characters_rebuilt": len(cards)},
                },
            )
            return {"ok": True, "data": {"novel_id": novel_id, "mode": "existing_facts", "characters_rebuilt": len(cards)}}

        rebuilt = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            result = await self.after_commit(
                {
                    "novel_id": novel_id,
                    "chapter_number": chapter.get("chapter_number") or chapter.get("number"),
                    "trigger_type": "manual_rebuild",
                    "payload": {"content": chapter.get("content") or ""},
                }
            )
            if result.get("ok") and not result.get("skipped"):
                rebuilt.append(result["data"]["facts"]["chapter_number"])
        cards = self.repository.rebuild_character_cards_from_facts(novel_id)
        return {"ok": True, "data": {"novel_id": novel_id, "rebuilt_chapters": rebuilt, "characters_rebuilt": len(cards)}}

    async def rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id or not chapter_number:
            return {"ok": False, "error": "missing novel_id/chapter_number"}

        removed = self.repository.delete_fact_snapshot(novel_id, chapter_number)
        cards = self.repository.rebuild_character_cards_from_facts(novel_id)
        event = {
            "type": "chapter_rollback",
            "chapter_number": chapter_number,
            "removed_snapshot": removed,
            "characters_rebuilt": len(cards),
            "at": _now(),
        }
        self.repository.append_event(novel_id, event)
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"rollback-{chapter_number}-{_hash_text(_now())}",
                "hook_name": "rollback",
                "trigger_type": str(payload.get("trigger_type") or "manual"),
                "chapter_number": chapter_number,
                "status": "succeeded",
                "started_at": event["at"],
                "finished_at": _now(),
                "input": {"chapter_number": chapter_number},
                "output": {"removed_snapshot": removed, "characters_rebuilt": len(cards)},
            },
        )
        return {"ok": True, "data": {"novel_id": novel_id, "chapter_number": chapter_number, "removed_snapshot": removed, "characters_rebuilt": len(cards)}}

    def import_st_preset(self, novel_id: str, preset: dict[str, Any]) -> dict[str, Any]:
        converted = convert_st_preset(preset)
        self.repository.save_imported_flows(novel_id, converted)
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"import-flows-{_hash_text(_now())}",
                "hook_name": "import_st_preset",
                "trigger_type": "manual",
                "status": "succeeded",
                "started_at": _now(),
                "finished_at": _now(),
                "input": {"source": converted.get("source")},
                "output": {"flows_imported": len(converted.get("flows") or []), "unsupported": converted.get("unsupported") or []},
            },
        )
        return {"ok": True, "data": converted}

    def list_imported_flows(self, novel_id: str) -> dict[str, Any]:
        return self.repository.list_imported_flows(novel_id)

    def list_runs(self, novel_id: str, limit: int = 50) -> dict[str, Any]:
        return {"items": self.repository.list_workflow_runs(novel_id, limit=limit)}

    def list_events(self, novel_id: str) -> dict[str, Any]:
        return {"items": self.repository.list_events(novel_id)}

    def list_snapshots(self, novel_id: str) -> dict[str, Any]:
        return {"items": self.repository.list_fact_snapshots(novel_id)}

    def list_characters(self, novel_id: str) -> dict[str, Any]:
        return self.repository.list_character_cards(novel_id)

    def get_character(self, novel_id: str, character_id: str) -> Optional[dict[str, Any]]:
        return self.repository.get_character_card(novel_id, character_id)

    def list_character_timeline(self, novel_id: str, character_id: str) -> dict[str, Any]:
        card = self.get_character(novel_id, character_id)
        if not card:
            return {"items": []}
        return {"character": card, "items": card.get("recent_events", [])}

    def review_chapter(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id or not chapter_number or not content:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number/content"}

        cards = self.repository.list_relevant_character_cards(novel_id, content).get("items", [])
        facts = self.repository.list_fact_snapshots(
            novel_id,
            before_chapter=chapter_number,
            limit=RECENT_CONTEXT_FACT_LIMIT,
        )
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []

        mentioned_cards = [card for card in cards if _character_is_mentioned(card, content)]
        for card in mentioned_cards:
            issues.extend(_review_character_card_against_content(card, content, chapter_number))

        recent_characters = _recent_fact_characters(facts, limit=3)
        mentioned_names = {str(card.get("name") or "") for card in mentioned_cards}
        offstage_mentions = [name for name in recent_characters if name and name in content and name not in mentioned_names]
        if offstage_mentions:
            issues.append(
                _review_issue(
                    "evolution_plot_continuity",
                    "suggestion",
                    f"本章提到近期角色 {', '.join(offstage_mentions[:4])}，但未找到对应人物卡或别名匹配。",
                    chapter_number,
                    "如该角色实际出场，请先让章节提交/重建生成人物卡；如只是背景信息，避免写成已在场行动。",
                )
            )

        if issues:
            suggestions.append("Evolution 建议优先补足角色得知信息、能力越界或误信被修正的过渡，而不是直接删除剧情推进。")

        return {
            "ok": True,
            "data": {
                "issues": issues,
                "suggestions": suggestions,
                "reviewed_characters": [card.get("name") for card in mentioned_cards],
            },
        }

    def build_context_patch(self, novel_id: str, chapter_number: Optional[int], *, outline: str = "") -> dict[str, Any]:
        facts = self.repository.list_fact_snapshots(
            novel_id,
            before_chapter=chapter_number,
            limit=RECENT_CONTEXT_FACT_LIMIT,
        )
        characters = self.repository.list_relevant_character_cards(novel_id, outline).get("items", [])
        return build_context_patch(novel_id, chapter_number, characters, facts, outline=outline)

    def build_context_summary(self, novel_id: str, chapter_number: Optional[int], *, outline: str = "") -> str:
        return render_patch_summary(self.build_context_patch(novel_id, chapter_number, outline=outline))



def _character_is_mentioned(card: dict[str, Any], content: str) -> bool:
    names = [card.get("name"), *(card.get("aliases") or [])]
    return any(str(name or "").strip() and str(name).strip() in content for name in names)


def _review_character_card_against_content(card: dict[str, Any], content: str, chapter_number: int) -> list[dict[str, Any]]:
    name = str(card.get("name") or "角色").strip()
    issues: list[dict[str, Any]] = []
    cognitive = card.get("cognitive_state") if isinstance(card.get("cognitive_state"), dict) else {}
    for unknown in _as_strings(cognitive.get("unknowns")):
        if _looks_resolved_without_transition(content, unknown):
            issues.append(
                _review_issue(
                    "evolution_character_cognition",
                    "warning",
                    f"{name} 在人物卡中仍标记为未知：{unknown}，但本章像是直接知道/利用了该信息。",
                    chapter_number,
                    "补充他如何得知、推断或误判这条信息；如果只是猜测，请在文本中保留不确定性。",
                )
            )
    for misbelief in _as_strings(cognitive.get("misbeliefs")):
        if _mentions_key_terms(content, misbelief) and not _has_transition_marker(content):
            issues.append(
                _review_issue(
                    "evolution_character_belief",
                    "suggestion",
                    f"{name} 仍有未修正误信：{misbelief}，本章相关表述需要交代误信是否被打破。",
                    chapter_number,
                    "写出证据、挫败或他人的告知，让认知变化成为剧情事件，而不是静默切换。",
                )
            )
    for limit in _as_strings(card.get("capability_limits")):
        if _mentions_key_terms(content, limit) and _has_mastery_marker(content) and not _has_transition_marker(content):
            issues.append(
                _review_issue(
                    "evolution_character_capability",
                    "warning",
                    f"{name} 的能力边界是：{limit}，但本章呈现为直接突破或熟练解决。",
                    chapter_number,
                    "增加试错、代价、外部帮助或失败风险；避免把能力边界写成突然全知全能。",
                )
            )
    if _has_all_knowing_marker(content) and (_as_strings(cognitive.get("unknowns")) or _as_strings(card.get("capability_limits"))):
        issues.append(
            _review_issue(
                "evolution_character_logic",
                "suggestion",
                f"{name} 本章语气接近全知判断，但人物卡仍存在未知或能力边界。",
                chapter_number,
                "将确定判断改为观察、推断、误判或带代价的验证，让角色认知随证据成长。",
            )
        )
    return issues


def _review_issue(issue_type: str, severity: str, description: str, chapter_number: int, suggestion: str) -> dict[str, Any]:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "location": f"Chapter {chapter_number}",
        "suggestion": suggestion,
    }


def _recent_fact_characters(facts: list[dict[str, Any]], *, limit: int) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for fact in reversed(facts[-limit:]):
        for name in fact.get("characters") or []:
            value = str(name or "").strip()
            if value and value not in seen:
                seen.add(value)
                names.append(value)
    return names


def _as_strings(items: Any) -> list[str]:
    return [str(item or "").strip() for item in (items or []) if str(item or "").strip()]


def _mentions_key_terms(content: str, phrase: str) -> bool:
    terms = [term for term in _split_terms(phrase) if len(term) >= 2]
    terms.extend(_semantic_terms(phrase))
    terms = list(dict.fromkeys(terms))
    if not terms:
        return phrase in content
    if any(len(term) >= 4 and term in content for term in terms):
        return True
    matches = sum(1 for term in terms if term in content)
    return matches >= min(2, len(terms))


def _semantic_terms(phrase: str) -> list[str]:
    cleaned = phrase
    for marker in ("不能", "无法", "不会", "不知", "不知道", "凭空", "直接", "轻易", "所有"):
        cleaned = cleaned.replace(marker, "")
    return [cleaned[index : index + 4] for index in range(0, max(len(cleaned) - 3, 0)) if cleaned[index : index + 4].strip()]


def _looks_resolved_without_transition(content: str, unknown: str) -> bool:
    return _mentions_key_terms(content, unknown) and _has_knowledge_marker(content) and not _has_transition_marker(content)


def _split_terms(text: str) -> list[str]:
    separators = "，。；、：:（）()【】[]《》 \n\t"
    current = text
    for sep in separators:
        current = current.replace(sep, "|")
    terms = []
    for part in current.split("|"):
        part = part.strip()
        if not part:
            continue
        if len(part) > 8:
            terms.extend(part[index : index + 4] for index in range(0, len(part), 4))
        else:
            terms.append(part)
    return terms


def _has_knowledge_marker(content: str) -> bool:
    markers = ["知道", "明白", "清楚", "意识到", "看穿", "断定", "确定", "早就", "原来"]
    return any(marker in content for marker in markers)


def _has_mastery_marker(content: str) -> bool:
    markers = ["轻易", "立刻", "毫不费力", "随手", "直接", "精准", "完全", "熟练", "一眼", "看穿"]
    return any(marker in content for marker in markers)


def _has_all_knowing_marker(content: str) -> bool:
    markers = ["一切都在", "早已算到", "全都知道", "早就知道", "毫无疑问", "不用验证"]
    return any(marker in content for marker in markers)


def _has_transition_marker(content: str) -> bool:
    markers = [
        "发现",
        "意识到",
        "终于明白",
        "从",
        "得知",
        "听见",
        "看见",
        "试探",
        "验证",
        "推断",
        "猜测",
        "误以为",
        "代价",
        "失败",
        "受伤",
        "请教",
        "提醒",
        "线索",
        "证据",
    ]
    return any(marker in content for marker in markers)

def _extract_content(payload: dict[str, Any]) -> str:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return str(payload.get("content") or nested.get("content") or nested.get("chapter_content") or "").strip()


def _hash_text(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()[:16]


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
