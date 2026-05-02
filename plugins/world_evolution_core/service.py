"""PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from hashlib import sha256
from typing import Any, Optional, Union, Tuple

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
from .agent_knowledge import AgentKnowledgeBase
from .agent_orchestrator import AgentOrchestrator, apply_gene_patches, decision_to_context_blocks
from .agent_runtime import AgentRuntime, LLM_MODEL_PROTOCOLS, LLM_PROVIDER_MODES
from .canonical_characters import (
    calibrate_extracted_characters,
    canonicalize_names_in_records,
    load_canonical_characters,
)
from .continuity import build_chapter_summary, build_volume_summary
from .context_capsules import build_injection_record
from .context_patch import build_context_patch, render_patch_summary, tier_summary
from .diagnostics_service import DiagnosticsService
from .host_context import HOST_CONTEXT_SOURCES, HostContextReader
from .local_semantic_memory import LocalSemanticMemory
from .planning_adapter import (
    build_prehistory_worldline,
    build_planning_alignment,
    build_planning_lock,
    build_runtime_style_adapter,
    planning_payload_with_worldline_defaults,
    render_planning_adapter_context,
)
from .preset_converter import convert_st_preset
from .repositories import RECENT_CONTEXT_FACT_LIMIT, EvolutionWorldRepository
from .review_rules import (
    REPETITION_PHRASES,
    character_is_mentioned,
    normalize_evolution_issue_metadata,
    replacement_guidance_for_phrase,
    review_boundary_state,
    review_character_card_against_content,
    review_extraction_pollution,
    review_host_context_against_content,
    review_issue,
    review_route_conflicts,
    review_style_repetition,
)
from .story_graph import build_global_route_map, build_story_graph_chapter
from .structured_extractor import StructuredExtractorProvider, extract_structured_chapter_facts

PLUGIN_NAME = "world_evolution_core"
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
        self.agent_knowledge = AgentKnowledgeBase(self.repository)
        self.agent_runtime = AgentRuntime(
            settings_getter=lambda: self.get_settings(safe=False),
            agent_llm_service=agent_llm_service,
            llm_provider_factory=llm_provider_factory,
        )
        self.agent_orchestrator = AgentOrchestrator(run_agent=self._run_agent_decision)
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
        return await self.agent_runtime.fetch_models(payload or {})

    async def test_api2_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.deprecated_api2_response()

    async def test_agent_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.agent_runtime.test_connection(payload or {})

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
        worldline = build_prehistory_worldline(
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
        self.agent_knowledge.index_document(
            novel_id,
            source_type="prehistory_worldline",
            source_id="worldline",
            title=title or "Evolution 故事前史",
            text="\n".join(part for part in [title, premise, genre, world_preset, style_hint, json.dumps(worldline, ensure_ascii=False, default=str)] if part),
            metadata={"indexed_from": "after_novel_created"},
            source_refs=[{"source_type": "prehistory_worldline", "source_id": "worldline"}],
        )
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
        planning_payload = planning_payload_with_worldline_defaults(nested, (evidence or {}).get("worldline") or {})
        planning_lock = build_planning_lock(planning_payload, purpose=purpose)
        if not evidence and not planning_lock.get("has_lock"):
            return {"ok": True, "skipped": True, "reason": "no prehistory worldline or premise lock yet"}

        style_adapter = build_runtime_style_adapter(evidence.get("worldline") or {}, nested) if evidence else {}
        if evidence:
            evidence["style_adapter"] = style_adapter
        content = render_planning_adapter_context(planning_lock, evidence, style_adapter=style_adapter)
        planning_decision = self.agent_orchestrator.decide_planning(
            novel_id=novel_id,
            purpose=purpose,
            planning_payload=planning_payload,
            fallback_content=content,
            evidence_refs=[{"source_type": "prehistory_worldline", "source_id": "worldline"}] if evidence else [],
        )
        self._record_agent_decision(
            novel_id,
            phase="before_story_planning",
            chapter_number=None,
            decision=planning_decision,
            input_summary={"purpose": purpose, "premise_received": bool(planning_payload.get("premise"))},
        )
        if (planning_decision.get("agent_result") or {}).get("ok"):
            agent_lines = ["Evolution Agent 规划锁"]
            if planning_decision.get("t0_constraints"):
                agent_lines.append("【必须遵守】")
                agent_lines.extend(f"- {item}" for item in planning_decision.get("t0_constraints") or [])
            if planning_decision.get("t1_strategy"):
                agent_lines.append("【建议参考】")
                agent_lines.extend(f"- {item}" for item in planning_decision.get("t1_strategy") or [])
            content = "\n".join(agent_lines)
        planning_alignment = build_planning_alignment(planning_lock, evidence=evidence, rendered_chars=len(content))
        self.repository.save_planning_alignment(novel_id, planning_alignment)
        data = dict(evidence or {})
        data["planning_lock"] = planning_lock
        data["planning_alignment"] = planning_alignment
        data["agent_decision"] = planning_decision
        return {
            "ok": True,
            "data": data,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution 规划锁与故事前史",
                    "content": content,
                    "priority": 72,
                    "token_budget": 1200,
                    "metadata": {
                        "novel_id": novel_id,
                        "purpose": purpose,
                        "schema_version": (evidence.get("worldline") or {}).get("schema_version") if evidence else None,
                        "premise_received": planning_alignment.get("premise_received"),
                        "planning_lock_generated": planning_alignment.get("planning_lock_generated"),
                        "bible_empty_fallback": planning_alignment.get("bible_empty_fallback"),
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
        invalid_character_candidates = [
            str(name).strip()
            for name in snapshot.characters
            if str(name).strip() and not _valid_snapshot_character_name(str(name or ""))
        ]
        if invalid_character_candidates:
            self.repository.record_invalid_character_candidates(
                novel_id,
                invalid_character_candidates,
                chapter_number=chapter_number,
            )
        snapshot.characters = _filter_snapshot_characters(snapshot.characters)
        snapshot.locations = _filter_snapshot_locations(snapshot.locations)
        character_updates = [
            item
            for item in character_updates
            if _valid_snapshot_character_name(str(item.get("name") or ""))
        ]
        review_routing = self._route_extraction_review_candidates(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            character_updates=character_updates,
            world_events=[item.to_dict() for item in extraction.world_events],
        )
        character_updates = review_routing["approved_character_updates"]
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
        card_snapshot = copy.deepcopy(snapshot)
        pending_character_names = {
            str(candidate.get("payload", {}).get("name") or "").strip()
            for candidate in review_routing["pending_candidates"]
            if candidate.get("candidate_type") == "character_update"
        }
        if pending_character_names:
            card_snapshot.characters = [name for name in card_snapshot.characters if name not in pending_character_names]
        updated_cards = self.repository.upsert_character_cards(
            novel_id,
            card_snapshot,
            character_updates,
        )
        extraction_payload = extraction.to_dict()
        extraction_payload["snapshot"] = snapshot.to_dict()
        extraction_payload["character_updates"] = character_updates
        extraction_payload["review_candidates"] = review_routing["pending_candidates"]
        extraction_payload["canonical_character_count"] = calibration.canonical_count
        extraction_payload["ignored_character_candidates"] = calibration.ignored_candidates
        if canonical_characters:
            extraction_payload["world_events"] = canonicalize_names_in_records(
                review_routing["approved_world_events"],
                canonical_characters,
            )
        else:
            extraction_payload["world_events"] = review_routing["approved_world_events"]
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
        knowledge_index = self.agent_knowledge.index_chapter(
            novel_id,
            chapter_number,
            content,
            metadata={"content_hash": content_hash, "indexed_from": "after_commit"},
        )
        try:
            native_context_for_index = self.host_context_reader.read(
                novel_id,
                query=content[:1200],
                before_chapter=chapter_number + 1,
                limit=12,
            )
            native_index = self.agent_knowledge.index_host_context(novel_id, native_context_for_index)
        except Exception as exc:
            logger.warning("Evolution agent knowledge native indexing failed for %s ch%s: %s", novel_id, chapter_number, exc)
            native_index = {"documents_indexed": 0, "chunks_indexed": 0, "error": str(exc)}
        asset_index = self.agent_knowledge.index_agent_assets(
            novel_id,
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            reflections=self.repository.list_agent_reflections(novel_id),
            candidates=self.repository.list_agent_gene_candidates(novel_id),
        )
        observe_knowledge = self.agent_knowledge.search(
            novel_id,
            chapter_summary.get("short_summary") or content[:500],
            before_chapter=chapter_number + 1,
            limit=8,
        )
        observe_decision = self.agent_orchestrator.observe_after_commit(
            novel_id=novel_id,
            chapter_number=chapter_number,
            chapter_summary=chapter_summary,
            native_after_commit=native_after_commit,
            knowledge=observe_knowledge,
        )
        self._record_agent_decision(
            novel_id,
            phase="after_commit",
            chapter_number=chapter_number,
            decision=observe_decision,
            input_summary={
                "knowledge_index": knowledge_index,
                "native_index": native_index,
                "asset_index": asset_index,
                "content_hash": content_hash,
            },
        )
        extraction_payload["native_after_commit"] = native_after_commit
        extraction_payload["fallback_degraded"] = bool(native_after_commit.get("fallback_degraded"))
        extraction_payload["agent_knowledge_index"] = {
            "chapter": knowledge_index,
            "native": native_index,
            "assets": asset_index,
        }
        extraction_payload["agent_observation"] = observe_decision
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
        pending_review_count = len(self.repository.list_review_candidates(novel_id, status="pending", limit=0))
        patch["blocks"], gate_pruned = _prune_t0_context_blocks([block for block in (patch.get("blocks") or []) if isinstance(block, dict)])
        if gate_pruned:
            patch.setdefault("skipped_blocks", []).extend(gate_pruned)
        gate_decision = _decide_injection_gate(
            patch.get("blocks") or [],
            patch.get("skipped_blocks") or [],
            pending_review_count=pending_review_count,
        )
        summary = render_patch_summary(patch)
        if not summary or not gate_decision["should_inject"]:
            injection_record = build_injection_record(
                novel_id=novel_id,
                chapter_number=chapter_number,
                blocks=[],
                skipped_blocks=[
                    *(patch.get("skipped_blocks") or []),
                    {
                        "id": "evolution_injection_gate",
                        "kind": "injection_gate",
                        "title": "Evolution 注入门控",
                        "reason": gate_decision["skipped_reasons"][0] if gate_decision["skipped_reasons"] else "no_evolution_state",
                    },
                ],
                at=_now(),
            )
            injection_record["gate_decision"] = gate_decision
            self.repository.append_context_injection_record(novel_id, injection_record)
            return {
                "ok": True,
                "skipped": True,
                "reason": gate_decision["skipped_reasons"][0] if gate_decision["skipped_reasons"] else "no evolution state yet",
                "context_injection_record": injection_record,
            }

        patch_blocks = [block for block in (patch.get("blocks") or []) if isinstance(block, dict)]
        patch_tiers = tier_summary(patch_blocks)
        metadata: dict[str, Any] = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "patch_schema_version": patch.get("schema_version"),
            "api2_control_card_enabled": False,
            "agent_control_card_enabled": False,
            "source_block_count": len(patch_blocks),
            **patch_tiers,
        }
        context_blocks = _platform_context_blocks_from_patch(patch, metadata)
        settings = self.get_settings(safe=False)
        agent_settings = settings.get("agent_api") if isinstance(settings.get("agent_api"), dict) else {}
        if agent_settings.get("enabled"):
            knowledge = self.agent_knowledge.search(
                novel_id=novel_id,
                query=outline or summary[:800],
                before_chapter=chapter_number,
                limit=10,
            )
            context_decision = self.agent_orchestrator.decide_context(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                patch_summary=summary,
                knowledge=knowledge,
                tier_summary=patch_tiers,
            )
            self._record_agent_decision(
                novel_id,
                phase="before_context_build",
                chapter_number=chapter_number,
                decision=context_decision,
                input_summary={
                    "outline_chars": len(outline),
                    "patch_chars": len(summary),
                    "knowledge_item_count": knowledge.get("item_count"),
                    "knowledge_source_types": knowledge.get("source_types") or [],
                },
            )
            agent_blocks = decision_to_context_blocks(context_decision, metadata=metadata)
            if agent_blocks:
                content = "\n\n".join(str(block.get("content") or "") for block in agent_blocks)
                card_metadata = dict(metadata)
                card_metadata.update(
                    {
                        "agent_control_card_enabled": True,
                        "agent_provider_mode": agent_settings.get("provider_mode"),
                        "agent_raw_context_chars": len(summary),
                        "agent_control_card_chars": len(content),
                        "agent_compression_ratio": round(len(content) / max(len(summary), 1), 4),
                        "agent_orchestrated": True,
                        "agent_knowledge_item_count": knowledge.get("item_count"),
                        "agent_knowledge_source_types": knowledge.get("source_types") or [],
                    }
                )
                for block in agent_blocks:
                    block_metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
                    block["metadata"] = {**block_metadata, **card_metadata, "tier": block.get("tier")}
                context_blocks = agent_blocks
                self.repository.append_context_control_card_record(
                    novel_id,
                    {
                        "at": _now(),
                        "chapter_number": chapter_number,
                        "provider_mode": agent_settings.get("provider_mode"),
                        "source": "agent_api" if (context_decision.get("agent_result") or {}).get("ok") else "agent_api_degraded",
                        "raw_context_chars": len(summary),
                        "control_card_chars": len(content),
                        "compression_ratio": round(len(content) / max(len(summary), 1), 4),
                        "model": str(agent_settings.get("model") or ""),
                        "token_usage": (context_decision.get("agent_result") or {}).get("token_usage") or {},
                        "knowledge_item_count": knowledge.get("item_count"),
                    },
                )
            else:
                for block in context_blocks:
                    block_metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
                    block["metadata"] = {
                        **block_metadata,
                        "agent_control_card_enabled": True,
                        "agent_error": context_decision.get("degraded_reason") or "agent_decision_empty",
                    }
                metadata.update(
                    {
                        "agent_control_card_enabled": True,
                        "agent_error": context_decision.get("degraded_reason") or "agent_decision_empty",
                    }
                )

        injection_record = build_injection_record(
            novel_id=novel_id,
            chapter_number=chapter_number,
            blocks=patch.get("blocks") or [],
            skipped_blocks=patch.get("skipped_blocks") or [],
            at=_now(),
        )
        injection_record["gate_decision"] = gate_decision
        self.repository.append_context_injection_record(novel_id, injection_record)
        agent_selection = patch.get("agent_selection") if isinstance(patch.get("agent_selection"), dict) else {}
        if agent_selection and (agent_selection.get("selected_gene_ids") or agent_selection.get("selected_capsule_ids")):
            self.repository.append_agent_selection_record(novel_id, agent_selection)
            self.repository.append_agent_event(novel_id, build_selection_event(agent_selection))

        return {
            "ok": True,
            "context_patch": patch,
            "context_injection_record": injection_record,
            "context_blocks": context_blocks,
        }

    def list_review_candidates(self, novel_id: str, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        return {
            "items": self.repository.list_review_candidates(novel_id, status=status, limit=limit),
            "pending_count": len(self.repository.list_review_candidates(novel_id, status="pending", limit=0)),
        }

    def approve_review_candidate(self, novel_id: str, candidate_id: str) -> dict[str, Any]:
        candidate = self.repository.get_review_candidate(novel_id, candidate_id)
        if not candidate:
            return {"ok": False, "error": "review candidate not found"}
        if candidate.get("status") not in {"pending", "approved"}:
            return {"ok": False, "error": f"candidate is {candidate.get('status')}"}

        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        candidate_type = str(candidate.get("candidate_type") or "")
        applied = False
        if candidate_type == "character_update":
            applied = bool(self._apply_reviewed_character_update(novel_id, payload, _int_or_none(candidate.get("chapter_number"))))
        elif candidate_type == "world_event":
            applied = bool(self._apply_reviewed_world_event(novel_id, payload, candidate))
        elif candidate_type == "continuity_constraint":
            applied = bool(self._apply_reviewed_continuity_constraint(novel_id, payload, candidate))
        elif candidate_type == "agent_gene_candidate":
            applied = bool(self._approve_agent_gene_candidate(novel_id, payload))
        else:
            applied = True

        updated = {
            **candidate,
            "status": "applied" if applied else "approved",
            "reviewed_at": _now(),
        }
        self.repository.upsert_review_candidate(novel_id, updated)
        self.repository.append_event(
            novel_id,
            {"type": "review_candidate_approved", "candidate_id": candidate_id, "candidate_type": candidate_type, "applied": applied, "at": updated["reviewed_at"]},
        )
        return {"ok": True, "candidate": updated}

    def reject_review_candidate(self, novel_id: str, candidate_id: str, *, note: str = "") -> dict[str, Any]:
        candidate = self.repository.get_review_candidate(novel_id, candidate_id)
        if not candidate:
            return {"ok": False, "error": "review candidate not found"}
        updated = {
            **candidate,
            "status": "rejected",
            "review_note": note,
            "reviewed_at": _now(),
        }
        self.repository.upsert_review_candidate(novel_id, updated)
        self.repository.append_event(
            novel_id,
            {"type": "review_candidate_rejected", "candidate_id": candidate_id, "candidate_type": candidate.get("candidate_type"), "at": updated["reviewed_at"]},
        )
        return {"ok": True, "candidate": updated}

    def _route_extraction_review_candidates(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_hash: str,
        character_updates: list[dict[str, Any]],
        world_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        approved_character_updates: list[dict[str, Any]] = []
        approved_world_events: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        for update in character_updates:
            decision = _review_decision_for_candidate("character_update", update)
            if decision["status"] == "pending":
                candidate = self._upsert_pending_review_candidate(
                    novel_id=novel_id,
                    chapter_number=chapter_number,
                    candidate_type="character_update",
                    risk_level=decision["risk_level"],
                    payload=update,
                    evidence=[{"source_type": "chapter", "chapter_number": chapter_number, "content_hash": content_hash}],
                    reason=decision["reason"],
                )
                pending.append(candidate)
            else:
                approved_character_updates.append(update)

        for event in world_events:
            decision = _review_decision_for_candidate("world_event", event)
            if decision["status"] == "pending":
                candidate = self._upsert_pending_review_candidate(
                    novel_id=novel_id,
                    chapter_number=chapter_number,
                    candidate_type="world_event",
                    risk_level=decision["risk_level"],
                    payload=event,
                    evidence=[{"source_type": "chapter", "chapter_number": chapter_number, "content_hash": content_hash}],
                    reason=decision["reason"],
                )
                pending.append(candidate)
            else:
                approved_world_events.append(event)

        return {
            "approved_character_updates": approved_character_updates,
            "approved_world_events": approved_world_events,
            "pending_candidates": pending,
        }

    def _upsert_pending_review_candidate(
        self,
        *,
        novel_id: str,
        chapter_number: int | None,
        candidate_type: str,
        risk_level: str,
        payload: dict[str, Any],
        evidence: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        candidate_id = f"rev_{_hash_text(json.dumps([novel_id, chapter_number, candidate_type, payload], ensure_ascii=False, sort_keys=True))}"
        existing = self.repository.get_review_candidate(novel_id, candidate_id)
        if existing and existing.get("status") != "pending":
            return existing
        candidate = {
            "id": candidate_id,
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "candidate_type": candidate_type,
            "risk_level": risk_level,
            "status": "pending",
            "payload": payload,
            "evidence": evidence,
            "reason": reason,
            "created_at": (existing or {}).get("created_at") or _now(),
            "reviewed_at": None,
        }
        return self.repository.upsert_review_candidate(novel_id, candidate)

    def _apply_reviewed_character_update(self, novel_id: str, payload: dict[str, Any], chapter_number: int | None) -> dict[str, Any] | None:
        name = str(payload.get("name") or "").strip()
        if not name:
            return None
        current = self.repository.get_character_card(novel_id, name)
        if not current:
            current = {
                "name": name,
                "first_seen_chapter": chapter_number or 1,
                "last_seen_chapter": chapter_number or 1,
                "aliases": [],
                "recent_events": [],
                "status": "active",
            }
            self.repository.write_character_card(novel_id, current)
        updated = self.repository.merge_character_updates(novel_id, [payload], chapter_number=chapter_number)
        if updated:
            return updated[0]
        return self.repository.write_character_card(novel_id, {**current, **payload})

    def _apply_reviewed_world_event(self, novel_id: str, payload: dict[str, Any], candidate: dict[str, Any]) -> bool:
        chapter_number = _int_or_none(candidate.get("chapter_number")) or _int_or_none(payload.get("chapter_number"))
        summary = str(payload.get("summary") or "").strip()
        if not chapter_number or not summary:
            return False
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
        content_hash = next((str(item.get("content_hash") or "") for item in evidence if isinstance(item, dict) and item.get("content_hash")), "")
        seed = f"{novel_id}:{chapter_number}:reviewed:{summary}:{content_hash}"
        raw_characters = payload.get("characters") or payload.get("participants") or []
        raw_locations = payload.get("locations") or ([payload.get("location")] if payload.get("location") else [])
        participants = [str(item).strip() for item in raw_characters if str(item).strip()][:12]
        locations = [str(item).strip() for item in raw_locations if str(item).strip()][:5]
        event = {
            "event_id": str(payload.get("event_id") or "evt_" + _hash_text(seed)[:16]),
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "scene_order": _int_or_none(payload.get("scene_order")) or 999,
            "event_type": str(payload.get("event_type") or "scene").strip() or "scene",
            "summary": summary[:240],
            "participants": participants,
            "location": locations[0] if locations else "",
            "locations": locations,
            "effects": _event_effects_from_raw(payload),
            "knowledge_delta": _knowledge_delta_from_raw(payload, participants),
            "source": "review_approved",
            "content_hash": content_hash,
            "confidence": _candidate_confidence(payload),
            "at": _now(),
        }
        self.repository.save_timeline_events(novel_id, [event])
        return True

    def _apply_reviewed_continuity_constraint(self, novel_id: str, payload: dict[str, Any], candidate: dict[str, Any]) -> bool:
        chapter_number = _int_or_none(candidate.get("chapter_number")) or _int_or_none(payload.get("chapter_number"))
        rule = str(payload.get("rule") or payload.get("summary") or "").strip()
        if not chapter_number or not rule:
            return False
        subject = str(payload.get("subject") or payload.get("name") or "__world__").strip() or "__world__"
        constraint_type = str(payload.get("type") or payload.get("constraint_type") or "reviewed_constraint").strip() or "reviewed_constraint"
        seed = f"{novel_id}:{chapter_number}:{constraint_type}:{subject}:{rule}"
        constraint = {
            "constraint_id": str(payload.get("constraint_id") or "cc_" + _hash_text(seed)[:16]),
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "type": constraint_type,
            "subject": subject,
            "rule": rule[:260],
            "severity": str(payload.get("severity") or "warning"),
            "evidence_events": [str(item) for item in (payload.get("evidence_events") or []) if str(item).strip()][:8],
            "created_or_updated_chapter": chapter_number,
            "source": "review_approved",
        }
        self.repository.save_continuity_constraints(novel_id, [constraint])
        return True

    def _approve_agent_gene_candidate(self, novel_id: str, payload: dict[str, Any]) -> bool:
        gene_id = str(payload.get("gene_id") or payload.get("id") or "").strip()
        if not gene_id:
            return False
        genes = self.repository.list_agent_genes(novel_id)
        next_gene = {**payload, "id": gene_id, "status": "active", "updated_at": _now()}
        by_id = {str(gene.get("id") or ""): gene for gene in genes if gene.get("id")}
        by_id[gene_id] = next_gene
        self.repository.save_agent_genes(novel_id, list(by_id.values()))
        return True

    def _run_agent_decision(self, phase: str, prompt_text: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.agent_runtime.run_decision(phase, prompt_text, payload)

    def _record_agent_decision(
        self,
        novel_id: str,
        *,
        phase: str,
        chapter_number: Optional[int],
        decision: dict[str, Any],
        input_summary: Optional[dict[str, Any]] = None,
    ) -> None:
        agent_result = decision.get("agent_result") if isinstance(decision.get("agent_result"), dict) else {}
        record = {
            "type": "AgentDecisionRecord",
            "schema_version": 1,
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "hook_name": phase,
            "phase": phase,
            "intent": decision.get("intent"),
            "input_summary": input_summary or {},
            "output": {
                "t0_constraints": list(decision.get("t0_constraints") or [])[:8],
                "t1_strategy": list(decision.get("t1_strategy") or [])[:8],
                "issue_count": len(decision.get("issues") or []),
                "gene_patch_count": len(decision.get("gene_patches") or []),
                "degraded_reason": decision.get("degraded_reason") or "",
            },
            "actions": list(decision.get("actions") or [])[:12],
            "evidence_refs": list(decision.get("evidence_refs") or [])[:12],
            "token_usage": agent_result.get("token_usage") or {},
            "model": agent_result.get("model") or "",
            "status": "succeeded" if agent_result.get("ok") else "degraded",
            "error": agent_result.get("error") or decision.get("degraded_reason") or "",
            "at": _now(),
        }
        self.repository.append_agent_decision_record(novel_id, record)

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
        dry_run = bool(payload.get("dry_run"))
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        affected_scopes = _manual_rebuild_affected_scopes(bool(isinstance(chapters, list) and chapters))
        if dry_run:
            return {"ok": True, "dry_run": True, "data": {"novel_id": novel_id, "affected_scopes": affected_scopes}}
        if not isinstance(chapters, list) or not chapters:
            cards = self.repository.rebuild_character_cards_from_facts(novel_id)
            palette_updates = self._apply_canonical_character_profiles(novel_id)
            knowledge = self.rebuild_agent_knowledge(novel_id, record_decision=False)
            self.repository.append_workflow_run(
                novel_id,
                {
                    "run_id": f"rebuild-existing-{_hash_text(_now())}",
                    "hook_name": "manual_rebuild",
                    "trigger_type": "manual",
                    "status": "succeeded",
                    "started_at": _now(),
                    "finished_at": _now(),
                    "input": {"mode": "existing_facts", "affected_scopes": affected_scopes},
                    "output": {"characters_rebuilt": len(cards), "canonical_palette_updates": len(palette_updates), "agent_knowledge": knowledge.get("data", {})},
                },
            )
            return {
                "ok": True,
                "data": {
                    "novel_id": novel_id,
                    "mode": "existing_facts",
                    "characters_rebuilt": len(cards),
                    "canonical_palette_updates": len(palette_updates),
                    "agent_knowledge": knowledge.get("data", {}),
                    "affected_scopes": affected_scopes,
                },
            }

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
        palette_updates = self._apply_canonical_character_profiles(novel_id)
        knowledge = self.rebuild_agent_knowledge(novel_id, record_decision=False)
        return {
            "ok": True,
            "data": {
                "novel_id": novel_id,
                "rebuilt_chapters": rebuilt,
                "characters_rebuilt": len(cards),
                "canonical_palette_updates": len(palette_updates),
                "agent_knowledge": knowledge.get("data", {}),
                "affected_scopes": affected_scopes,
            },
        }

    async def rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        dry_run = bool(payload.get("dry_run"))
        if not novel_id or not chapter_number:
            return {"ok": False, "error": "missing novel_id/chapter_number"}
        affected_scopes = _rollback_affected_scopes(chapter_number)
        if dry_run:
            return {"ok": True, "dry_run": True, "data": {"novel_id": novel_id, "chapter_number": chapter_number, "affected_scopes": affected_scopes}}

        removed = self.repository.delete_fact_snapshot(novel_id, chapter_number)
        self.repository.delete_chapter_summary(novel_id, chapter_number)
        self.repository.delete_story_graph_chapter(novel_id, chapter_number)
        timeline_removed = self.repository.delete_timeline_events_for_chapter(novel_id, chapter_number)
        constraints_removed = self.repository.delete_continuity_constraints_for_chapter(novel_id, chapter_number)
        cards = self.repository.rebuild_character_cards_from_facts(novel_id)
        palette_updates = self._apply_canonical_character_profiles(novel_id)
        knowledge = self.rebuild_agent_knowledge(novel_id, record_decision=False)
        event = {
            "type": "chapter_rollback",
            "chapter_number": chapter_number,
            "removed_snapshot": removed,
            "timeline_events_removed": timeline_removed,
            "continuity_constraints_removed": constraints_removed,
            "characters_rebuilt": len(cards),
            "canonical_palette_updates": len(palette_updates),
            "agent_knowledge": knowledge.get("data", {}),
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
                "input": {"chapter_number": chapter_number, "affected_scopes": affected_scopes},
                "output": {"removed_snapshot": removed, "timeline_events_removed": timeline_removed, "continuity_constraints_removed": constraints_removed, "characters_rebuilt": len(cards), "agent_knowledge": knowledge.get("data", {})},
            },
        )
        return {"ok": True, "data": {"novel_id": novel_id, "chapter_number": chapter_number, "removed_snapshot": removed, "timeline_events_removed": timeline_removed, "continuity_constraints_removed": constraints_removed, "characters_rebuilt": len(cards), "agent_knowledge": knowledge.get("data", {}), "affected_scopes": affected_scopes}}

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

    def rebuild_agent_knowledge(self, novel_id: str, *, record_decision: bool = True) -> dict[str, Any]:
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        cleared = self.repository.clear_agent_knowledge(novel_id)
        documents = 0
        chunks = 0
        host_chapter_index = self.agent_knowledge.index_host_chapters(novel_id, self.host_database)
        documents += int(host_chapter_index.get("documents_indexed") or 0)
        chunks += int(host_chapter_index.get("chunks_indexed") or 0)
        for summary in self.repository.list_chapter_summaries(novel_id, limit=0):
            chapter_number = _int_or_none(summary.get("chapter_number"))
            text = json.dumps(summary, ensure_ascii=False, default=str)
            result = self.agent_knowledge.index_document(
                novel_id,
                source_type="chapter_summary",
                source_id=f"chapter_summary_{chapter_number or _hash_text(text)[:8]}",
                title=f"第{chapter_number or '?'}章摘要",
                text=text,
                chapter_number=chapter_number,
                metadata={"indexed_from": "knowledge_rebuild"},
                source_refs=[{"source_type": "chapter_summary", "chapter_number": chapter_number}],
            )
            documents += int(result.get("document_indexed") or 0)
            chunks += int(result.get("chunk_count") or 0)
        for fact in self.repository.list_fact_snapshots(novel_id):
            chapter_number = _int_or_none(fact.get("chapter_number"))
            text = json.dumps(fact, ensure_ascii=False, default=str)
            result = self.agent_knowledge.index_document(
                novel_id,
                source_type="evolution_fact_snapshot",
                source_id=f"fact_{chapter_number or _hash_text(text)[:8]}",
                title=f"第{chapter_number or '?'}章 Evolution 事实快照",
                text=text,
                chapter_number=chapter_number,
                metadata={"indexed_from": "knowledge_rebuild"},
                source_refs=[{"source_type": "evolution_fact_snapshot", "chapter_number": chapter_number}],
            )
            documents += int(result.get("document_indexed") or 0)
            chunks += int(result.get("chunk_count") or 0)
        try:
            host_context = self.host_context_reader.read(novel_id, query="", before_chapter=None, limit=20)
            native_index = self.agent_knowledge.index_host_context(novel_id, host_context)
            documents += int(native_index.get("documents_indexed") or 0)
            chunks += int(native_index.get("chunks_indexed") or 0)
        except Exception as exc:
            native_index = {"documents_indexed": 0, "chunks_indexed": 0, "error": str(exc)}
        asset_index = self.agent_knowledge.index_agent_assets(
            novel_id,
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            reflections=self.repository.list_agent_reflections(novel_id),
            candidates=self.repository.list_agent_gene_candidates(novel_id),
        )
        documents += int(asset_index.get("documents_indexed") or 0)
        chunks += int(asset_index.get("chunks_indexed") or 0)
        coverage = self.agent_knowledge.coverage(novel_id)
        if record_decision:
            self.repository.append_agent_decision_record(
                novel_id,
                {
                    "type": "AgentDecisionRecord",
                    "schema_version": 1,
                    "novel_id": novel_id,
                    "phase": "knowledge_rebuild",
                    "hook_name": "agent_knowledge_rebuild",
                    "intent": "rebuild_knowledge",
                    "input_summary": {},
                    "output": {"documents_indexed": documents, "chunks_indexed": chunks, "coverage": coverage},
                    "actions": [{"type": "knowledge_rebuild"}],
                    "evidence_refs": [],
                    "status": "succeeded",
                    "at": _now(),
                },
            )
        return {
            "ok": True,
            "data": {
                "documents_indexed": documents,
                "chunks_indexed": chunks,
                "cleared": cleared,
                "host_chapters": host_chapter_index,
                "native_index": native_index,
                "asset_index": asset_index,
                "coverage": coverage,
            },
        }

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

    def _apply_canonical_character_profiles(self, novel_id: str, chapter_number: Optional[int] = None) -> list[dict[str, Any]]:
        canonical_characters = load_canonical_characters(self.host_database, novel_id)
        if not canonical_characters:
            return []
        return self.repository.merge_character_updates(
            novel_id,
            [character.to_update() for character in canonical_characters],
            chapter_number=chapter_number,
        )

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

        mentioned_cards = [card for card in cards if character_is_mentioned(card, content)]
        for card in mentioned_cards:
            issues.extend(
                _attach_issue_evidence(
                    review_character_card_against_content(card, content, chapter_number),
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
                        review_issue(
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

        route_issues = review_route_conflicts(evidence.get("route_conflicts") or [], chapter_number)
        if route_issues:
            issues.extend(_attach_issue_evidence(route_issues, evidence, subject=""))
        all_cards = (
            self.repository.list_all_character_cards(novel_id).get("items", [])
            if hasattr(self.repository, "list_all_character_cards")
            else self.repository.list_character_cards(novel_id).get("items", [])
        )
        pollution_issues = review_extraction_pollution(
            all_cards,
            facts,
            chapter_number,
        )
        if pollution_issues:
            issues.extend(pollution_issues)
        boundary_issues = review_boundary_state(
            self.repository.list_chapter_summaries(novel_id, before_chapter=chapter_number, limit=1),
            content,
            chapter_number,
        )
        if boundary_issues:
            issues.extend(boundary_issues)
        repetition_issues = review_style_repetition(content, chapter_number)
        if repetition_issues:
            issues.extend(repetition_issues)
        host_context = self.host_context_reader.read(
            novel_id,
            query=content[:1200],
            before_chapter=chapter_number,
            limit=6,
        )
        self.repository.save_host_context_summary(novel_id, self.host_context_reader.summary(host_context))
        host_issues = review_host_context_against_content(host_context, content, chapter_number)
        if host_issues:
            issues.extend(host_issues)

        if issues:
            suggestions.append("Evolution 建议优先补足角色得知信息、能力越界或误信被修正的过渡，而不是直接删除剧情推进。")
        issues = [normalize_evolution_issue_metadata(item) for item in issues if isinstance(item, dict)]
        knowledge = self.agent_knowledge.search(
            novel_id,
            content[:1000],
            before_chapter=chapter_number,
            limit=10,
        )
        review_decision = self.agent_orchestrator.decide_review(
            novel_id=novel_id,
            chapter_number=chapter_number,
            deterministic_issues=issues,
            evidence=evidence,
            knowledge=knowledge,
        )
        self._record_agent_decision(
            novel_id,
            phase="review_chapter",
            chapter_number=chapter_number,
            decision=review_decision,
            input_summary={"deterministic_issue_count": len(issues), "knowledge_item_count": knowledge.get("item_count")},
        )
        agent_issues = [
            normalize_evolution_issue_metadata({**item, "source": "agent_orchestrator"})
            for item in (review_decision.get("issues") or [])
            if isinstance(item, dict)
        ]
        if agent_issues:
            issues.extend(agent_issues)
            suggestions.append("Evolution Agent 已基于全文知识库补充审查问题。")

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
                "agent_decision": review_decision,
                "agent_knowledge": {"item_count": knowledge.get("item_count"), "source_types": knowledge.get("source_types") or []},
            },
        }

    def after_chapter_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        review_result = (payload.get("payload") or {}).get("review_result") or {}
        if not novel_id or not chapter_number:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number"}
        issues = review_result.get("issues") or []
        issue_items = [normalize_evolution_issue_metadata(item) for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
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
            agent_api_record = self.agent_runtime.build_reflection(
                novel_id=novel_id,
                chapter_number=chapter_number,
                capsules=solidified,
                issues=issue_items,
                settings=agent_api_settings,
            )
            reflection_record = agent_api_record.get("reflection") if isinstance(agent_api_record, dict) else None
            self.repository.append_agent_event(novel_id, _agent_reflection_event(novel_id, chapter_number, agent_api_record, agent_api_settings))
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
        reflection_query = "\n".join(str(item.get("description") or item.get("issue_type") or "") for item in issue_items[:8])
        reflection_knowledge = self.agent_knowledge.search(
            novel_id,
            reflection_query or f"第{chapter_number}章审查反思",
            before_chapter=chapter_number + 1,
            limit=10,
        )
        orchestration_decision = self.agent_orchestrator.decide_reflection(
            novel_id=novel_id,
            chapter_number=chapter_number,
            issues=issue_items,
            capsules=solidified,
            active_genes=self.repository.list_agent_genes(novel_id),
            knowledge=reflection_knowledge,
        )
        self._record_agent_decision(
            novel_id,
            phase="after_chapter_review",
            chapter_number=chapter_number,
            decision=orchestration_decision,
            input_summary={
                "issue_count": len(issue_items),
                "capsule_count": len(solidified),
                "knowledge_item_count": reflection_knowledge.get("item_count"),
            },
        )
        gene_versions: list[dict[str, Any]] = []
        if orchestration_decision.get("gene_patches"):
            updated_genes, gene_versions = apply_gene_patches(
                novel_id=novel_id,
                chapter_number=chapter_number,
                genes=self.repository.list_agent_genes(novel_id),
                patches=orchestration_decision.get("gene_patches") or [],
                at=_now(),
            )
            if gene_versions:
                self.repository.save_agent_genes(novel_id, updated_genes)
                for version in gene_versions:
                    self.repository.append_gene_version(novel_id, version)
                    self.repository.append_agent_event(
                        novel_id,
                        {
                            "type": "EvolutionEvent",
                            "schema_version": 1,
                            "id": f"evt_gene_version_{_hash_text(novel_id + str(chapter_number) + str(version.get('gene_id')) + str(version.get('version')))}",
                            "intent": "evolve_gene",
                            "hook_name": "after_chapter_review",
                            "novel_id": novel_id,
                            "chapter_number": chapter_number,
                            "signals": ["agent_auto_evolution", "gene_patch"],
                            "genes_used": [version.get("gene_id")],
                            "capsule_id": None,
                            "outcome": {"status": "success", "gene_id": version.get("gene_id"), "version": version.get("version")},
                            "meta": {"at": _now(), "mode": "immediate"},
                        },
                    )
                self.agent_knowledge.index_agent_assets(
                    novel_id,
                    genes=updated_genes,
                    capsules=self.repository.list_agent_capsules(novel_id),
                    reflections=self.repository.list_agent_reflections(novel_id),
                    candidates=self.repository.list_agent_gene_candidates(novel_id),
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
            self._upsert_pending_review_candidate(
                novel_id=novel_id,
                chapter_number=chapter_number,
                candidate_type="agent_gene_candidate",
                risk_level="medium",
                payload=candidate,
                evidence=[{"source_type": "after_chapter_review", "chapter_number": chapter_number}],
                reason="agent_gene_candidate_pending_review",
            )
        for event in candidate_events:
            self.repository.append_agent_event(novel_id, event)
        self.repository.save_agent_memory_index(novel_id, memory_index)
        self.agent_knowledge.index_agent_assets(
            novel_id,
            genes=self.repository.list_agent_genes(novel_id),
            capsules=self.repository.list_agent_capsules(novel_id),
            reflections=self.repository.list_agent_reflections(novel_id),
            candidates=self.repository.list_agent_gene_candidates(novel_id),
        )
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
                "agent_orchestration": orchestration_decision,
                "gene_versions": [version.get("gene_id") for version in gene_versions],
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
                "agent_orchestration": orchestration_decision,
                "gene_versions": gene_versions,
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


def _agent_reflection_event(novel_id: str, chapter_number: int, record: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any]:
    record = record if isinstance(record, dict) else {}
    at = str(record.get("at") or _now())
    ok = bool(record.get("ok"))
    return {
        "type": "EvolutionEvent",
        "schema_version": 1,
        "id": f"evt_agent_api{'_failed' if not ok else ''}_{_hash_text(novel_id + str(chapter_number) + at)}",
        "intent": "reflect",
        "hook_name": "after_chapter_review",
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "signals": ["agent_api", "review_reflection"],
        "genes_used": [],
        "capsule_id": None,
        "outcome": (
            {"status": "success", "capsule_count": len(record.get("capsule_ids") or [])}
            if ok
            else {"status": "failed", "error": str(record.get("error") or "")}
        ),
        "meta": {"at": at, "model": str(settings.get("model") or "")} if ok else {"at": at},
    }


def _matching_agent_selection(records: list[dict[str, Any]], chapter_number: int) -> dict[str, Any]:
    for record in reversed(records or []):
        if _int_or_none(record.get("chapter_number")) == chapter_number:
            return record
    return {}



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
    "水箱",
    "水箱下方",
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
    "水箱",
    "下方",
)
_NON_CHARACTER_ENTITY_SUFFIXES = ("之谜", "真相", "记录", "线索", "计划", "任务", "报告", "下方", "区域")
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
    min_count = 2
    for phrase in REPETITION_PHRASES:
        count = text.count(phrase)
        if count >= min_count:
            phrases.append(
                {
                    "phrase": phrase,
                    "count": count,
                    "chapters": [chapter_number],
                    "replacement_guidance": replacement_guidance_for_phrase(phrase),
                    "source": "recent_repetition_scan",
                    "strategy_tier": "intended_t1",
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


def _build_timeline_events(snapshot, extraction: dict[str, Any], content_hash: str, at: str) -> list[dict[str, Any]]:
    raw_events = extraction.get("world_events") if "world_events" in extraction else []
    if raw_events is None:
        raw_events = []
    if "world_events" not in extraction and not raw_events:
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
    seed = f"{novel_id}:{chapter_number}:{constraint_type}:{subject}:{rule}"
    return {
        "constraint_id": "cc_" + _hash_text(seed)[:16],
        "novel_id": novel_id,
        "chapter_number": chapter_number,
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
        "empty_sources": [],
        "field_missing_sources": [],
        "source_status": {},
        "counts": counts,
        "plotpilot_context_usage": {
            "source": "plotpilot_native_context_adapter",
            "mode": "strategy_only",
            "hit_counts_by_tier": {},
            "degraded_sources": [reason],
            "empty_sources": [],
            "field_missing_sources": [],
            "long_context_duplicated": False,
        },
        **{key: [] for key in HOST_CONTEXT_SOURCES},
    }


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


def _platform_context_blocks_from_patch(patch: dict[str, Any], shared_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    base_metadata = {key: value for key, value in shared_metadata.items() if key != "block_tiers"}
    for block in patch.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        content = str(block.get("content") or "").strip()
        if not content:
            continue
        block_metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        tier = str(block.get("tier") or block_metadata.get("tier") or "").strip()
        metadata = {
            **base_metadata,
            **block_metadata,
            "source_block_id": block.get("id"),
            "source_block_kind": block.get("kind"),
        }
        if tier:
            metadata["tier"] = tier
        blocks.append(
            {
                "plugin_name": PLUGIN_NAME,
                "id": block.get("id"),
                "kind": block.get("kind"),
                "tier": tier or None,
                "title": block.get("title") or block.get("id") or "Evolution 写作约束",
                "content": content,
                "priority": int(block.get("priority") or 0),
                "token_budget": int(block.get("token_budget") or 0),
                "metadata": metadata,
            }
        )
    return blocks


def _prune_t0_context_blocks(blocks: list[dict[str, Any]], *, max_t0_chars: int = 2800) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    used = 0
    for block in blocks:
        if _block_tier_for_gate(block) != "intended_t0":
            kept.append(block)
            continue
        content = str(block.get("content") or "")
        if used >= max_t0_chars:
            skipped.append(_gate_skip_record(block, "t0_budget_pruned"))
            continue
        remaining = max_t0_chars - used
        next_block = dict(block)
        if len(content) > remaining:
            next_block["content"] = content[:remaining].rstrip() + "..."
            skipped.append(_gate_skip_record(block, "t0_content_truncated"))
        used += len(str(next_block.get("content") or ""))
        kept.append(next_block)
    return kept, skipped


def _decide_injection_gate(
    blocks: list[dict[str, Any]],
    skipped_blocks: list[dict[str, Any]],
    *,
    pending_review_count: int,
) -> dict[str, Any]:
    active_blocks = [block for block in blocks if str(block.get("content") or "").strip()]
    t0_blocks = [
        block for block in active_blocks
        if _block_tier_for_gate(block) == "intended_t0"
        or str(block.get("kind") or "") in {"chapter_state_bridge", "story_graph_route_constraints", "continuity_risk", "hard_constraint"}
    ]
    t1_blocks = [block for block in active_blocks if block not in t0_blocks]
    reasons: list[str] = []
    skipped_reasons: list[str] = []
    if t0_blocks:
        reasons.append("t0_constraints_available")
    if any(str(block.get("kind") or "") == "story_graph_route_constraints" for block in t0_blocks):
        reasons.append("route_constraints_available")
    if any(str(block.get("kind") or "") == "continuity_risk" for block in t0_blocks):
        reasons.append("continuity_risk_available")
    if any(str(block.get("kind") or "") == "chapter_state_bridge" for block in t0_blocks):
        reasons.append("chapter_state_bridge_available")
    if not reasons and t1_blocks:
        reasons.append("t1_support_available")
    if not active_blocks:
        skipped_reasons.append("no_active_context_blocks")
    if not reasons and skipped_blocks:
        skipped_reasons.append("only_duplicate_or_pruned_blocks")
    should_inject = bool(reasons) and bool(active_blocks)
    return {
        "should_inject": should_inject,
        "reasons": reasons,
        "skipped_reasons": skipped_reasons,
        "state_item_count": len(active_blocks),
        "pending_review_count": pending_review_count,
        "t0_chars": sum(len(str(block.get("content") or "")) for block in t0_blocks),
        "t1_chars": sum(len(str(block.get("content") or "")) for block in t1_blocks),
        "skipped_block_count": len(skipped_blocks),
        "source": "evolution_gate",
    }


def _block_tier_for_gate(block: dict[str, Any]) -> str:
    tier = str(block.get("tier") or "").strip()
    if tier:
        return tier
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return str(metadata.get("tier") or metadata.get("intended_tier") or "").strip()


def _gate_skip_record(block: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": block.get("id"),
        "kind": block.get("kind"),
        "tier": _block_tier_for_gate(block),
        "title": block.get("title"),
        "priority": block.get("priority"),
        "token_budget": block.get("token_budget"),
        "content_chars": len(str(block.get("content") or "")),
        "reason": reason,
    }


def _review_decision_for_candidate(candidate_type: str, payload: dict[str, Any]) -> dict[str, str]:
    confidence = _candidate_confidence(payload)
    high_risk_reasons = _candidate_high_risk_reasons(candidate_type, payload)
    explicit_confidence = bool(payload.get("confidence_explicit"))
    explicit_review = bool(payload.get("review_required") or payload.get("risk_level"))
    if explicit_confidence and confidence < 0.78:
        return {
            "status": "pending",
            "risk_level": "medium" if not high_risk_reasons else "high",
            "reason": f"explicit_confidence_below_threshold:{confidence:.2f}",
        }
    if (explicit_confidence or explicit_review) and high_risk_reasons:
        return {
            "status": "pending",
            "risk_level": "high",
            "reason": "high_risk_fields:" + ",".join(high_risk_reasons[:4]),
        }
    return {"status": "approved", "risk_level": "low", "reason": "auto_apply"}


def _candidate_confidence(payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("confidence", 0.8))
    except (TypeError, ValueError):
        return 0.0


def _candidate_high_risk_reasons(candidate_type: str, payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("unknowns", "misbeliefs", "capability_limits"):
        if payload.get(key):
            reasons.append(key)
    if candidate_type == "character_update":
        if payload.get("aliases"):
            reasons.append("identity_alias")
        status = str(payload.get("status") or "active")
        if status and status != "active":
            reasons.append("status_change")
        palette = payload.get("personality_palette") if isinstance(payload.get("personality_palette"), dict) else {}
        if palette.get("relationship_tones"):
            reasons.append("relationship_state")
    if candidate_type == "world_event":
        event_type = str(payload.get("event_type") or "")
        if event_type in {"world_rule", "system_rule", "relationship", "route_conflict"}:
            reasons.append(event_type)
    return reasons


def _manual_rebuild_affected_scopes(has_chapter_payloads: bool) -> dict[str, list[str]]:
    body = [
        "facts",
        "chapter_summaries",
        "volume_summaries",
        "character_cards",
        "timeline_events",
        "continuity_constraints",
        "story_graph",
    ]
    if not has_chapter_payloads:
        body = ["character_cards"]
    return {
        "body_state": body,
        "index_state": ["agent_knowledge_documents", "agent_knowledge_chunks"],
        "audit_state_preserved": ["workflow_runs", "events", "review_candidates", "diagnostics_snapshots", "context_injection_records"],
    }


def _rollback_affected_scopes(chapter_number: int) -> dict[str, list[str]]:
    return {
        "body_state": [
            f"facts/chapter_{chapter_number}",
            f"chapter_summaries/chapter_{chapter_number}",
            f"story_graph/chapter_{chapter_number}",
            f"timeline_events/chapter_{chapter_number}",
            f"continuity_constraints/chapter_{chapter_number}",
            "character_cards_rebuild",
        ],
        "index_state": ["agent_knowledge_documents", "agent_knowledge_chunks"],
        "audit_state_preserved": ["workflow_runs", "events", "review_candidates", "diagnostics_snapshots", "context_injection_records"],
    }


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
