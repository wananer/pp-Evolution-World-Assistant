"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import logging
import threading
from datetime import datetime, timezone
from time import perf_counter
from hashlib import sha256
from typing import Any, Optional, Union, Tuple
from urllib.parse import urlparse, urlunparse

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.host_database import ReadOnlyHostDatabase, create_default_readonly_host_database
from plugins.platform.plugin_storage import PluginStorage

from .agent_assets import (
    build_commit_event,
    build_reflection_record,
    build_selection_event,
    consolidate_agent_memory,
    evaluate_strategy_effectiveness,
    extract_context_signals,
    select_agent_assets,
    solidify_capsules_from_review,
)
from .canonical_characters import (
    calibrate_extracted_characters,
    canonicalize_names_in_records,
    load_canonical_characters,
)
from .continuity import build_chapter_summary, build_volume_summary
from .context_capsules import build_injection_record
from .context_patch import build_context_patch, render_patch_summary
from .diagnostics_service import DiagnosticsService
from .host_context import HOST_CONTEXT_SOURCES, HostContextReader
from .local_semantic_memory import LocalSemanticMemory
from .preset_converter import convert_st_preset
from .repositories import RECENT_CONTEXT_FACT_LIMIT, EvolutionWorldRepository
from .story_graph import build_global_route_map, build_story_graph_chapter
from .structured_extractor import StructuredExtractorProvider, extract_structured_chapter_facts

PLUGIN_NAME = "world_evolution_core"
LLM_PROVIDER_MODES = {"same_as_main", "custom"}
LLM_MODEL_PROTOCOLS = {"openai", "anthropic", "gemini"}
CONTEXT_EXTERNAL_TIMEOUT_SECONDS = 2.5
logger = logging.getLogger(__name__)


class EvolutionWorldAssistantService:
    def __init__(
        self,
        storage: Optional[PluginStorage] = None,
        jobs: Optional[PluginJobRegistry] = None,
        repository: Optional[EvolutionWorldRepository] = None,
        extractor_provider: Optional[StructuredExtractorProvider] = None,
        agent_llm_service: Optional[Any] = None,
        llm_provider_factory: Optional[Any] = None,
        host_database: Optional[ReadOnlyHostDatabase] = None,
        semantic_memory: Optional[LocalSemanticMemory] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)
        self.repository = repository or EvolutionWorldRepository(self.storage)
        self.extractor_provider = extractor_provider
        self.agent_llm_service = agent_llm_service
        self.llm_provider_factory = llm_provider_factory
        self.host_database = host_database if host_database is not None else create_default_readonly_host_database()
        self.semantic_memory = semantic_memory or LocalSemanticMemory(host_database=self.host_database)
        self.host_context_reader = HostContextReader(self.host_database)
        self.diagnostics_service = DiagnosticsService(
            repository=self.repository,
            route_map_provider=self.get_global_route_map,
        )

    def get_settings(self, *, safe: bool = True) -> dict[str, Any]:
        settings = _normalize_settings(self.repository.get_settings())
        return _redact_settings(settings) if safe else settings

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = _normalize_settings(self.repository.get_settings())
        settings = _normalize_settings(payload or {}, existing=existing)
        self.repository.save_settings(settings)
        return self.get_settings(safe=True)

    async def fetch_api2_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Deprecated compatibility endpoint; API2 no longer performs Evolution work."""
        return self.deprecated_api2_response()

    def deprecated_api2_response(self) -> dict[str, Any]:
        return {
            "ok": False,
            "deprecated": True,
            "items": [],
            "count": 0,
            "source": "legacy_api2",
            "protocol": None,
            "error": "API2 is deprecated. Configure settings.agent_api for Evolution control cards and reflections.",
            "replacement": "agent_api",
        }

    async def fetch_agent_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _build_agent_models_request(payload, self.get_settings(safe=False))
        items = await _fetch_model_list_items(request)
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "source": request["source"],
            "protocol": request["protocol"],
        }

    async def test_api2_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.deprecated_api2_response()

    async def test_agent_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = _agent_runtime_settings_from_payload(payload, self.get_settings(safe=False))
        started = perf_counter()
        try:
            llm_service = self._resolve_agent_llm_service(settings)
            result = await llm_service.generate(
                _build_llm_connection_test_prompt(),
                _make_generation_config(
                    model=str(settings.get("model") or ""),
                    max_tokens=32,
                    temperature=0.0,
                ),
            )
            return {
                "ok": True,
                "provider_mode": settings.get("provider_mode"),
                "protocol": (settings.get("custom_profile") or {}).get("protocol") if settings.get("provider_mode") == "custom" else None,
                "model": str(settings.get("model") or ""),
                "latency_ms": int((perf_counter() - started) * 1000),
                "preview": str(result.content or "").strip()[:120],
                "token_usage": _token_usage_to_dict(getattr(result, "token_usage", None)),
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider_mode": settings.get("provider_mode"),
                "model": str(settings.get("model") or ""),
                "latency_ms": int((perf_counter() - started) * 1000),
                "error": str(exc),
            }

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
        canonical_characters = load_canonical_characters(self.host_database, novel_id)
        calibration = calibrate_extracted_characters(
            content=content,
            snapshot_characters=snapshot.characters,
            character_updates=[item.to_dict() for item in extraction.character_updates],
            canonical_characters=canonical_characters,
        )
        snapshot.characters = calibration.characters
        character_updates = calibration.character_updates
        if calibration.warnings:
            extraction.warnings.extend(calibration.warnings)
        snapshot.characters = _filter_snapshot_characters(snapshot.characters)
        snapshot.locations = _filter_snapshot_locations(snapshot.locations)
        character_updates = [
            item
            for item in character_updates
            if _valid_snapshot_character_name(str(item.get("name") or ""))
        ]
        chapter_summary = build_chapter_summary(novel_id, chapter_number, content, _now())
        known_names = [card.get("name") for card in self.repository.list_character_index(novel_id).get("items", [])]
        if canonical_characters:
            canonical_names = {item.name for item in canonical_characters}
            known_names = [name for name in known_names if name in canonical_names]
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
            character_updates,
        )
        extraction_payload = extraction.to_dict()
        extraction_payload["snapshot"] = snapshot.to_dict()
        extraction_payload["character_updates"] = character_updates
        extraction_payload["canonical_character_count"] = calibration.canonical_count
        extraction_payload["ignored_character_candidates"] = calibration.ignored_candidates
        if canonical_characters:
            extraction_payload["world_events"] = canonicalize_names_in_records(
                extraction_payload.get("world_events") or [],
                canonical_characters,
            )
        timeline_events = _build_timeline_events(snapshot, extraction_payload, content_hash, _now())
        self.repository.save_timeline_events(novel_id, timeline_events)
        self.repository.save_continuity_constraints(
            novel_id,
            _build_continuity_constraints(novel_id, updated_cards, snapshot.chapter_number, timeline_events),
        )
        previous_graph_chapters = self.repository.list_story_graph_chapters(novel_id, before_chapter=chapter_number)
        story_graph_chapter = build_story_graph_chapter(
            novel_id=novel_id,
            chapter_number=chapter_number,
            snapshot=snapshot.to_dict(),
            chapter_summary=chapter_summary,
            timeline_events=timeline_events,
            previous_chapters=previous_graph_chapters,
            at=_now(),
        )
        self.repository.save_story_graph_chapter(novel_id, chapter_number, story_graph_chapter)
        style_repetition_state = _build_style_repetition_state(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content=content,
            recent_summaries=self.repository.list_chapter_summaries(novel_id, limit=3),
            at=_now(),
        )
        if style_repetition_state.get("phrases"):
            self.repository.save_style_repetition_state(novel_id, style_repetition_state)
        native_after_commit = self._read_native_after_commit_context(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content=content,
        )
        extraction_payload["native_after_commit"] = native_after_commit
        extraction_payload["fallback_degraded"] = bool(native_after_commit.get("fallback_degraded"))
        finished_at = _now()
        duration_ms = int((perf_counter() - start_time) * 1000)
        agent_event = build_commit_event(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            snapshot=snapshot.to_dict(),
            story_graph=story_graph_chapter,
            at=finished_at,
        )
        self.repository.append_agent_event(novel_id, agent_event)
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
                    "story_graph_saved": True,
                    "style_repetition_phrase_count": len(style_repetition_state.get("phrases") or []),
                    "route_edge_count": len(story_graph_chapter.get("route_edges") or []),
                    "route_conflict_count": len(story_graph_chapter.get("conflicts") or []),
                    "native_after_commit": native_after_commit,
                    "agent_event_id": agent_event.get("id"),
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
                    "story_graph_path": f"story_graph/chapters/chapter_{chapter_number}.json",
                    "characters_updated": [card.get("character_id") for card in updated_cards],
                    "native_after_commit": native_after_commit,
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
                "extraction": extraction_payload,
                "story_graph": story_graph_chapter,
                "native_after_commit": native_after_commit,
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
            "agent_control_card_enabled": False,
        }
        settings = self.get_settings(safe=False)
        agent_settings = settings.get("agent_api") if isinstance(settings.get("agent_api"), dict) else {}
        if agent_settings.get("enabled"):
            control_card = self._build_agent_control_card(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                raw_context=summary,
                settings=agent_settings,
            )
            if control_card.get("ok") and control_card.get("content"):
                content = str(control_card["content"]).strip()
                title = "Evolution 智能体写作控制卡"
                metadata.update(
                    {
                        "agent_control_card_enabled": True,
                        "agent_provider_mode": agent_settings.get("provider_mode"),
                        "agent_raw_context_chars": len(summary),
                        "agent_control_card_chars": len(content),
                        "agent_compression_ratio": round(len(content) / max(len(summary), 1), 4),
                    }
                )
            else:
                metadata.update(
                    {
                        "agent_control_card_enabled": True,
                        "agent_error": control_card.get("error") or control_card.get("reason") or "unknown",
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
        agent_selection = patch.get("agent_selection") if isinstance(patch.get("agent_selection"), dict) else {}
        if agent_selection and (agent_selection.get("selected_gene_ids") or agent_selection.get("selected_capsule_ids")):
            self.repository.append_agent_selection_record(novel_id, agent_selection)
            self.repository.append_agent_event(novel_id, build_selection_event(agent_selection))

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

    def _build_agent_control_card(
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
            llm_service = self._resolve_agent_llm_service(settings)
            prompt = _build_agent_control_card_prompt(
                chapter_number=chapter_number,
                outline=outline,
                raw_context=raw_context,
            )
            result = _run_async_blocking(
                llm_service.generate(
                    prompt,
                    _make_generation_config(
                        model=str(settings.get("model") or ""),
                        max_tokens=_clamp_int(settings.get("max_tokens"), 256, 4096, 1200),
                        temperature=_clamp_float(settings.get("temperature"), 0.0, 2.0, 0.1),
                    ),
                )
            )
            content = _clean_control_card(result.content)
            if not content:
                return {"ok": False, "error": "agent api returned empty control card"}
            finished_at = _now()
            token_usage = getattr(result, "token_usage", None)
            record = {
                "at": finished_at,
                "started_at": started_at,
                "chapter_number": chapter_number,
                "provider_mode": settings.get("provider_mode"),
                "source": "agent_api",
                "raw_context_chars": len(raw_context),
                "control_card_chars": len(content),
                "compression_ratio": round(len(content) / max(len(raw_context), 1), 4),
                "model": str(settings.get("model") or ""),
                "token_usage": _token_usage_to_dict(token_usage),
            }
            self.repository.append_context_control_card_record(novel_id, record)
            self.repository.append_agent_event(
                novel_id,
                {
                    "type": "EvolutionEvent",
                    "schema_version": 1,
                    "id": f"evt_agent_control_card_{_hash_text(novel_id + str(chapter_number or '') + finished_at)}",
                    "intent": "control_card",
                    "hook_name": "before_context_build",
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "signals": ["agent_api", "context_compression", "control_card"],
                    "genes_used": [],
                    "capsule_id": None,
                    "outcome": {"status": "success", "control_card_chars": len(content)},
                    "meta": {"at": finished_at, "model": str(settings.get("model") or "")},
                },
            )
            return {"ok": True, "content": content, "record": record}
        except Exception as exc:
            failed_at = _now()
            self.repository.append_agent_event(
                novel_id,
                {
                    "type": "EvolutionEvent",
                    "schema_version": 1,
                    "id": f"evt_agent_control_card_failed_{_hash_text(novel_id + str(chapter_number or '') + failed_at)}",
                    "intent": "control_card",
                    "hook_name": "before_context_build",
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "signals": ["agent_api", "context_compression", "control_card"],
                    "genes_used": [],
                    "capsule_id": None,
                    "outcome": {"status": "failed", "error": str(exc)},
                    "meta": {"at": failed_at},
                },
            )
            return {"ok": False, "error": str(exc)}

    def _resolve_agent_llm_service(self, settings: dict[str, Any]) -> Any:
        if self.agent_llm_service is not None:
            return self.agent_llm_service
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
                "route_conflicts": evidence.get("route_conflicts", []),
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
        self.repository.delete_story_graph_chapter(novel_id, chapter_number)
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

    def get_global_route_map(self, novel_id: str) -> dict[str, Any]:
        return build_global_route_map(novel_id, self.repository.list_story_graph_chapters(novel_id))

    def list_story_graph_chapters(self, novel_id: str, limit: int = 50) -> dict[str, Any]:
        return {"items": self.repository.list_story_graph_chapters(novel_id, limit=limit)}

    def list_route_conflicts(self, novel_id: str, limit: int = 80) -> dict[str, Any]:
        return {"items": self.repository.list_route_conflicts(novel_id, limit=limit)}

    def list_review_records(self, novel_id: str, limit: int = 30) -> dict[str, Any]:
        return {"items": self.repository.list_review_records(novel_id, limit=limit)}

    def get_agent_status(self, novel_id: str) -> dict[str, Any]:
        return self.repository.get_agent_status(novel_id)

    def get_diagnostics(self, novel_id: str) -> dict[str, Any]:
        return self.diagnostics_service.get_diagnostics(novel_id)

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

        route_issues = _review_route_conflicts(evidence.get("route_conflicts") or [], chapter_number)
        if route_issues:
            issues.extend(_attach_issue_evidence(route_issues, evidence, subject=""))
        all_cards = (
            self.repository.list_all_character_cards(novel_id).get("items", [])
            if hasattr(self.repository, "list_all_character_cards")
            else self.repository.list_character_cards(novel_id).get("items", [])
        )
        pollution_issues = _review_extraction_pollution(
            all_cards,
            facts,
            chapter_number,
        )
        if pollution_issues:
            issues.extend(pollution_issues)
        boundary_issues = _review_boundary_state(
            self.repository.list_chapter_summaries(novel_id, before_chapter=chapter_number, limit=1),
            content,
            chapter_number,
        )
        if boundary_issues:
            issues.extend(boundary_issues)
        repetition_issues = _review_style_repetition(content, chapter_number)
        if repetition_issues:
            issues.extend(repetition_issues)
        host_context = self.host_context_reader.read(
            novel_id,
            query=content[:1200],
            before_chapter=chapter_number,
            limit=6,
        )
        self.repository.save_host_context_summary(novel_id, self.host_context_reader.summary(host_context))
        host_issues = _review_host_context_against_content(host_context, content, chapter_number)
        if host_issues:
            issues.extend(host_issues)

        if issues:
            suggestions.append("Evolution 建议优先补足角色得知信息、能力越界或误信被修正的过渡，而不是直接删除剧情推进。")
        issues = [_normalize_evolution_issue_metadata(item) for item in issues if isinstance(item, dict)]

        return {
            "ok": True,
            "data": {
                "issues": issues,
                "suggestions": suggestions,
                "reviewed_characters": [card.get("name") for card in mentioned_cards],
                "evidence": evidence.get("events", []),
                "constraints": evidence.get("constraints", []),
                "route_conflicts": evidence.get("route_conflicts", []),
                "host_context": self.host_context_reader.summary(host_context),
            },
        }

    def after_chapter_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        review_result = (payload.get("payload") or {}).get("review_result") or {}
        if not novel_id or not chapter_number:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number"}
        issues = review_result.get("issues") or []
        issue_items = [_normalize_evolution_issue_metadata(item) for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
        solidified, agent_events = solidify_capsules_from_review(
            novel_id=novel_id,
            chapter_number=chapter_number,
            issues=issue_items,
            existing_capsules=self.repository.list_agent_capsules(novel_id),
            at=_now(),
        )
        for capsule in solidified:
            self.repository.append_agent_capsule(novel_id, capsule)
        for event in agent_events:
            self.repository.append_agent_event(novel_id, event)
        selection = _matching_agent_selection(
            self.repository.list_agent_selection_records(novel_id, limit=30),
            chapter_number,
        )
        evaluated_genes, evaluated_capsules, evaluation_event = evaluate_strategy_effectiveness(
            novel_id=novel_id,
            chapter_number=chapter_number,
            issues=issue_items,
            selection=selection,
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            at=_now(),
        )
        if evaluation_event:
            self.repository.save_agent_genes(novel_id, evaluated_genes)
            for capsule in evaluated_capsules:
                if str(capsule.get("id") or "") in set(selection.get("selected_capsule_ids") or []):
                    self.repository.append_agent_capsule(novel_id, capsule)
            self.repository.append_agent_event(novel_id, evaluation_event)
        agent_api_record = None
        reflection_record = None
        agent_api_settings = self.get_settings(safe=False).get("agent_api")
        if solidified and isinstance(agent_api_settings, dict) and agent_api_settings.get("enabled"):
            agent_api_record = self._build_agent_reflection(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=solidified,
                issues=issue_items,
                settings=agent_api_settings,
            )
            reflection_record = agent_api_record.get("reflection") if isinstance(agent_api_record, dict) else None
        elif solidified:
            reflection_record = build_reflection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=solidified,
                issues=issue_items,
                source="deterministic_fallback",
                ok=True,
                at=_now(),
            )
        if reflection_record:
            self.repository.append_agent_reflection(novel_id, reflection_record)
            self.repository.append_agent_event(
                novel_id,
                {
                    "type": "EvolutionEvent",
                    "schema_version": 1,
                    "id": f"evt_reflection_saved_{_hash_text(novel_id + str(chapter_number) + str(reflection_record.get('id') or ''))}",
                    "intent": "reflect",
                    "hook_name": "after_chapter_review",
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "signals": ["review_reflection", "agent_memory"],
                    "genes_used": [],
                    "capsule_id": None,
                    "outcome": {"status": "success", "reflection_id": reflection_record.get("id")},
                    "meta": {"at": _now(), "source": reflection_record.get("source")},
                },
            )
        candidate_records, memory_index, candidate_events = consolidate_agent_memory(
            novel_id=novel_id,
            chapter_number=chapter_number,
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            reflections=self.repository.list_agent_reflections(novel_id),
            existing_candidates=self.repository.list_agent_gene_candidates(novel_id),
            at=_now(),
        )
        for candidate in candidate_records:
            self.repository.append_agent_gene_candidate(novel_id, candidate)
        for event in candidate_events:
            self.repository.append_agent_event(novel_id, event)
        self.repository.save_agent_memory_index(novel_id, memory_index)
        self.repository.append_review_record(
            novel_id,
            {
                "chapter_number": chapter_number,
                "issue_count": len(issue_items),
                "issues": issue_items[:12],
                "issue_types": [str(item.get("issue_type") or "") for item in issue_items[:12]],
                "overall_score": review_result.get("overall_score"),
                "source": str(payload.get("source") or "chapter_review_service"),
                "solidified_capsules": [capsule.get("id") for capsule in solidified],
                "selection_id": selection.get("id") if selection else None,
                "strategy_evaluation": evaluation_event.get("outcome") if evaluation_event else None,
                "agent_api_reflection": agent_api_record,
                "reflection_id": reflection_record.get("id") if reflection_record else None,
                "gene_candidates": [candidate.get("id") for candidate in candidate_records],
                "at": _now(),
            },
        )
        return {
            "ok": True,
            "data": {
                "recorded": True,
                "chapter_number": chapter_number,
                "solidified_capsules": solidified,
                "agent_api_reflection": agent_api_record,
                "reflection": reflection_record,
                "gene_candidates": candidate_records,
                "memory_index": memory_index,
            },
        }

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
        route_map = self.get_global_route_map(novel_id)
        host_context = self._read_host_context_safe(novel_id, chapter_number, outline=outline)
        semantic_memory = self._read_semantic_memory_safe(novel_id, chapter_number, outline=outline)
        self._save_context_dependency_summaries(novel_id, host_context, semantic_memory)
        review_records = self.repository.list_review_records(novel_id, limit=10)
        style_repetition_state = self.repository.get_style_repetition_state(novel_id)
        agent_selection = select_agent_assets(
            novel_id=novel_id,
            chapter_number=chapter_number,
            signals=extract_context_signals(
                outline=outline,
                chapter_summaries=chapter_summaries,
                route_map=route_map,
                semantic_memory=semantic_memory,
                review_records=review_records,
                host_context=host_context,
            ),
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            outline=outline,
            at=_now(),
        )
        return build_context_patch(
            novel_id,
            chapter_number,
            characters,
            facts,
            outline=outline,
            chapter_summaries=chapter_summaries,
            volume_summaries=volume_summaries,
            previous_injections=previous_injections,
            route_map=route_map,
            semantic_memory=semantic_memory,
            host_context=host_context,
            agent_selection=agent_selection,
            style_repetition_state=style_repetition_state,
        )

    def _read_host_context_safe(self, novel_id: str, chapter_number: Optional[int], *, outline: str) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            return self.host_context_reader.read(
                novel_id,
                query=outline,
                before_chapter=chapter_number,
                limit=6,
            )

        result = _call_with_timeout(read, timeout_seconds=CONTEXT_EXTERNAL_TIMEOUT_SECONDS)
        if result.get("ok") and isinstance(result.get("value"), dict):
            return result["value"]
        reason = "host_context_timeout" if result.get("timeout") else "host_context_failed"
        if result.get("error"):
            logger.warning("Evolution host context degraded for %s: %s", novel_id, result["error"])
        return _empty_host_context(novel_id, before_chapter=chapter_number, reason=reason)

    def _read_semantic_memory_safe(self, novel_id: str, chapter_number: Optional[int], *, outline: str) -> dict[str, Any]:
        def search() -> dict[str, Any]:
            return self.semantic_memory.search(
                novel_id,
                outline,
                before_chapter=chapter_number,
                limit=8,
            )

        result = _call_with_timeout(search, timeout_seconds=CONTEXT_EXTERNAL_TIMEOUT_SECONDS)
        if result.get("ok") and isinstance(result.get("value"), dict):
            return result["value"]
        reason = "semantic_recall_timeout" if result.get("timeout") else "semantic_recall_failed"
        if result.get("error"):
            logger.warning("Evolution semantic recall degraded for %s: %s", novel_id, result["error"])
        return {
            "items": [],
            "source": reason,
            "vector_enabled": False,
            "collection_status": {"enabled": False, "degraded_reason": reason},
        }

    def _save_context_dependency_summaries(
        self,
        novel_id: str,
        host_context: dict[str, Any],
        semantic_memory: dict[str, Any],
    ) -> None:
        try:
            self.repository.save_host_context_summary(novel_id, self.host_context_reader.summary(host_context))
        except Exception as exc:
            logger.warning("Evolution host context summary write failed for %s: %s", novel_id, exc)
        try:
            self.repository.save_semantic_recall_summary(
                novel_id,
                {
                    "source": semantic_memory.get("source"),
                    "vector_enabled": bool(semantic_memory.get("vector_enabled")),
                    "item_count": len(semantic_memory.get("items") or []),
                    "source_types": sorted({str(item.get("source_type") or "") for item in semantic_memory.get("items") or [] if isinstance(item, dict)}),
                    "collection_status": semantic_memory.get("collection_status") or {},
                },
            )
        except Exception as exc:
            logger.warning("Evolution semantic recall summary write failed for %s: %s", novel_id, exc)

    def _read_native_after_commit_context(self, *, novel_id: str, chapter_number: int, content: str) -> dict[str, Any]:
        try:
            host_context = self.host_context_reader.read(
                novel_id,
                query=content[:1200],
                before_chapter=chapter_number + 1,
                limit=6,
            )
            summary = self.host_context_reader.summary(host_context)
            self.repository.save_host_context_summary(novel_id, summary)
        except Exception as exc:
            logger.warning("Evolution native after-commit context read failed for %s ch%s: %s", novel_id, chapter_number, exc)
            summary = _empty_host_context(novel_id, before_chapter=chapter_number + 1, reason="native_after_commit_failed")
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        native_counts = {
            key: int(counts.get(key) or 0)
            for key in ("story_knowledge", "triples", "foreshadow", "storyline", "timeline", "dialogue", "memory_engine")
        }
        has_native_sync = any(native_counts.values())
        return {
            "source": "plotpilot_native_after_commit",
            "chapter_number": chapter_number,
            "has_native_sync": has_native_sync,
            "fallback_degraded": not has_native_sync,
            "native_counts": native_counts,
            "degraded_sources": list(summary.get("degraded_sources") or []),
            "empty_sources": list(summary.get("empty_sources") or []),
            "suggestion": "" if has_native_sync else "PlotPilot 原生章后同步尚未命中；本章 Evolution 抽取仅作为 degraded fallback，不覆盖宿主主库。",
        }

    def build_context_summary(self, novel_id: str, chapter_number: Optional[int], *, outline: str = "") -> str:
        return render_patch_summary(self.build_context_patch(novel_id, chapter_number, outline=outline))

    def _build_agent_reflection(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        capsules: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = _now()
        try:
            llm_service = self._resolve_agent_llm_service(settings)
            prompt = _build_agent_reflection_prompt(chapter_number=chapter_number, capsules=capsules, issues=issues)
            result = _run_async_blocking(
                llm_service.generate(
                    prompt,
                    _make_generation_config(
                        model=str(settings.get("model") or ""),
                        max_tokens=_clamp_int(settings.get("max_tokens"), 128, 2048, 800),
                        temperature=_clamp_float(settings.get("temperature"), 0.0, 2.0, 0.1),
                    ),
                )
            )
            content = str(result.content or "").strip()[:1200]
            finished_at = _now()
            structured = _parse_agent_reflection_json(content)
            reflection = build_reflection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=capsules,
                issues=issues,
                content=content,
                structured=structured,
                source="agent_api",
                model=str(settings.get("model") or ""),
                token_usage=_token_usage_to_dict(getattr(result, "token_usage", None)),
                ok=True,
                at=finished_at,
            )
            record = {
                "ok": True,
                "started_at": started_at,
                "at": finished_at,
                "chapter_number": chapter_number,
                "provider_mode": settings.get("provider_mode"),
                "model": str(settings.get("model") or ""),
                "capsule_ids": [str(item.get("id") or "") for item in capsules],
                "content": content,
                "structured": structured,
                "reflection": reflection,
                "token_usage": _token_usage_to_dict(getattr(result, "token_usage", None)),
            }
            self.repository.append_agent_event(
                novel_id,
                {
                    "type": "EvolutionEvent",
                    "schema_version": 1,
                    "id": f"evt_agent_api_{_hash_text(novel_id + str(chapter_number) + finished_at)}",
                    "intent": "reflect",
                    "hook_name": "after_chapter_review",
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "signals": ["agent_api", "review_reflection"],
                    "genes_used": [],
                    "capsule_id": None,
                    "outcome": {"status": "success", "capsule_count": len(capsules)},
                    "meta": {"at": finished_at, "model": str(settings.get("model") or "")},
                },
            )
            return record
        except Exception as exc:
            failed_at = _now()
            reflection = build_reflection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=capsules,
                issues=issues,
                source="agent_api_fallback",
                ok=False,
                error=str(exc),
                at=failed_at,
            )
            self.repository.append_agent_event(
                novel_id,
                {
                    "type": "EvolutionEvent",
                    "schema_version": 1,
                    "id": f"evt_agent_api_failed_{_hash_text(novel_id + str(chapter_number) + failed_at)}",
                    "intent": "reflect",
                    "hook_name": "after_chapter_review",
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "signals": ["agent_api", "review_reflection"],
                    "genes_used": [],
                    "capsule_id": None,
                    "outcome": {"status": "failed", "error": str(exc)},
                    "meta": {"at": failed_at},
                },
            )
            return {"ok": False, "started_at": started_at, "at": failed_at, "error": str(exc), "reflection": reflection}


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
    palette = card.get("personality_palette") if isinstance(card.get("personality_palette"), dict) else {}
    missing_palette_fields = _missing_palette_fields(palette)
    if missing_palette_fields:
        issue = _review_issue(
            "evolution_palette_missing",
            "warning",
            f"{name} 本章出场，但人物卡性格调色盘仍缺少：{', '.join(missing_palette_fields)}。",
            chapter_number,
            "不要只写性格标签；请从本章动作、选择和关系反应中推断底色、主色调与点缀。",
        )
        issue["evidence"] = [{"character": name, "missing_fields": missing_palette_fields}]
        issues.append(issue)
    elif _looks_like_palette_drift(content) and not _has_transition_marker(content):
        issue = _review_issue(
            "evolution_palette_drift",
            "warning",
            f"{name} 本章出现明显性格反转/漂移表述，但缺少情境压力、关系触发或成长过渡。",
            chapter_number,
            "如要违背既有调色盘，请写出触发条件；否则让行为回到既有底色、主色调和点缀的衍生范围。",
        )
        issue["evidence"] = [
            {
                "character": name,
                "base": palette.get("base"),
                "main_tones": _as_strings(palette.get("main_tones"))[:4],
                "sample": str(content or "")[:240],
            }
        ]
        issues.append(issue)
    return issues


def _review_issue(issue_type: str, severity: str, description: str, chapter_number: int, suggestion: str) -> dict[str, Any]:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "location": f"Chapter {chapter_number}",
        "suggestion": suggestion,
    }


def _normalize_evolution_issue_metadata(issue: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(issue)
    issue_type = str(normalized.get("issue_type") or "")
    if issue_type.startswith("evolution_"):
        normalized.setdefault("source_plugin", PLUGIN_NAME)
    normalized.setdefault("issue_family", _issue_family(issue_type))
    normalized.setdefault("suggestion", "")
    evidence = normalized.get("evidence")
    if evidence is None:
        normalized["evidence"] = []
    elif isinstance(evidence, dict):
        normalized["evidence"] = [evidence]
    elif not isinstance(evidence, list):
        normalized["evidence"] = [{"value": str(evidence)}]
    if "host_source_refs" not in normalized:
        refs = []
        for item in normalized.get("evidence") or []:
            if isinstance(item, dict) and (item.get("source") or item.get("source_type") or item.get("id")):
                refs.append(
                    {
                        "source": item.get("source") or item.get("source_type") or "",
                        "id": item.get("id"),
                        "source_type": item.get("source_type"),
                    }
                )
        normalized["host_source_refs"] = refs
    return normalized


def _issue_family(issue_type: str) -> str:
    text = str(issue_type or "")
    for marker, family in (
        ("route", "route"),
        ("boundary", "boundary_state"),
        ("palette", "personality_palette"),
        ("pollution", "entity_pollution"),
        ("style_repetition", "style_repetition"),
        ("bible", "bible"),
        ("story_knowledge", "story_knowledge"),
        ("storyline", "storyline"),
        ("foreshadow", "foreshadow"),
        ("timeline", "timeline"),
        ("chronicle", "chronicle"),
        ("dialogue", "dialogue"),
        ("triple", "triples"),
        ("memory_engine", "memory_engine"),
        ("knowledge", "knowledge"),
        ("worldbuilding", "worldbuilding"),
    ):
        if marker in text:
            return family
    return text.replace("evolution_", "") or "general"


def _review_host_context_against_content(host_context: dict[str, Any], content: str, chapter_number: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    text = str(content or "")
    for source, issue_type, label in (
        ("bible", "evolution_bible_context", "Bible 人物/地点边界"),
        ("world", "evolution_worldbuilding_context", "世界观/地点设定"),
        ("knowledge", "evolution_knowledge_context", "知识库事实"),
        ("story_knowledge", "evolution_story_knowledge_context", "章后叙事同步"),
        ("storyline", "evolution_storyline_context", "故事线"),
        ("timeline", "evolution_timeline_context", "时间线"),
        ("chronicle", "evolution_chronicle_context", "编年史"),
        ("foreshadow", "evolution_foreshadow_context", "伏笔账本"),
        ("dialogue", "evolution_dialogue_voice_context", "对话声线样本"),
        ("triples", "evolution_triples_context", "图谱三元组"),
        ("memory_engine", "evolution_memory_engine_context", "MemoryEngine fact lock"),
    ):
        matches = _host_context_mentions(host_context.get(source) or [], text)
        if not matches:
            continue
        issue = _review_issue(
            issue_type,
            "warning",
            f"本章触及 PlotPilot {label} 中的既有信息：{', '.join(str(item.get('name') or item.get('id') or '') for item in matches[:3])}。",
            chapter_number,
            f"写作与审查时应显式核对 {label}；如要偏离，需要在正文中给出转场、解释、误导或回收依据。",
        )
        evidence = [
            {
                "source": source,
                "id": item.get("id"),
                "name": item.get("name"),
                "description": item.get("description"),
                "source_type": item.get("source_type"),
            }
            for item in matches[:4]
        ]
        issue["source_plugin"] = "world_evolution_core"
        issue["issue_family"] = source
        issue["host_source_refs"] = [{"source": item["source"], "id": item.get("id"), "source_type": item.get("source_type")} for item in evidence]
        issue["evidence"] = evidence
        issues.append(issue)
    return issues[:6]


def _host_context_mentions(items: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    matches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        terms = [str(item.get("name") or "").strip(), str(item.get("kind") or "").strip()]
        terms.extend(_extract_short_terms(item.get("description")))
        if any(term and len(term) >= 2 and term in text for term in terms[:8]):
            matches.append(item)
    return matches


def _extract_short_terms(value: Any) -> list[str]:
    terms = []
    current = []
    for char in str(value or ""):
        if "\u4e00" <= char <= "\u9fff" or char.isalnum():
            current.append(char)
            continue
        if 2 <= len(current) <= 12:
            terms.append("".join(current))
        current = []
    if 2 <= len(current) <= 12:
        terms.append("".join(current))
    return terms[:6]


def _matching_agent_selection(records: list[dict[str, Any]], chapter_number: int) -> dict[str, Any]:
    for record in reversed(records or []):
        if _int_or_none(record.get("chapter_number")) == chapter_number:
            return record
    return {}


_REPETITION_PHRASES = [
    "没有说话",
    "没有回答",
    "喉咙发紧",
    "深吸一口气",
    "沉默几秒",
    "沉默了几秒",
    "声音很轻",
    "掌心发烫",
    "像是等",
]


_NON_CHARACTER_ENTITY_NAMES = {
    "金属牌",
    "方向",
    "查询记录",
    "记录",
    "编号",
    "债务",
    "契约",
    "防火门",
    "黑色书籍",
    "书籍",
    "访客卡",
    "臂章",
    "钥匙",
    "黑匣子",
    "章节标题",
    "标题",
    "线索",
    "真相",
    "秘密",
    "记忆",
    "沉默",
}
_NON_CHARACTER_ENTITY_TOKENS = (
    "金属",
    "查询",
    "记录",
    "方向",
    "编号",
    "钥匙",
    "防火门",
    "书籍",
    "教程",
    "章节",
    "标题",
    "线索",
    "真相",
    "秘密",
    "记忆",
    "警报",
)
_NON_CHARACTER_ENTITY_SUFFIXES = ("之谜", "真相", "记录", "线索", "计划", "任务", "报告")
_BAD_LOCATION_NAMES = {"专门", "道防火门", "个信息站", "老板专门", "但他咬牙站"}
_BAD_LOCATION_PARTS = ("咬牙", "老板", "专门", "那道", "这道")


def _filter_snapshot_characters(names: list[Any]) -> list[str]:
    return _dedupe_runtime(str(name).strip() for name in names if _valid_snapshot_character_name(str(name or "")))


def _filter_snapshot_locations(names: list[Any]) -> list[str]:
    return _dedupe_runtime(str(name).strip() for name in names if _valid_snapshot_location_name(str(name or "")))


def _valid_snapshot_character_name(name: str) -> bool:
    value = str(name or "").strip()
    if not value or value in _NON_CHARACTER_ENTITY_NAMES:
        return False
    if any(token in value for token in _NON_CHARACTER_ENTITY_TOKENS):
        return False
    if any(value.endswith(suffix) for suffix in _NON_CHARACTER_ENTITY_SUFFIXES):
        return False
    if value.startswith("第") and ("章" in value or "幕" in value):
        return False
    if 6 < len(value) and not any(token in value for token in ("·", "氏", "家", "队", "团")):
        return False
    return True


def _valid_snapshot_location_name(name: str) -> bool:
    value = str(name or "").strip()
    if len(value) < 2 or value in _BAD_LOCATION_NAMES:
        return False
    if any(token in value for token in _BAD_LOCATION_PARTS):
        return False
    return True


def _dedupe_runtime(items: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _build_style_repetition_state(
    *,
    novel_id: str,
    chapter_number: int,
    content: str,
    recent_summaries: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    text = "\n".join([*(str(item.get("short_summary") or "") for item in recent_summaries[-3:]), str(content or "")])
    phrases = []
    for phrase in _REPETITION_PHRASES:
        count = text.count(phrase)
        if count >= 3:
            phrases.append(
                {
                    "phrase": phrase,
                    "count": count,
                    "chapters": [chapter_number],
                    "replacement_guidance": _replacement_guidance_for_phrase(phrase),
                }
            )
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "window": "recent_3_chapters_plus_current",
        "phrases": sorted(phrases, key=lambda item: (-int(item.get("count") or 0), str(item.get("phrase") or "")))[:10],
        "at": at,
    }


def _review_style_repetition(content: str, chapter_number: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for phrase in _REPETITION_PHRASES:
        count = str(content or "").count(phrase)
        if count < 4:
            continue
        issue = _review_issue(
            "evolution_style_repetition",
            "warning",
            f"本章高频重复反应模板「{phrase}」出现 {count} 次，容易形成机械化表达。",
            chapter_number,
            _replacement_guidance_for_phrase(phrase),
        )
        issue["evidence"] = [{"phrase": phrase, "count": count, "sample": _sample_phrase_context(content, phrase)}]
        issues.append(issue)
    return issues


def _review_extraction_pollution(cards: list[dict[str, Any]], facts: list[dict[str, Any]], chapter_number: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    invalid_cards = [card for card in cards if str(card.get("status") or "") == "invalid_entity"]
    if invalid_cards:
        names = [str(card.get("name") or "") for card in invalid_cards[:6]]
        issue = _review_issue(
            "evolution_character_pollution",
            "warning",
            f"人物卡检测到非人物实体污染：{', '.join(names)}。",
            chapter_number,
            "将物品、方向、查询记录、抽象概念放入 world facts 或 props，不要注入人物卡主上下文。",
        )
        issue["evidence"] = [
            {
                "names": names,
                "count": len(invalid_cards),
                "entities": [
                    {
                        "name": str(card.get("name") or ""),
                        "first_seen_chapter": card.get("first_seen_chapter"),
                        "last_seen_chapter": card.get("last_seen_chapter"),
                        "invalid_reason": card.get("invalid_reason"),
                    }
                    for card in invalid_cards[:6]
                ],
            }
        ]
        issues.append(issue)
    bad_locations: list[str] = []
    for fact in facts:
        for location in fact.get("locations") or []:
            value = str(location or "").strip()
            if value in {"但他咬牙站", "个信息站", "老板专门", "道防火门"} or any(token in value for token in ("咬牙", "老板", "专门")):
                bad_locations.append(value)
    if bad_locations:
        issue = _review_issue(
            "evolution_location_pollution",
            "warning",
            f"地点列表检测到疑似半句残片：{', '.join(bad_locations[:6])}。",
            chapter_number,
            "地点必须是空间名词、地图节点或上下文位置表达；动词残片和半句不要进入路线图。",
        )
        issue["evidence"] = [{"locations": bad_locations[:8], "count": len(bad_locations)}]
        issues.append(issue)
    return issues


def _review_boundary_state(previous_summaries: list[dict[str, Any]], content: str, chapter_number: int) -> list[dict[str, Any]]:
    if not previous_summaries:
        return []
    previous = previous_summaries[-1]
    carry = previous.get("carry_forward") if isinstance(previous.get("carry_forward"), dict) else {}
    previous_locations = [str(item) for item in carry.get("last_known_locations") or [] if str(item).strip()]
    if not previous_locations:
        return []
    opening = str(content or "")[:520]
    if any(location and location in opening for location in previous_locations):
        if any(token in opening for token in ("才找到", "第一次找到", "重新进入", "又一次进入", "再次抵达", "终于找到")):
            return [
                _boundary_issue(
                    chapter_number,
                    "上一章结尾已将角色停在同一地点，本章开头又写成重新/首次抵达，疑似章节首尾回滚。",
                    previous,
                    opening,
                )
            ]
        return []
    if any(token in opening for token in ("回到", "来到", "抵达", "进入", "走进")) and not any(
        token in opening for token in ("后来", "数小时后", "第二天", "转场", "离开", "赶往", "沿着", "穿过", "绕过")
    ):
        return [
            _boundary_issue(
                chapter_number,
                f"上一章终点在 {', '.join(previous_locations[:3])}，本章开头切换地点但缺少明确移动/跳时桥段。",
                previous,
                opening,
            )
        ]
    return []


def _boundary_issue(chapter_number: int, description: str, previous: dict[str, Any], opening: str) -> dict[str, Any]:
    issue = _review_issue(
        "evolution_boundary_state",
        "warning",
        description,
        chapter_number,
        "下一章开头必须承接上一章终点；若跳时空，先补一句转场、移动路径或视角桥接。",
    )
    ending = previous.get("ending_state") if isinstance(previous.get("ending_state"), dict) else {}
    issue["evidence"] = [
        {
            "previous_chapter": previous.get("chapter_number"),
            "previous_ending": str(ending.get("excerpt") or "")[:220],
            "current_opening": str(opening or "")[:220],
        }
    ]
    return issue


def _replacement_guidance_for_phrase(phrase: str) -> str:
    if phrase in {"没有说话", "没有回答", "沉默几秒", "沉默了几秒"}:
        return "用手部动作、视线落点、站位变化或物件处理替代沉默模板，并让沉默推动关系或信息差。"
    if phrase in {"喉咙发紧", "深吸一口气", "声音很轻"}:
        return "改用更具体的身体反应、环境压迫或句式节奏，不要重复同一生理模板。"
    return "替换为场景化动作和可观察细节，让反应承担新的剧情信息。"


def _sample_phrase_context(content: str, phrase: str) -> str:
    text = str(content or "")
    index = text.find(phrase)
    if index < 0:
        return ""
    return text[max(0, index - 50) : index + len(phrase) + 50]


def _review_route_conflicts(conflicts: list[dict[str, Any]], chapter_number: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        current_chapter = _int_or_none(conflict.get("chapter_current"))
        if current_chapter != chapter_number:
            continue
        conflict_type = str(conflict.get("type") or "route_conflict").strip() or "route_conflict"
        message = str(conflict.get("message") or "").strip()
        if not message:
            continue
        key = f"{conflict_type}:{message}"
        if key in seen:
            continue
        seen.add(key)
        severity = "critical" if str(conflict.get("severity") or "") == "hard" else "warning"
        suggestion = _route_conflict_suggestion(conflict_type)
        issue_type = "evolution_route_missing_transition" if conflict_type == "location_jump_without_bridge" else f"evolution_route_{conflict_type}"
        issue = _review_issue(
            issue_type,
            severity,
            message,
            chapter_number,
            suggestion,
        )
        issue["evidence"] = [
            {
                "type": conflict_type,
                "severity": conflict.get("severity"),
                "character": conflict.get("character"),
                "chapter_previous": conflict.get("chapter_previous"),
                "chapter_current": conflict.get("chapter_current"),
                "previous_location": conflict.get("previous_location"),
                "current_location": conflict.get("current_location"),
                "evidence": conflict.get("evidence"),
            }
        ]
        issues.append(issue)
    return issues


def _route_conflict_suggestion(conflict_type: str) -> str:
    if conflict_type == "repeated_arrival":
        return "如果角色上一章结尾已经在该地点，本章开头应承接在场状态；若要重新进入，请补足离开、转场和再次抵达的因果。"
    if conflict_type == "location_jump_without_bridge":
        return "补写移动桥段、跳时提示或视角切换，让读者知道角色如何从上一地点到达当前地点。"
    if conflict_type == "missing_transition":
        return "补写移动桥段、跳时提示或视角切换，让读者知道角色如何从上一地点到达当前地点。"
    if conflict_type == "boundary_rollback":
        return "承接上一章终点；如果回到旧地点，必须先交代离开与再次抵达。"
    if conflict_type == "multi_location_same_chapter":
        return "明确同章内的移动顺序和时间间隔，避免同一角色像同时存在于多个地点。"
    return "核对人物上一章终点、本章起点和场景移动链，补足必要过渡。"


def _missing_palette_fields(palette: Any) -> list[str]:
    if not isinstance(palette, dict):
        return ["base", "main_tones", "derivatives"]
    missing = []
    if not str(palette.get("base") or "").strip():
        missing.append("base")
    if not palette.get("main_tones"):
        missing.append("main_tones")
    if not palette.get("derivatives"):
        missing.append("derivatives")
    return missing


def _looks_like_palette_drift(content: str) -> bool:
    text = str(content or "")
    return any(token in text for token in ("突然变得", "一反常态", "完全不像自己", "像换了个人", "毫无理由地"))


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
                "name": "Evolution Legacy API2",
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
                "notes": "Deprecated legacy API2 settings; Evolution now uses agent_api.",
                "use_legacy_chat_completions": False,
            },
        },
        "agent_api": {
            "enabled": False,
            "provider_mode": "same_as_main",
            "model": "",
            "temperature": 0.1,
            "max_tokens": 800,
            "custom_profile": {
                "id": "evolution-agent-custom",
                "name": "Evolution Agent API",
                "preset_key": "custom-openai-compatible",
                "protocol": "openai",
                "base_url": "",
                "api_key": "",
                "model": "",
                "temperature": 0.1,
                "max_tokens": 800,
                "timeout_seconds": 180,
                "extra_headers": {},
                "extra_query": {},
                "extra_body": {},
                "notes": "Evolution 智能体反思与策略固化专用 API",
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
    api2["provider_mode"] = provider_mode if provider_mode in LLM_PROVIDER_MODES else "same_as_main"
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
    agent = base["agent_api"]
    agent["enabled"] = bool(agent.get("enabled"))
    agent_mode = str(agent.get("provider_mode") or "same_as_main")
    agent["provider_mode"] = agent_mode if agent_mode in LLM_PROVIDER_MODES else "same_as_main"
    agent["temperature"] = _clamp_float(agent.get("temperature"), 0.0, 2.0, 0.1)
    agent["max_tokens"] = _clamp_int(agent.get("max_tokens"), 128, 2048, 800)
    agent_custom = _custom_profile_for_storage(
        agent.get("custom_profile") if isinstance(agent.get("custom_profile"), dict) else {},
        profile_id="evolution-agent-custom",
        profile_name="Evolution Agent API",
        notes="Evolution 智能体反思与策略固化专用 API",
        default_temperature=0.1,
        default_max_tokens=800,
    )
    if existing:
        prior = ((existing.get("agent_api") or {}).get("custom_profile") or {}) if isinstance(existing, dict) else {}
        submitted_key = str(agent_custom.get("api_key") or "")
        if submitted_key in {"", "********", "••••••••"}:
            agent_custom["api_key"] = str(prior.get("api_key") or "")
    agent["custom_profile"] = agent_custom
    if agent["provider_mode"] == "custom":
        agent["model"] = agent_custom.get("model") or ""
        agent["temperature"] = agent_custom.get("temperature", agent["temperature"])
        agent["max_tokens"] = agent_custom.get("max_tokens", agent["max_tokens"])
    return base


def _redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    safe = copy.deepcopy(settings)
    for key in ("api2_control_card", "agent_api"):
        custom = ((safe.get(key) or {}).get("custom_profile") or {})
        api_key = str(custom.get("api_key") or "")
        custom["api_key"] = ""
        custom["api_key_configured"] = bool(api_key)
    return safe


def _call_with_timeout(fn: Any, *, timeout_seconds: float) -> dict[str, Any]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="evolution-context")
    future = executor.submit(fn)
    try:
        return {"ok": True, "value": future.result(timeout=timeout_seconds)}
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return {"ok": False, "timeout": True}
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        return {"ok": False, "error": str(exc)[:240]}
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _empty_host_context(novel_id: str, *, before_chapter: Optional[int], reason: str) -> dict[str, Any]:
    counts = {key: 0 for key in HOST_CONTEXT_SOURCES}
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "source": "plotpilot_host_readonly",
        "before_chapter": before_chapter,
        "active_sources": [],
        "degraded_sources": [reason],
        "counts": counts,
        "plotpilot_context_usage": {
            "source": "plotpilot_native_context_adapter",
            "mode": "strategy_only",
            "hit_counts_by_tier": {},
            "degraded_sources": [reason],
            "long_context_duplicated": False,
        },
        **{key: [] for key in HOST_CONTEXT_SOURCES},
    }


def _build_agent_models_request(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    return _build_llm_models_request(payload, settings, key="agent_api")


def _build_llm_models_request(payload: dict[str, Any], settings: dict[str, Any], *, key: str) -> dict[str, Any]:
    channel = settings.get(key) if isinstance(settings.get(key), dict) else {}
    saved_custom = channel.get("custom_profile") if isinstance(channel.get("custom_profile"), dict) else {}
    submitted_channel = payload.get(key) if isinstance(payload.get(key), dict) else payload
    submitted_custom = submitted_channel.get("custom_profile") if isinstance(submitted_channel.get("custom_profile"), dict) else {}
    provider_mode = str(submitted_channel.get("provider_mode") or channel.get("provider_mode") or "same_as_main")
    if provider_mode not in LLM_PROVIDER_MODES:
        provider_mode = "same_as_main"

    if provider_mode == "custom":
        protocol = str(submitted_custom.get("protocol") or saved_custom.get("protocol") or "openai").strip()
        if protocol not in LLM_MODEL_PROTOCOLS:
            protocol = "openai"
        api_key = str(submitted_custom.get("api_key") or "").strip()
        if api_key in {"", "********", "••••••••"}:
            api_key = str(saved_custom.get("api_key") or "").strip()
        return {
            "source": "custom",
            "protocol": protocol,
            "base_url": str(submitted_custom.get("base_url") or saved_custom.get("base_url") or "").strip(),
            "api_key": api_key,
            "timeout_ms": _clamp_int(payload.get("timeout_ms"), 1000, 120000, 30000),
        }

    active_protocol = str(submitted_channel.get("protocol") or "openai").strip() or "openai"
    active_base_url = str(submitted_channel.get("base_url") or "").strip()
    active_api_key = str(submitted_channel.get("api_key") or "").strip()
    if not active_api_key:
        try:
            from application.ai.llm_control_service import LLMControlService

            active_profile = LLMControlService().get_active_profile()
            if active_profile:
                active_protocol = str(active_profile.protocol or active_protocol)
                active_base_url = str(active_profile.base_url or active_base_url)
                active_api_key = str(active_profile.api_key or "")
        except Exception:
            pass

    return {
        "source": "same_as_main",
        "protocol": active_protocol if active_protocol in LLM_MODEL_PROTOCOLS else "openai",
        "base_url": active_base_url,
        "api_key": active_api_key,
        "timeout_ms": _clamp_int(payload.get("timeout_ms"), 1000, 120000, 30000),
    }


def _agent_runtime_settings_from_payload(payload: dict[str, Any], saved_settings: dict[str, Any]) -> dict[str, Any]:
    raw_agent = payload.get("agent_api") if isinstance(payload.get("agent_api"), dict) else payload
    return _normalize_settings({"agent_api": raw_agent}, existing=saved_settings)["agent_api"]


async def _fetch_model_list_items(request: dict[str, Any]) -> list[dict[str, str]]:
    import httpx

    api_key = str(request.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("API key is required to fetch model list")

    protocol = str(request.get("protocol") or "openai").strip()
    base_url = str(request.get("base_url") or "").strip()
    timeout = max(1.0, _clamp_int(request.get("timeout_ms"), 1000, 120000, 30000) / 1000)
    headers: dict[str, str]
    params: Optional[dict[str, str]] = None

    if protocol == "anthropic":
        url = f"{(base_url or 'https://api.anthropic.com').rstrip('/')}/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif protocol == "gemini":
        url = f"{_gemini_models_base(base_url)}/models"
        headers = {}
        params = {"key": api_key}
    else:
        url = f"{_openai_models_base(base_url)}/models"
        headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:400].replace("\n", " ")
        raise RuntimeError(f"上游模型列表 HTTP {exc.response.status_code}：{body or exc.response.reason_phrase}（请求 {url}）") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"连接上游失败：{exc}（请求 {url}）") from exc
    except ValueError as exc:
        raise RuntimeError(f"上游未返回 JSON（请求 {url}）") from exc

    return _normalize_model_list_items(data, protocol)


def _normalize_model_list_items(data: dict[str, Any], protocol: str) -> list[dict[str, str]]:
    if protocol == "gemini":
        raw_items = data.get("models", [])
    else:
        raw_items = data.get("data", [])
    if not isinstance(raw_items, list):
        return []

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id") or entry.get("name") or entry.get("model")
        model_id = str(raw_id or "").strip()
        if protocol == "gemini" and model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        items.append(
            {
                "id": model_id,
                "name": str(entry.get("displayName") or entry.get("name") or model_id).removeprefix("models/"),
                "owned_by": str(entry.get("owned_by") or entry.get("ownedBy") or entry.get("publisher") or ""),
            }
        )
    return items


def _openai_models_base(base_url: str) -> str:
    raw = (base_url or "").strip() or "https://api.openai.com/v1"
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/v1"
    else:
        path = "/" + path.lstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", "")).rstrip("/")


def _gemini_models_base(base_url: str) -> str:
    raw = (base_url or "").strip().rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
    for suffix in ("/models", "/v1beta/models", "/v1/models"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
            break
    return raw


def _build_llm_connection_test_prompt() -> Any:
    try:
        from domain.ai.value_objects.prompt import Prompt

        return Prompt(system="你是 API 连接测试器。", user="请只回复 OK 两个字母，不要添加任何解释。")
    except Exception:
        class PromptFallback:
            def __init__(self) -> None:
                self.system = "你是 API 连接测试器。"
                self.user = "请只回复 OK 两个字母，不要添加任何解释。"

        return PromptFallback()


def _build_agent_reflection_prompt(*, chapter_number: int, capsules: list[dict[str, Any]], issues: list[dict[str, Any]]) -> Any:
    system = (
        "你是 Evolution 智能体的反思器，不写小说正文。"
        "你只总结本轮审查暴露出的可复用经验，帮助后续章节减少连续性和人物逻辑错误。"
        "必须输出 JSON 对象，不要输出 Markdown。"
    )
    capsule_lines = "\n".join(
        f"- {item.get('title') or item.get('id')}：{item.get('guidance') or item.get('summary')}"
        for item in capsules[:6]
    )
    issue_lines = "\n".join(
        f"- [{item.get('severity')}] {item.get('issue_type')}：{item.get('description')}｜建议：{item.get('suggestion')}"
        for item in issues[:8]
    )
    user = f"""【章节】
第{chapter_number}章

【本轮固化 Capsule】
{capsule_lines or '无'}

【审查问题】
{issue_lines or '无'}

请输出 JSON：
{{
  "problem_pattern": "本轮问题模式，80字内",
  "root_cause": "根因，160字内",
  "next_chapter_constraints": ["后续可执行约束1", "后续可执行约束2"],
  "evidence_refs": [{{"summary": "引用证据摘要"}}],
  "suggest_gene_candidate": false
}}

要求：
1. 只写后续可执行的写作/审查策略。
2. 不复述完整剧情。
3. 不新增事实设定。
4. 优先处理章节承接、人物路线、认知边界、能力边界、性格调色盘。"""
    try:
        from domain.ai.value_objects.prompt import Prompt

        return Prompt(system=system, user=user)
    except Exception:
        class PromptFallback:
            def __init__(self) -> None:
                self.system = system
                self.user = user

        return PromptFallback()


def _parse_agent_reflection_json(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _custom_profile_for_storage(
    raw: dict[str, Any],
    *,
    profile_id: str = "evolution-api2-custom",
    profile_name: str = "Evolution Legacy API2",
    notes: str = "Deprecated legacy API2 settings; Evolution now uses agent_api.",
    default_temperature: float = 0.2,
    default_max_tokens: int = 1400,
) -> dict[str, Any]:
    protocol = str(raw.get("protocol") or "openai").strip()
    if protocol not in LLM_MODEL_PROTOCOLS:
        protocol = "openai"
    return {
        "id": str(raw.get("id") or profile_id).strip() or profile_id,
        "name": str(raw.get("name") or profile_name).strip() or profile_name,
        "preset_key": str(raw.get("preset_key") or "custom-openai-compatible").strip() or "custom-openai-compatible",
        "protocol": protocol,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "temperature": _clamp_float(raw.get("temperature"), 0.0, 2.0, default_temperature),
        "max_tokens": _clamp_int(raw.get("max_tokens"), 128, 4096, default_max_tokens),
        "timeout_seconds": _clamp_int(raw.get("timeout_seconds"), 10, 900, 180),
        "extra_headers": raw.get("extra_headers") if isinstance(raw.get("extra_headers"), dict) else {},
        "extra_query": raw.get("extra_query") if isinstance(raw.get("extra_query"), dict) else {},
        "extra_body": raw.get("extra_body") if isinstance(raw.get("extra_body"), dict) else {},
        "notes": str(raw.get("notes") or notes),
        "use_legacy_chat_completions": bool(raw.get("use_legacy_chat_completions")),
    }


def _custom_profile_for_llm(raw: dict[str, Any]) -> dict[str, Any]:
    return _custom_profile_for_storage(raw)


def _build_agent_control_card_prompt(*, chapter_number: Optional[int], outline: str, raw_context: str) -> Any:
    system = (
        "你是 Evolution 智能体的上下文调度器，不写正文。"
        "你负责把世界线、路线图、角色卡、审查经验和本地语义记忆压缩成给正文作者使用的本章写作控制卡。"
    )
    user = f"""【本章】
第{chapter_number or '-'}章

【本章大纲】
{outline or '无'}

【原始 Evolution 上下文】
{raw_context}

请输出中文智能体控制卡，建议 900-1300 字符，必须包含：
1. 上一章结尾必须承接的状态，特别是人物已经所在的位置、携带物、伤势、情绪、未完成动作。
2. 本章硬约束与禁写事项：不要重复抵达已经抵达的地点，不要重置已经发生的状态。
3. 角色信息边界：谁知道什么，谁不能提前知道什么，谁只能根据现场线索推断。
4. 路线与空间提醒：人物移动必须有起点、过程、终点；相遇必须满足时间和地点交汇。
5. 人物卡与性格调色盘提醒：性格表现要从底色、主色调、点缀和衍生行为自然显现，不要只贴标签。
6. 本章剧情推进目标：只列可执行动作和必须完成的戏剧任务。
7. 篇幅控制：默认目标约 2500 字；若用户或原始上下文给出其他章节字数，以用户目标为准；超过 3000 字必须收束当前场景。
8. 禁用重复模板：不要使用“没有说话/没有回答/没有立刻回答/声音很轻/深吸一口气/沉默了几秒/盯着屏幕看了几秒/呼吸停了一拍/像是”等。
9. 替代表现方式：具体动作、环境反应、技术操作、心理判断、场面调度。
10. 文风适配提醒：根据原始上下文和本章题材调整措辞，不固定成某一种文风。

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


def _clean_control_card(content: str) -> str:
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

    thread = threading.Thread(target=runner, name="evolution-agent-control-card", daemon=True)
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
