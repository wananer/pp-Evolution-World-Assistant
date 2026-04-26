"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

import asyncio
import copy
import threading
from datetime import datetime, timezone
from time import perf_counter
from hashlib import sha256
from typing import Any, Optional, Union, Tuple

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage

from .continuity import build_chapter_summary, build_volume_summary
from .context_capsules import build_injection_record
from .context_patch import build_context_patch, render_patch_summary
from .preset_converter import convert_st_preset
from .repositories import RECENT_CONTEXT_FACT_LIMIT, EvolutionWorldRepository
from .structured_extractor import StructuredExtractorProvider, extract_structured_chapter_facts

PLUGIN_NAME = "world_evolution_core"
API2_PROVIDER_MODES = {"same_as_main", "custom"}
API2_PROTOCOLS = {"openai", "anthropic", "gemini"}


class EvolutionWorldAssistantService:
    def __init__(
        self,
        storage: Optional[PluginStorage] = None,
        jobs: Optional[PluginJobRegistry] = None,
        repository: Optional[EvolutionWorldRepository] = None,
        extractor_provider: Optional[StructuredExtractorProvider] = None,
        api2_llm_service: Optional[Any] = None,
        llm_provider_factory: Optional[Any] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)
        self.repository = repository or EvolutionWorldRepository(self.storage)
        self.extractor_provider = extractor_provider
        self.api2_llm_service = api2_llm_service
        self.llm_provider_factory = llm_provider_factory

    def get_settings(self, *, safe: bool = True) -> dict[str, Any]:
        settings = _normalize_settings(self.repository.get_settings())
        return _redact_settings(settings) if safe else settings

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = _normalize_settings(self.repository.get_settings())
        settings = _normalize_settings(payload or {}, existing=existing)
        self.repository.save_settings(settings)
        return self.get_settings(safe=True)

    async def after_novel_created(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        meta = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        started_at = _now()
        title = str(meta.get("title") or "").strip()
        premise = str(meta.get("premise") or "").strip()
        genre = str(meta.get("genre") or "").strip()
        world_preset = str(meta.get("world_preset") or "").strip()
        style_hint = str(meta.get("style_hint") or "").strip()
        target_chapters = _int_or_none(meta.get("target_chapters"))
        length_tier = str(meta.get("length_tier") or "").strip()
        existing = self.repository.get_prehistory_worldline(novel_id)
        worldline = _build_prehistory_worldline(
            novel_id=novel_id,
            title=title,
            premise=premise,
            genre=genre,
            world_preset=world_preset,
            style_hint=style_hint,
            target_chapters=target_chapters,
            length_tier=length_tier,
            at=started_at,
        )
        self.repository.save_prehistory_worldline(novel_id, worldline)
        self.repository.append_event(
            novel_id,
            {
                "type": "prehistory_worldline_seeded",
                "horizon_years": worldline.get("depth", {}).get("horizon_years"),
                "era_count": len(worldline.get("eras") or []),
                "at": started_at,
            },
        )
        self.repository.append_workflow_run(
            novel_id,
            {
                "run_id": f"prehistory-{_hash_text(novel_id + started_at)}",
                "hook_name": "after_novel_created",
                "trigger_type": str(payload.get("trigger_type") or "novel_create"),
                "status": "succeeded",
                "started_at": started_at,
                "finished_at": _now(),
                "input": {
                    "title": title,
                    "genre": genre,
                    "world_preset": world_preset,
                    "style_hint": style_hint,
                    "target_chapters": target_chapters,
                    "length_tier": length_tier,
                },
                "output": {
                    "horizon_years": worldline.get("depth", {}).get("horizon_years"),
                    "era_count": len(worldline.get("eras") or []),
                    "foreshadow_seed_count": len(worldline.get("foreshadow_seeds") or []),
                    "replaced_existing_worldline": bool(existing),
                },
            },
        )
        return {"ok": True, "data": {"worldline": worldline, "replaced_existing_worldline": bool(existing)}}

    def before_story_planning(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        purpose = str(nested.get("purpose") or payload.get("trigger_type") or "story_planning").strip()
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        evidence = self.repository.build_story_planning_evidence(novel_id, purpose=purpose)
        if not evidence:
            return {"ok": True, "skipped": True, "reason": "no prehistory worldline yet"}

        style_adapter = _build_runtime_style_adapter(evidence.get("worldline") or {}, nested)
        evidence["style_adapter"] = style_adapter
        content = _render_story_planning_evidence(evidence, style_adapter=style_adapter)
        return {
            "ok": True,
            "data": evidence,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution 故事前史与伏笔库",
                    "content": content,
                    "priority": 72,
                    "token_budget": 1600,
                    "metadata": {
                        "novel_id": novel_id,
                        "purpose": purpose,
                        "schema_version": evidence.get("worldline", {}).get("schema_version"),
                    },
                }
            ],
        }

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
        chapter_summary = build_chapter_summary(novel_id, chapter_number, content, _now())
        known_names = [card.get("name") for card in self.repository.list_character_index(novel_id).get("items", [])]
        for name in known_names:
            if name and name in content and name not in snapshot.characters:
                snapshot.characters.append(name)
        previous_snapshot = self.repository.get_fact_snapshot(novel_id, chapter_number)
        self.repository.save_fact_snapshot(snapshot)
        self.repository.save_chapter_summary(novel_id, chapter_number, chapter_summary)
        volume_summary = None
        if chapter_number % 10 == 0:
            volume_index = chapter_number // 10
            recent_summaries = self.repository.list_chapter_summaries(novel_id, limit=10)
            volume_summary = build_volume_summary(novel_id, volume_index, recent_summaries, _now())
            self.repository.save_volume_summary(novel_id, volume_index, volume_summary)
        updated_cards = self.repository.upsert_character_cards(
            novel_id,
            snapshot,
            [item.to_dict() for item in extraction.character_updates],
        )
        timeline_events = _build_timeline_events(snapshot, extraction.to_dict(), content_hash, _now())
        self.repository.save_timeline_events(novel_id, timeline_events)
        self.repository.save_continuity_constraints(
            novel_id,
            _build_continuity_constraints(novel_id, updated_cards, snapshot.chapter_number, timeline_events),
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
                    "chapter_summary_saved": True,
                    "volume_summary_saved": bool(volume_summary),
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
                    "summary_path": f"summaries/chapters/chapter_{chapter_number}.json",
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "facts": snapshot.to_dict(),
                "chapter_summary": chapter_summary,
                "volume_summary": volume_summary,
                "characters_updated": updated_cards,
                "extraction": extraction.to_dict(),
            },
        }

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

        content = summary
        title = "Evolution World State"
        metadata: dict[str, Any] = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "patch_schema_version": patch.get("schema_version"),
            "api2_control_card_enabled": False,
        }
        settings = self.get_settings(safe=False)
        api2_settings = settings.get("api2_control_card") if isinstance(settings.get("api2_control_card"), dict) else {}
        if api2_settings.get("enabled"):
            control_card = self._build_api2_control_card(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                raw_context=summary,
                settings=api2_settings,
            )
            if control_card.get("ok") and control_card.get("content"):
                content = str(control_card["content"]).strip()
                title = "Evolution 写作控制卡"
                metadata.update(
                    {
                        "api2_control_card_enabled": True,
                        "api2_provider_mode": api2_settings.get("provider_mode"),
                        "api2_raw_context_chars": len(summary),
                        "api2_control_card_chars": len(content),
                        "api2_compression_ratio": round(len(content) / max(len(summary), 1), 4),
                    }
                )
            else:
                metadata.update(
                    {
                        "api2_control_card_enabled": True,
                        "api2_error": control_card.get("error") or control_card.get("reason") or "unknown",
                    }
                )

        injection_record = build_injection_record(
            novel_id=novel_id,
            chapter_number=chapter_number,
            blocks=patch.get("blocks") or [],
            skipped_blocks=patch.get("skipped_blocks") or [],
            at=_now(),
        )
        self.repository.append_context_injection_record(novel_id, injection_record)

        return {
            "ok": True,
            "context_patch": patch,
            "context_injection_record": injection_record,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": title,
                    "content": content,
                    "priority": 60,
                    "token_budget": patch.get("estimated_token_budget") or 1200,
                    "metadata": metadata,
                }
            ],
        }

    def _build_api2_control_card(
        self,
        *,
        novel_id: str,
        chapter_number: Optional[int],
        outline: str,
        raw_context: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = _now()
        if not raw_context.strip():
            return {"ok": True, "skipped": True, "reason": "empty raw context"}
        try:
            llm_service = self._resolve_api2_llm_service(settings)
            prompt = _build_api2_control_card_prompt(
                chapter_number=chapter_number,
                outline=outline,
                raw_context=raw_context,
            )
            result = _run_async_blocking(
                llm_service.generate(
                    prompt,
                    _make_generation_config(
                        model=str(settings.get("model") or ""),
                        max_tokens=_clamp_int(settings.get("max_tokens"), 256, 4096, 1400),
                        temperature=_clamp_float(settings.get("temperature"), 0.0, 2.0, 0.2),
                    ),
                )
            )
            content = _clean_api2_control_card(result.content)
            if not content:
                return {"ok": False, "error": "api2 returned empty control card"}
            finished_at = _now()
            token_usage = getattr(result, "token_usage", None)
            record = {
                "at": finished_at,
                "started_at": started_at,
                "chapter_number": chapter_number,
                "provider_mode": settings.get("provider_mode"),
                "raw_context_chars": len(raw_context),
                "control_card_chars": len(content),
                "compression_ratio": round(len(content) / max(len(raw_context), 1), 4),
                "model": str(settings.get("model") or ""),
                "token_usage": _token_usage_to_dict(token_usage),
            }
            self.repository.append_context_control_card_record(novel_id, record)
            self.repository.append_event(
                novel_id,
                {
                    "type": "api2_control_card_built",
                    "chapter_number": chapter_number,
                    "raw_context_chars": len(raw_context),
                    "control_card_chars": len(content),
                    "at": finished_at,
                },
            )
            return {"ok": True, "content": content, "record": record}
        except Exception as exc:
            self.repository.append_event(
                novel_id,
                {
                    "type": "api2_control_card_failed",
                    "chapter_number": chapter_number,
                    "error": str(exc),
                    "at": _now(),
                },
            )
            return {"ok": False, "error": str(exc)}

    def _resolve_api2_llm_service(self, settings: dict[str, Any]) -> Any:
        if self.api2_llm_service is not None:
            return self.api2_llm_service
        if self.llm_provider_factory is None:
            from infrastructure.ai.provider_factory import LLMProviderFactory

            self.llm_provider_factory = LLMProviderFactory()
        provider_mode = str(settings.get("provider_mode") or "same_as_main")
        if provider_mode == "custom":
            from application.ai.llm_control_service import LLMProfile

            profile_payload = settings.get("custom_profile") if isinstance(settings.get("custom_profile"), dict) else {}
            profile = LLMProfile(**_custom_profile_for_llm(profile_payload))
            return self.llm_provider_factory.create_from_profile(profile)
        return self.llm_provider_factory.create_active_provider()

    def before_chapter_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        evidence = self.repository.build_review_evidence(novel_id, content, before_chapter=chapter_number)
        blocks = _build_review_context_blocks(evidence)
        if not blocks:
            return {"ok": True, "skipped": True, "reason": "no evolution review evidence yet"}
        return {
            "ok": True,
            "data": {
                "review_context_blocks": blocks,
                "evidence": evidence.get("events", []),
                "constraints": evidence.get("constraints", []),
                "characters": evidence.get("characters", []),
            },
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
        self.repository.delete_chapter_summary(novel_id, chapter_number)
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

    def list_timeline_events(self, novel_id: str, before_chapter: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
        return {"items": self.repository.list_timeline_events(novel_id, before_chapter=before_chapter, limit=limit)}

    def list_continuity_constraints(self, novel_id: str, limit: int = 80) -> dict[str, Any]:
        return {"items": self.repository.list_continuity_constraints(novel_id, limit=limit)}

    def list_review_records(self, novel_id: str, limit: int = 30) -> dict[str, Any]:
        return {"items": self.repository.list_review_records(novel_id, limit=limit)}

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

        evidence = self.repository.build_review_evidence(novel_id, content, before_chapter=chapter_number)
        cards = evidence.get("characters") or self.repository.list_relevant_character_cards(novel_id, content).get("items", [])
        facts = self.repository.list_fact_snapshots(
            novel_id,
            before_chapter=chapter_number,
            limit=RECENT_CONTEXT_FACT_LIMIT,
        )
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []

        mentioned_cards = [card for card in cards if _character_is_mentioned(card, content)]
        for card in mentioned_cards:
            issues.extend(
                _attach_issue_evidence(
                    _review_character_card_against_content(card, content, chapter_number),
                    evidence,
                    subject=str(card.get("name") or ""),
                )
            )

        recent_characters = _recent_fact_characters(facts, limit=3)
        mentioned_names = {str(card.get("name") or "") for card in mentioned_cards}
        offstage_mentions = [name for name in recent_characters if name and name in content and name not in mentioned_names]
        if offstage_mentions:
            issues.extend(
                _attach_issue_evidence(
                    [
                        _review_issue(
                            "evolution_plot_continuity",
                            "suggestion",
                            f"本章提到近期角色 {', '.join(offstage_mentions[:4])}，但未找到对应人物卡或别名匹配。",
                            chapter_number,
                            "如该角色实际出场，请先让章节提交/重建生成人物卡；如只是背景信息，避免写成已在场行动。",
                        )
                    ],
                    evidence,
                    subject=offstage_mentions[0],
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
                "evidence": evidence.get("events", []),
                "constraints": evidence.get("constraints", []),
            },
        }

    def after_chapter_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        review_result = (payload.get("payload") or {}).get("review_result") or {}
        if not novel_id or not chapter_number:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number"}
        issues = review_result.get("issues") or []
        self.repository.append_review_record(
            novel_id,
            {
                "chapter_number": chapter_number,
                "issue_count": len(issues) if isinstance(issues, list) else 0,
                "overall_score": review_result.get("overall_score"),
                "source": str(payload.get("source") or "chapter_review_service"),
                "at": _now(),
            },
        )
        return {"ok": True, "data": {"recorded": True, "chapter_number": chapter_number}}

    def build_context_patch(self, novel_id: str, chapter_number: Optional[int], *, outline: str = "") -> dict[str, Any]:
        facts = self.repository.list_fact_snapshots(
            novel_id,
            before_chapter=chapter_number,
            limit=RECENT_CONTEXT_FACT_LIMIT,
        )
        characters = self.repository.list_relevant_character_cards(novel_id, outline).get("items", [])
        chapter_summaries = self.repository.list_chapter_summaries(novel_id, before_chapter=chapter_number, limit=10)
        volume_summaries = self.repository.list_volume_summaries(novel_id, before_chapter=chapter_number, limit=3)
        previous_injections = self.repository.list_context_injection_records(novel_id, limit=20)
        return build_context_patch(
            novel_id,
            chapter_number,
            characters,
            facts,
            outline=outline,
            chapter_summaries=chapter_summaries,
            volume_summaries=volume_summaries,
            previous_injections=previous_injections,
        )

    def build_context_summary(self, novel_id: str, chapter_number: Optional[int], *, outline: str = "") -> str:
        return render_patch_summary(self.build_context_patch(novel_id, chapter_number, outline=outline))


def _build_prehistory_worldline(
    *,
    novel_id: str,
    title: str,
    premise: str,
    genre: str,
    world_preset: str,
    style_hint: str,
    target_chapters: Optional[int],
    length_tier: str,
    at: str,
) -> dict[str, Any]:
    profile = _select_worldline_profile(genre, world_preset, premise, target_chapters, length_tier)
    axes = _infer_story_axes(genre, world_preset, premise)
    style_adapter = _build_style_adapter(
        title=title,
        premise=premise,
        genre=genre,
        world_preset=world_preset,
        style_hint=style_hint,
    )
    forces = _build_world_forces(axes, profile)
    eras = _build_prehistory_eras(profile, axes, forces, title)
    seeds = _build_prehistory_foreshadow_seeds(profile, axes, forces)
    guidance = _build_prehistory_guidance(profile, axes)
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "title": title,
        "source": "deterministic_prehistory_generator",
        "created_at": at,
        "input_digest": _hash_text("|".join([title, genre, world_preset, premise, str(target_chapters or ""), length_tier])),
        "depth": {
            "tier": profile["tier"],
            "label": profile["label"],
            "horizon_years": profile["horizon_years"],
            "era_count": profile["era_count"],
            "detail_level": profile["detail_level"],
            "reason": profile["reason"],
        },
        "story_axes": axes,
        "style_adapter": style_adapter,
        "eras": eras,
        "forces": forces,
        "foreshadow_seeds": seeds,
        "planning_guidance": guidance,
    }


def _select_worldline_profile(
    genre: str,
    world_preset: str,
    premise: str,
    target_chapters: Optional[int],
    length_tier: str,
) -> dict[str, Any]:
    text = " ".join([genre, world_preset, premise, length_tier]).lower()
    epic_terms = ["玄幻", "修仙", "仙侠", "奇幻", "史诗", "神话", "王朝", "帝国", "科幻", "星际", "宇宙", "克苏鲁", "文明"]
    complex_terms = ["悬疑", "推理", "权谋", "谍战", "战争", "末世", "赛博", "犯罪", "宫斗", "阴谋", "群像", "历史"]
    intimate_terms = ["都市", "校园", "日常", "恋爱", "青春", "职场", "家庭", "轻喜", "现代"]
    target = target_chapters or 100
    if length_tier == "epic" or target >= 500 or any(term in text for term in epic_terms):
        return {
            "tier": "epic",
            "label": "宏大长线",
            "horizon_years": 3000 if target < 1000 else 10000,
            "era_count": 6 if target < 1000 else 7,
            "detail_level": "high",
            "reason": "题材或篇幅需要跨文明/跨时代因果，前史必须提供制度、灾难与禁忌的长线来源。",
        }
    if target >= 200 or any(term in text for term in complex_terms):
        return {
            "tier": "complex",
            "label": "复杂因果",
            "horizon_years": 180,
            "era_count": 5,
            "detail_level": "medium_high",
            "reason": "题材强调阴谋、制度或多方博弈，需要至少数代人的秘密、旧案和势力传承。",
        }
    if any(term in text for term in intimate_terms):
        return {
            "tier": "intimate",
            "label": "近现代关系线",
            "horizon_years": 12,
            "era_count": 3,
            "detail_level": "focused",
            "reason": "题材更重人物关系和当代生活，前史以近年创伤、家庭/学校/职场制度和关系源头为主。",
        }
    return {
        "tier": "standard",
        "label": "标准长篇",
        "horizon_years": 60,
        "era_count": 4,
        "detail_level": "medium",
        "reason": "默认按中篇商业叙事处理，保留一代以上因果和开篇前夜的可用伏笔。",
    }


def _infer_story_axes(genre: str, world_preset: str, premise: str) -> list[str]:
    text = " ".join([genre, world_preset, premise])
    candidates = [
        ("权力秩序", ["权", "王", "贵族", "组织", "公司", "帝国", "宗门", "学校"]),
        ("禁忌知识", ["禁", "秘", "真相", "档案", "旧案", "研究", "知识", "黑箱"]),
        ("资源争夺", ["资源", "灵气", "矿", "能源", "钥匙", "遗产", "名额", "土地"]),
        ("身份伪装", ["伪装", "身份", "替身", "大小姐", "卧底", "假", "面具"]),
        ("情感依赖", ["依赖", "爱", "亲吻", "拥抱", "家人", "青梅", "搭档", "守护"]),
        ("异常觉醒", ["觉醒", "异能", "异常", "系统", "天赋", "魔法", "污染", "变异"]),
        ("灾难余波", ["灾", "战争", "崩溃", "末世", "瘟疫", "事故", "袭击", "毁灭"]),
    ]
    axes = [name for name, terms in candidates if any(term in text for term in terms)]
    if not axes:
        axes = ["权力秩序", "人物欲望", "隐藏真相"]
    elif len(axes) == 1:
        axes.append("人物欲望")
    return axes[:4]


def _build_world_forces(axes: list[str], profile: dict[str, Any]) -> list[dict[str, str]]:
    forces = []
    for index, axis in enumerate(axes, start=1):
        force_type = "institution" if axis in {"权力秩序", "身份伪装"} else "pressure"
        forces.append(
            {
                "force_id": f"force_{index}",
                "name": f"{axis}的既得利益者",
                "type": force_type,
                "desire": f"维持{axis}带来的优势，不允许开篇主线轻易揭开根因。",
                "weakness": f"{axis}的历史断层或见不得光的交换条件。",
                "planning_use": "可作为主线阻力、阶段反派或伏笔回收对象。",
            }
        )
    if profile["tier"] in {"epic", "complex"}:
        forces.append(
            {
                "force_id": "force_legacy",
                "name": "旧时代残留机制",
                "type": "legacy_system",
                "desire": "继续按旧规则筛选幸存者、继承人或真相持有者。",
                "weakness": "只要有人理解旧时代的代价，就能绕开表层秩序。",
                "planning_use": "用于解释远古遗迹、秘密机构、旧案卷宗和终局反转。",
            }
        )
    return forces


def _build_prehistory_eras(
    profile: dict[str, Any],
    axes: list[str],
    forces: list[dict[str, str]],
    title: str,
) -> list[dict[str, Any]]:
    names = ["根源期", "制度成形期", "第一次创伤期", "秩序粉饰期", "暗流积累期", "开篇前夜", "未公开余波期"]
    horizon = int(profile["horizon_years"])
    count = int(profile["era_count"])
    span = max(horizon // count, 1)
    eras = []
    for index in range(count):
        starts = horizon - span * index
        ends = max(horizon - span * (index + 1), 0)
        axis = axes[index % len(axes)]
        force = forces[index % len(forces)]
        if index == count - 1:
            time_label = "开篇前1年-第1章前"
        else:
            time_label = f"开篇前约{starts}-{ends}年"
        eras.append(
            {
                "era_id": f"pre_{index + 1}",
                "name": names[index],
                "time_label": time_label,
                "summary": _era_summary(names[index], axis, force.get("name", ""), title),
                "causal_effect": f"把{axis}转化为开篇可见的压力，使主角面对的不是偶然麻烦，而是历史长期积累后的爆点。",
                "planning_hooks": [
                    f"用一件看似日常的小物/制度痕迹暗示{name_or_axis(names[index], axis)}。",
                    f"让{force.get('name')}的行动暴露一条旧因果，但暂不解释全部真相。",
                ],
            }
        )
    return eras


def _era_summary(era_name: str, axis: str, force_name: str, title: str) -> str:
    subject = title or "本故事"
    if era_name == "根源期":
        return f"{subject}的核心矛盾在{axis}上首次成形，{force_name}掌握了最初的解释权。"
    if era_name == "制度成形期":
        return f"围绕{axis}形成稳定制度，公开规则保护秩序，隐藏规则保护少数人的收益。"
    if era_name == "第一次创伤期":
        return f"{axis}引发无法公开的事故、背叛或牺牲，成为后续人物命运的隐性债务。"
    if era_name == "秩序粉饰期":
        return f"旧创伤被改写成合理历史，幸存者、受益者和失语者被安排到不同位置。"
    if era_name == "暗流积累期":
        return f"被压住的证据和欲望重新靠近开篇人物，冲突开始从背景走向台前。"
    return f"开篇前夜，各方围绕{axis}完成最后一次布置，主角即将撞上这条历史暗线。"


def name_or_axis(name: str, axis: str) -> str:
    return axis if name == "开篇前夜" else f"{name}的{axis}"


def _build_prehistory_foreshadow_seeds(
    profile: dict[str, Any],
    axes: list[str],
    forces: list[dict[str, str]],
) -> list[dict[str, Any]]:
    seeds = []
    for index, axis in enumerate(axes, start=1):
        force = forces[(index - 1) % len(forces)]
        seeds.append(
            {
                "seed_id": f"seed_{index}",
                "axis": axis,
                "planting_form": f"开篇用一句异常称呼、一份残缺记录或一次不合常理的回避埋下{axis}。",
                "surface_meaning": "读者初看只会认为这是世界观质感或人物习惯。",
                "true_meaning": f"它指向{force.get('name')}在前史中留下的债务。",
                "recommended_payoff": "中后期当主角掌握证据或付出代价后再解释完整因果。",
            }
        )
    if profile["tier"] in {"epic", "complex"}:
        seeds.append(
            {
                "seed_id": "seed_epoch_lie",
                "axis": "历史谎言",
                "planting_form": "让官方年表、家族传说或宗门记录出现一个无法同时成立的日期。",
                "surface_meaning": "像是资料误差。",
                "true_meaning": "旧时代被人为截断，某个关键事件发生时间被整体改写。",
                "recommended_payoff": "用于卷末或部末反转，推动主线从个人冲突升级为世界结构冲突。",
            }
        )
    return seeds


def _build_prehistory_guidance(profile: dict[str, Any], axes: list[str]) -> list[str]:
    guidance = [
        "前史只提供因果压力，不替代正文选择；规划时应把它转化为角色目标、误判、代价和伏笔。",
        f"当前前史深度为{profile['label']}：大纲中至少选择一条前史因果进入第一卷，一条保留到中后期回收。",
        f"优先围绕{axes[0]}设计开篇钩子，让读者先看到结果，再逐步追溯原因。",
    ]
    if profile["tier"] in {"epic", "complex"}:
        guidance.append("长线题材需要把旧时代因果拆成多次揭示：误导线索、阶段真相、终局真相不可一次说完。")
    else:
        guidance.append("近关系/现代题材不宜堆砌古老历史，重点让前史服务人物关系、家庭压力或制度惯性。")
    return guidance


def _build_style_adapter(
    *,
    title: str = "",
    premise: str = "",
    genre: str = "",
    world_preset: str = "",
    style_hint: str = "",
) -> dict[str, Any]:
    raw_text = "\n".join(part for part in [style_hint, genre, world_preset, premise, title] if part).strip()
    tags = _detect_style_tags(raw_text)
    primary = tags[0] if tags else "custom_or_unspecified"
    strategy = _style_strategy(primary)
    return {
        "schema_version": 1,
        "mode": "semantic_first_style_late_binding",
        "requested_style": style_hint[:500],
        "detected_style_tags": tags or ["custom_or_unspecified"],
        "primary_style": primary,
        "rendering_strategy": strategy,
        "adaptation_contract": [
            "Evolution 前史是语义蓝图，不是最终正文；规划和写作时必须按小说当前文风重新表达。",
            "保留因果、秘密、代价、伏笔功能，允许彻底改写措辞、节奏、意象、叙述视角和信息密度。",
            "若用户/Bible/章节样本文风与本适配器不一致，以最新显式文风为准。",
            "不要把前史条目机械塞进正文；只能转化为符合文风的场景痕迹、人物选择、传闻、物件或沉默。",
        ],
        "style_axes": {
            "diction": strategy["diction"],
            "sentence_rhythm": strategy["sentence_rhythm"],
            "imagery": strategy["imagery"],
            "information_density": strategy["information_density"],
            "revelation": strategy["revelation"],
        },
    }


def _build_runtime_style_adapter(worldline: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    stored = worldline.get("style_adapter") if isinstance(worldline.get("style_adapter"), dict) else {}
    style_hint = _extract_runtime_style_hint(payload)
    if not style_hint:
        return stored or _build_style_adapter()
    runtime = _build_style_adapter(
        title=str(worldline.get("title") or ""),
        premise=str(payload.get("premise") or payload.get("novel_premise") or ""),
        genre=str(payload.get("genre") or ""),
        world_preset=str(payload.get("world_preset") or ""),
        style_hint=style_hint,
    )
    runtime["base_detected_style_tags"] = stored.get("detected_style_tags") or []
    runtime["style_source"] = "runtime_payload"
    return runtime


def _extract_runtime_style_hint(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("style_hint", "style", "writing_style", "voice", "tone"):
        value = str(payload.get(key) or "").strip()
        if value:
            candidates.append(value)
    bible_context = payload.get("bible_context") if isinstance(payload.get("bible_context"), dict) else {}
    if bible_context:
        for key in ("style_hint", "style", "writing_style", "voice", "tone"):
            value = str(bible_context.get(key) or "").strip()
            if value:
                candidates.append(value)
        for note in bible_context.get("style_notes") or []:
            if isinstance(note, dict):
                content = str(note.get("content") or note.get("description") or "").strip()
                category = str(note.get("category") or "").strip()
                if content:
                    candidates.append(f"{category}: {content}" if category else content)
            else:
                value = str(note or "").strip()
                if value:
                    candidates.append(value)
    return "\n".join(candidates)[:1200]


def _detect_style_tags(text: str) -> list[str]:
    value = str(text or "").lower()
    buckets = [
        ("poetic_lyrical", ["诗", "抒情", "散文", "意象", "唯美", "朦胧", "浪漫", " lyrical", "poetic"]),
        ("plain_realist", ["白描", "现实", "纪实", "克制", "冷静", "平实", "生活流", "realist", "minimal"]),
        ("fast_web_serial", ["爽文", "热血", "节奏快", "强情绪", "打脸", "升级", "网文", "serial"]),
        ("comedic_light", ["轻松", "吐槽", "搞笑", "喜剧", "沙雕", "幽默", "日常向", "comedy"]),
        ("classical_archaic", ["古风", "文言", "典雅", "志怪", "章回", "古典", "classical"]),
        ("hardboiled_noir", ["冷硬", "黑色", "硬汉", "犯罪", "侦探", "noir", "hardboiled"]),
        ("cosmic_ominous", ["克苏鲁", "诡异", "恐怖", "压抑", "阴郁", "不可名状", "ominous", "horror"]),
        ("technical_sf", ["硬科幻", "技术", "赛博", "算法", "工程", "实验", "cyber", "sci-fi", "science fiction"]),
        ("fairytale_fable", ["童话", "寓言", "儿童", "温柔", "治愈", "fairytale", "fable"]),
        ("epic_chronicle", ["史诗", "编年", "群像", "战争史", "王朝", "文明史", "chronicle", "epic"]),
    ]
    tags = [name for name, terms in buckets if any(term in value for term in terms)]
    return tags[:4]


def _style_strategy(primary: str) -> dict[str, str]:
    strategies = {
        "poetic_lyrical": {
            "diction": "用意象、感官和隐喻承载信息，少用制度说明词。",
            "sentence_rhythm": "句式可长短错落，保留回声和余韵。",
            "imagery": "把前史转成物候、颜色、声音、旧物和身体感受。",
            "information_density": "低到中；一次只透露一层情绪化线索。",
            "revelation": "先给象征，再给事实，真相像潮水一样回返。",
        },
        "plain_realist": {
            "diction": "用日常、具体、克制的词，避免宏大抽象名词压过人物生活。",
            "sentence_rhythm": "中短句为主，因果藏在行动和细节里。",
            "imagery": "使用账单、校规、工位、病历、街道等可触摸物。",
            "information_density": "中；每个线索服务一个现实压力。",
            "revelation": "通过人物碰壁、旁人回避、制度流程逐步显影。",
        },
        "fast_web_serial": {
            "diction": "用目标、阻力、赌注、反转来表达前史，保持可读性和推进感。",
            "sentence_rhythm": "短句和强转折更优先。",
            "imagery": "线索要能迅速变成冲突、奖励、惩罚或升级资源。",
            "information_density": "中到高；每幕至少让一条前史因果推动爽点或危机。",
            "revelation": "误导-爆点-更大黑幕，分层抬高期待。",
        },
        "comedic_light": {
            "diction": "用轻巧、反差和吐槽式误会承载严肃因果。",
            "sentence_rhythm": "短促灵活，允许包袱后突然落入真相。",
            "imagery": "把秘密藏在尴尬物件、错位对话和日常事故里。",
            "information_density": "低到中；不要让设定解释压垮喜剧节奏。",
            "revelation": "先当笑点，再在关键处证明笑点是伏笔。",
        },
        "classical_archaic": {
            "diction": "用典雅、含蓄、礼法/名分/旧闻承载因果。",
            "sentence_rhythm": "整饬、留白，少用现代术语。",
            "imagery": "碑、谱牒、旧诏、祠堂、风物和传闻适合承载前史。",
            "information_density": "中；重传承和名分变迁。",
            "revelation": "由传闻、旧物、礼制破绽层层反证。",
        },
        "hardboiled_noir": {
            "diction": "冷、硬、短，重事实、伤痕、交易和背叛。",
            "sentence_rhythm": "短句优先，少解释，多压迫。",
            "imagery": "雨夜、档案袋、烟味、账本、监控盲区等具体痕迹。",
            "information_density": "中高；每条线索都带风险。",
            "revelation": "让真相像旧伤一样被迫撕开。",
        },
        "cosmic_ominous": {
            "diction": "避免直接解释不可名状之物，用异常、缺页、重复梦境和认知污染呈现。",
            "sentence_rhythm": "逐步失稳，允许不完全解释。",
            "imagery": "星象、潮声、畸形仪式、腐蚀文字、无法对齐的时间。",
            "information_density": "低到中；保留未知感。",
            "revelation": "每次解释只揭开更深的不安。",
        },
        "technical_sf": {
            "diction": "用系统、协议、实验、数据缺口和工程限制表达因果。",
            "sentence_rhythm": "清晰准确，避免玄学化。",
            "imagery": "日志、接口、传感器异常、材料疲劳、算法偏差。",
            "information_density": "中高；前史要能支持机制推演。",
            "revelation": "先暴露观测异常，再追溯设计缺陷或历史篡改。",
        },
        "fairytale_fable": {
            "diction": "用简单、温柔、象征性的词承载深层因果。",
            "sentence_rhythm": "明亮、重复、有寓言感。",
            "imagery": "钥匙、门、森林、灯、名字、约定。",
            "information_density": "低；一个象征对应一个秘密。",
            "revelation": "让真相像寓言教训一样自然浮现。",
        },
        "epic_chronicle": {
            "diction": "用编年、誓约、迁徙、王朝和代际代价表达前史。",
            "sentence_rhythm": "稳重，有历史纵深。",
            "imagery": "年表、城邦、血脉、盟约、战场遗址。",
            "information_density": "高；允许多势力、多时代并置。",
            "revelation": "从个人命运回望文明级因果。",
        },
    }
    return strategies.get(
        primary,
        {
            "diction": "跟随用户最新文风提示；无法归类时只保留语义功能，不规定措辞。",
            "sentence_rhythm": "匹配样本文本的句长、停顿和叙述视角。",
            "imagery": "沿用小说自身反复出现的物象，不引入违和符号。",
            "information_density": "弹性；按目标文风决定铺陈或留白。",
            "revelation": "按目标文风选择直给、留白、象征、反转或对话侧写。",
        },
    )


def _render_story_planning_evidence(evidence: dict[str, Any], *, style_adapter: Optional[dict[str, Any]] = None) -> str:
    worldline = evidence.get("worldline") or {}
    depth = worldline.get("depth") or {}
    style_adapter = style_adapter or worldline.get("style_adapter") or {}
    lines = [
        f"前史深度：{depth.get('label', '未定')}；跨度：约{depth.get('horizon_years', 0)}年；原因：{depth.get('reason', '')}",
    ]
    if style_adapter:
        axes = style_adapter.get("style_axes") or {}
        lines.append("【文风适配协议】")
        lines.append(f"- 当前文风标签：{'、'.join(_as_strings(style_adapter.get('detected_style_tags'))) or '自定义/未指定'}；前史条目只作为语义蓝图，不能原样写进正文。")
        if style_adapter.get("requested_style"):
            lines.append(f"- 用户/Bible文风提示：{str(style_adapter.get('requested_style'))[:240]}")
        for item in style_adapter.get("adaptation_contract") or []:
            lines.append(f"- {item}")
        if axes:
            lines.append(
                "- 转译方式："
                f"措辞={axes.get('diction', '')}；"
                f"节奏={axes.get('sentence_rhythm', '')}；"
                f"意象={axes.get('imagery', '')}；"
                f"揭示={axes.get('revelation', '')}"
            )
    if evidence.get("eras"):
        lines.append("【故事开始前的世界线】")
        for era in evidence["eras"]:
            lines.append(f"- {era.get('time_label')}｜{era.get('name')}：{era.get('summary')} 因果作用：{era.get('causal_effect')}")
    if evidence.get("forces"):
        lines.append("【势力/制度因果】")
        for force in evidence["forces"]:
            lines.append(f"- {force.get('name')}：欲望={force.get('desire')}；弱点={force.get('weakness')}")
    if evidence.get("foreshadow_seeds"):
        lines.append("【可用于大纲与伏笔的种子】")
        for seed in evidence["foreshadow_seeds"]:
            lines.append(f"- {seed.get('axis')}：{seed.get('planting_form')} 真相={seed.get('true_meaning')}")
    if evidence.get("planning_guidance"):
        lines.append("【使用约束】")
        lines.extend(f"- {item}" for item in evidence["planning_guidance"])
    return "\n".join(line for line in lines if str(line).strip())


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


def _build_timeline_events(snapshot, extraction: dict[str, Any], content_hash: str, at: str) -> list[dict[str, Any]]:
    raw_events = extraction.get("world_events") or []
    if not raw_events:
        raw_events = [{"summary": item, "characters": snapshot.characters, "locations": snapshot.locations[:5]} for item in snapshot.world_events]
    events: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_events, start=1):
        if not isinstance(raw, dict):
            raw = {"summary": str(raw)}
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            continue
        participants = [str(item) for item in (raw.get("characters") or snapshot.characters) if str(item).strip()]
        locations = [str(item) for item in (raw.get("locations") or snapshot.locations[:5]) if str(item).strip()]
        event_type = str(raw.get("event_type") or "scene").strip() or "scene"
        seed = f"{snapshot.novel_id}:{snapshot.chapter_number}:{index}:{summary}:{content_hash}"
        events.append(
            {
                "event_id": "evt_" + _hash_text(seed)[:16],
                "novel_id": snapshot.novel_id,
                "chapter_number": snapshot.chapter_number,
                "scene_order": index,
                "event_type": event_type,
                "summary": summary[:240],
                "participants": participants[:12],
                "location": locations[0] if locations else "",
                "locations": locations[:5],
                "effects": _event_effects_from_raw(raw),
                "knowledge_delta": _knowledge_delta_from_raw(raw, participants),
                "source": extraction.get("source") or "deterministic",
                "content_hash": content_hash,
                "confidence": float(raw.get("confidence") or 0.7),
                "at": at,
            }
        )
    return events


def _event_effects_from_raw(raw: dict[str, Any]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for field in ("emotion", "inner_change", "growth_stage", "growth_change"):
        value = str(raw.get(field) or "").strip()
        if value:
            effects.append({"target_type": "character", "field": field, "value": value[:160]})
    return effects[:8]


def _knowledge_delta_from_raw(raw: dict[str, Any], participants: list[str]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for fact in raw.get("known_facts") or []:
        value = str(fact or "").strip()
        if value:
            for name in participants[:4] or ["__scene__"]:
                deltas.append({"character": name, "learned": value[:160]})
    return deltas[:12]


def _build_continuity_constraints(novel_id: str, cards: list[dict[str, Any]], chapter_number: int, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    evidence_ids = [event.get("event_id") for event in events if event.get("event_id")]
    for card in cards:
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        for unknown in _as_strings((card.get("cognitive_state") or {}).get("unknowns"))[-3:]:
            constraints.append(_constraint(novel_id, chapter_number, "knowledge_boundary", name, f"{name} 仍未知：{unknown}。", evidence_ids))
        for limit in _as_strings(card.get("capability_limits"))[-3:]:
            constraints.append(_constraint(novel_id, chapter_number, "capability_boundary", name, f"{name} 的能力边界：{limit}。", evidence_ids))
        palette = card.get("personality_palette") if isinstance(card.get("personality_palette"), dict) else {}
        base = str(palette.get("base") or "").strip()
        main = "、".join(_as_strings(palette.get("main_tones"))[:3])
        accents = "、".join(_as_strings(palette.get("accents"))[:2])
        if base or main or accents:
            rule = f"{name} 的性格调色盘：底色={base or '未定'}；主色调={main or '未定'}；点缀={accents or '无'}。行为转折需与调色盘衍生一致。"
            constraints.append(_constraint(novel_id, chapter_number, "personality_boundary", name, rule, evidence_ids))
    return constraints


def _constraint(novel_id: str, chapter_number: int, constraint_type: str, subject: str, rule: str, evidence_ids: list[str]) -> dict[str, Any]:
    seed = f"{novel_id}:{constraint_type}:{subject}:{rule}"
    return {
        "constraint_id": "cc_" + _hash_text(seed)[:16],
        "novel_id": novel_id,
        "type": constraint_type,
        "subject": subject,
        "rule": rule[:260],
        "severity": "warning",
        "evidence_events": evidence_ids[:8],
        "created_or_updated_chapter": chapter_number,
    }


def _build_review_context_blocks(evidence: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    events = evidence.get("events") or []
    constraints = evidence.get("constraints") or []
    characters = evidence.get("characters") or []
    if events:
        blocks.append({"title": "Evolution 时间线证据", "kind": "timeline_evidence", "content": _render_review_events(events), "items": events})
    if constraints:
        blocks.append({"title": "Evolution 连续性约束", "kind": "continuity_constraints", "content": _render_review_constraints(constraints), "items": constraints})
    if characters:
        blocks.append({"title": "Evolution 人物状态投影", "kind": "character_state_projection", "content": _render_review_characters(characters), "items": characters})
    return blocks


def _render_review_events(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events[-8:]:
        names = "、".join(str(item) for item in event.get("participants") or [])
        who = f" 角色：{names}" if names else ""
        location = f" 地点：{event.get('location')}" if event.get("location") else ""
        lines.append(f"- 第{event.get('chapter_number')}章：{event.get('summary')}{who}{location}")
    return "\n".join(lines)


def _render_review_constraints(constraints: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{item.get('type')}] {item.get('rule')}" for item in constraints[:10])


def _render_review_characters(characters: list[dict[str, Any]]) -> str:
    lines = []
    for card in characters[:8]:
        cognitive = card.get("cognitive_state") or {}
        unknowns = "、".join(_as_strings(cognitive.get("unknowns"))[-2:])
        limits = "、".join(_as_strings(card.get("capability_limits"))[-2:])
        suffix = "；".join(item for item in [f"未知={unknowns}" if unknowns else "", f"能力边界={limits}" if limits else ""] if item)
        lines.append(f"- {card.get('name')}：最近第{card.get('last_seen_chapter')}章；{suffix or '暂无硬性边界'}")
    return "\n".join(lines)


def _attach_issue_evidence(issues: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]], *, subject: str) -> list[dict[str, Any]]:
    subject = str(subject or "")
    events = [
        event
        for event in evidence.get("events", [])
        if not subject or subject in [str(item) for item in event.get("participants") or []] or subject in str(event.get("summary") or "")
    ][:3]
    constraints = [
        constraint
        for constraint in evidence.get("constraints", [])
        if not subject or subject == str(constraint.get("subject") or "") or subject in str(constraint.get("rule") or "")
    ][:3]
    refs = [
        {"event_id": item.get("event_id"), "chapter_number": item.get("chapter_number"), "summary": item.get("summary")}
        for item in events
    ]
    refs.extend(
        {"constraint_id": item.get("constraint_id"), "type": item.get("type"), "rule": item.get("rule")}
        for item in constraints
    )
    for issue in issues:
        if refs:
            issue["evidence"] = refs
    return issues


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


def _default_settings() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "api2_control_card": {
            "enabled": False,
            "provider_mode": "same_as_main",
            "model": "",
            "temperature": 0.2,
            "max_tokens": 1400,
            "custom_profile": {
                "id": "evolution-api2-custom",
                "name": "Evolution API2",
                "preset_key": "custom-openai-compatible",
                "protocol": "openai",
                "base_url": "",
                "api_key": "",
                "model": "",
                "temperature": 0.2,
                "max_tokens": 1400,
                "timeout_seconds": 180,
                "extra_headers": {},
                "extra_query": {},
                "extra_body": {},
                "notes": "Evolution 控制卡压缩专用 API",
                "use_legacy_chat_completions": False,
            },
        },
    }


def _normalize_settings(raw: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    base = _default_settings()
    if existing:
        base = _deep_merge(base, existing)
    if isinstance(raw, dict):
        base = _deep_merge(base, raw)
    api2 = base["api2_control_card"]
    api2["enabled"] = bool(api2.get("enabled"))
    provider_mode = str(api2.get("provider_mode") or "same_as_main")
    api2["provider_mode"] = provider_mode if provider_mode in API2_PROVIDER_MODES else "same_as_main"
    api2["temperature"] = _clamp_float(api2.get("temperature"), 0.0, 2.0, 0.2)
    api2["max_tokens"] = _clamp_int(api2.get("max_tokens"), 256, 4096, 1400)
    custom = _custom_profile_for_storage(api2.get("custom_profile") if isinstance(api2.get("custom_profile"), dict) else {})
    if existing:
        prior = ((existing.get("api2_control_card") or {}).get("custom_profile") or {}) if isinstance(existing, dict) else {}
        submitted_key = str(custom.get("api_key") or "")
        if submitted_key in {"", "********", "••••••••"}:
            custom["api_key"] = str(prior.get("api_key") or "")
    api2["custom_profile"] = custom
    if api2["provider_mode"] == "custom":
        api2["model"] = custom.get("model") or ""
        api2["temperature"] = custom.get("temperature", api2["temperature"])
        api2["max_tokens"] = custom.get("max_tokens", api2["max_tokens"])
    return base


def _redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    safe = copy.deepcopy(settings)
    custom = ((safe.get("api2_control_card") or {}).get("custom_profile") or {})
    api_key = str(custom.get("api_key") or "")
    custom["api_key"] = ""
    custom["api_key_configured"] = bool(api_key)
    return safe


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _custom_profile_for_storage(raw: dict[str, Any]) -> dict[str, Any]:
    protocol = str(raw.get("protocol") or "openai").strip()
    if protocol not in API2_PROTOCOLS:
        protocol = "openai"
    return {
        "id": str(raw.get("id") or "evolution-api2-custom").strip() or "evolution-api2-custom",
        "name": str(raw.get("name") or "Evolution API2").strip() or "Evolution API2",
        "preset_key": str(raw.get("preset_key") or "custom-openai-compatible").strip() or "custom-openai-compatible",
        "protocol": protocol,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "temperature": _clamp_float(raw.get("temperature"), 0.0, 2.0, 0.2),
        "max_tokens": _clamp_int(raw.get("max_tokens"), 256, 4096, 1400),
        "timeout_seconds": _clamp_int(raw.get("timeout_seconds"), 10, 900, 180),
        "extra_headers": raw.get("extra_headers") if isinstance(raw.get("extra_headers"), dict) else {},
        "extra_query": raw.get("extra_query") if isinstance(raw.get("extra_query"), dict) else {},
        "extra_body": raw.get("extra_body") if isinstance(raw.get("extra_body"), dict) else {},
        "notes": str(raw.get("notes") or "Evolution 控制卡压缩专用 API"),
        "use_legacy_chat_completions": bool(raw.get("use_legacy_chat_completions")),
    }


def _custom_profile_for_llm(raw: dict[str, Any]) -> dict[str, Any]:
    return _custom_profile_for_storage(raw)


def _build_api2_control_card_prompt(*, chapter_number: Optional[int], outline: str, raw_context: str) -> Any:
    system = (
        "你是 Evolution 插件的状态压缩器，不写正文。"
        "你只把冗长世界状态压缩成给正文作者使用的本章写作控制卡。"
    )
    user = f"""【本章】
第{chapter_number or '-'}章

【本章大纲】
{outline or '无'}

【原始 Evolution 上下文】
{raw_context}

请输出中文控制卡，建议 900-1300 字符，必须包含：
1. 上一章结尾必须承接的状态。
2. 本章硬约束与禁写事项。
3. 角色信息边界：谁知道什么，谁不能提前知道什么。
4. 本章剧情推进目标。
5. 禁用重复模板：不要使用“没有说话/没有回答/没有立刻回答/沉默了几秒/盯着屏幕看了几秒/呼吸停了一拍”等。
6. 替代表现方式：具体动作、环境反应、技术操作、心理判断、场面调度。
7. 文风适配提醒：根据原始上下文和本章题材调整措辞，不固定成某一种文风。

只输出控制卡，不要写正文、标题、解释或评分。"""
    try:
        from domain.ai.value_objects.prompt import Prompt

        return Prompt(system=system, user=user)
    except Exception:
        class PromptFallback:
            def __init__(self, system: str, user: str) -> None:
                self.system = system
                self.user = user

        return PromptFallback(system=system, user=user)


def _make_generation_config(*, model: str, max_tokens: int, temperature: float) -> Any:
    try:
        from domain.ai.services.llm_service import GenerationConfig

        return GenerationConfig(model=model, max_tokens=max_tokens, temperature=temperature)
    except Exception:
        class GenerationConfigFallback:
            def __init__(self, model: str, max_tokens: int, temperature: float) -> None:
                self.model = model
                self.max_tokens = max_tokens
                self.temperature = temperature

        return GenerationConfigFallback(model=model, max_tokens=max_tokens, temperature=temperature)


def _clean_api2_control_card(content: str) -> str:
    text = str(content or "").strip()
    text = text.removeprefix("```markdown").removeprefix("```text").removeprefix("```").strip()
    text = text.removesuffix("```").strip()
    return text


def _token_usage_to_dict(token_usage: Any) -> dict[str, int]:
    if token_usage is None:
        return {}
    if hasattr(token_usage, "to_dict"):
        data = token_usage.to_dict()
        return data if isinstance(data, dict) else {}
    return {
        "input_tokens": int(getattr(token_usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(token_usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(token_usage, "total_tokens", 0) or 0),
    }


def _run_async_blocking(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            box["error"] = exc

    thread = threading.Thread(target=runner, name="evolution-api2-control-card", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


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
