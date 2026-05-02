"""Read-only Agent status aggregation helpers."""
from __future__ import annotations

from typing import Any, Optional


def agent_api_usage_from_control_cards(records: list[dict[str, Any]]) -> dict[str, Any]:
    calls = []
    totals = {
        "call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }
    for record in records:
        if not isinstance(record, dict) or record.get("source") != "agent_api":
            continue
        usage = record.get("token_usage") if isinstance(record.get("token_usage"), dict) else {}
        call = {
            "chapter_number": _int_or_none(record.get("chapter_number")),
            "provider_mode": str(record.get("provider_mode") or ""),
            "model": str(record.get("model") or ""),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "total_cost_usd": float(usage.get("total_cost_usd") or 0.0),
            "control_card_chars": int(record.get("control_card_chars") or 0),
            "created_at": str(record.get("created_at") or ""),
        }
        calls.append(call)
        totals["call_count"] += 1
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "total_tokens"):
            totals[key] += int(call.get(key) or 0)
        totals["total_cost_usd"] += float(call.get("total_cost_usd") or 0.0)
    totals["total_cost_usd"] = round(totals["total_cost_usd"], 6)
    return {"aggregate": totals, "calls": calls[-20:]}


def normalize_planning_alignment(alignment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(alignment, dict) or not alignment:
        return {
            "source": "before_story_planning",
            "premise_received": False,
            "planning_lock_generated": False,
            "bible_empty_fallback": False,
            "prehistory_available": False,
        }
    return {
        "source": str(alignment.get("source") or "before_story_planning"),
        "purpose": str(alignment.get("purpose") or "story_planning"),
        "premise_received": bool(alignment.get("premise_received")),
        "planning_lock_generated": bool(alignment.get("planning_lock_generated")),
        "bible_empty_fallback": bool(alignment.get("bible_empty_fallback")),
        "prehistory_available": bool(alignment.get("prehistory_available")),
        "rendered_chars": int(alignment.get("rendered_chars") or 0),
        "title": str(alignment.get("title") or "")[:200],
        "genre": str(alignment.get("genre") or "")[:120],
        "world_preset": str(alignment.get("world_preset") or "")[:200],
        "target_chapters": int(alignment.get("target_chapters") or 0),
        "bible_counts": dict(alignment.get("bible_counts") or {}),
    }


def native_context_alignment(summary: dict[str, Any]) -> dict[str, Any]:
    summary = summary if isinstance(summary, dict) else {}
    counts = dict(summary.get("counts") or {})
    usage = dict(summary.get("plotpilot_context_usage") or {})
    active_sources = list(summary.get("active_sources") or [])
    strategy_only = (usage.get("mode") or "strategy_only") == "strategy_only" and not bool(usage.get("long_context_duplicated"))
    source_roles = {
        "story_knowledge": "chapter_after_sync",
        "triples": "graph_fact_source",
        "knowledge": "weak_recall_support",
    }
    overlapping_sources = [source for source in ("knowledge", "story_knowledge", "triples") if int(counts.get(source) or 0)]
    return {
        "source": "plotpilot_native_context_adapter",
        "strategy_only": strategy_only,
        "active_source_counts": {source: int(counts.get(source) or 0) for source in counts},
        "active_source_count": len(active_sources),
        "duplicated_source_count": 0 if strategy_only else max(0, len(overlapping_sources) - 1),
        "source_roles": source_roles,
        "degraded_sources": list(summary.get("degraded_sources") or []),
        "empty_sources": list(summary.get("empty_sources") or []),
        "field_missing_sources": list(summary.get("field_missing_sources") or []),
    }


def context_injection_tier_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    latest = records[-1] if records else {}
    blocks = []
    if isinstance(latest, dict):
        for candidate in (latest.get("blocks"), latest.get("selected"), latest.get("context_blocks")):
            blocks = [item for item in (candidate or []) if isinstance(item, dict)]
            if blocks:
                break
        if not blocks and isinstance(latest.get("context_patch"), dict):
            blocks = [item for item in (latest.get("context_patch", {}).get("blocks") or []) if isinstance(item, dict)]
    counts = {"intended_t0": 0, "intended_t1": 0, "unknown": 0}
    chars = {"intended_t0": 0, "intended_t1": 0, "unknown": 0}
    for block in blocks:
        tier = _block_tier(block)
        bucket = tier if tier in {"intended_t0", "intended_t1"} else "unknown"
        counts[bucket] += 1
        chars[bucket] += _block_chars(block)
    return {
        "has_context_injection": bool(records),
        "block_count": len(blocks),
        "t0_block_count": counts["intended_t0"],
        "t1_block_count": counts["intended_t1"],
        "tier_unknown_count": counts["unknown"],
        "t0_chars": chars["intended_t0"],
        "t1_chars": chars["intended_t1"],
        "tier_unknown_chars": chars["unknown"],
        "latest_chapter": latest.get("chapter_number") if isinstance(latest, dict) else None,
    }


def agent_orchestration_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    phase_counts: dict[str, int] = {}
    degraded = 0
    latest = records[-1] if records else {}
    for record in records:
        phase = str(record.get("phase") or record.get("hook_name") or "unknown")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if str(record.get("status") or "") != "succeeded":
            degraded += 1
    return {
        "architecture_mode": "agent_first_hybrid",
        "decision_boundary": "agent_orchestrator",
        "deterministic_role": "tools_storage_validation_fallback",
        "enabled": True,
        "decision_count": len(records),
        "phase_counts": phase_counts,
        "degraded_decision_count": degraded,
        "latest_phase": latest.get("phase") if isinstance(latest, dict) else None,
        "latest_status": latest.get("status") if isinstance(latest, dict) else None,
    }


def knowledge_base_summary(documents: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    doc_counts: dict[str, int] = {}
    chunk_counts: dict[str, int] = {}
    latest = ""
    for item in documents:
        source = str(item.get("source_type") or "unknown")
        doc_counts[source] = doc_counts.get(source, 0) + 1
        latest = max(latest, str(item.get("updated_at") or ""))
    for item in chunks:
        source = str(item.get("source_type") or "unknown")
        chunk_counts[source] = chunk_counts.get(source, 0) + 1
        latest = max(latest, str(item.get("updated_at") or ""))
    return {
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "document_counts_by_source": dict(sorted(doc_counts.items())),
        "chunk_counts_by_source": dict(sorted(chunk_counts.items())),
        "latest_updated_at": latest,
        "vector_status": "keyword_indexed",
    }


def auto_evolution_summary(versions: list[dict[str, Any]]) -> dict[str, Any]:
    gene_ids = [str(item.get("gene_id") or "") for item in versions if item.get("gene_id")]
    return {
        "mode": "immediate",
        "gene_version_count": len(versions),
        "mutated_gene_count": len(set(gene_ids)),
        "latest_gene_id": gene_ids[-1] if gene_ids else "",
        "latest_version": versions[-1].get("version") if versions else None,
    }


def active_gene_versions(genes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "gene_id": gene.get("id"),
            "version": int(gene.get("version") or 0),
            "title": gene.get("title"),
            "created_by_agent": bool(gene.get("created_by_agent")),
            "updated_at": gene.get("updated_at"),
        }
        for gene in genes[:20]
        if isinstance(gene, dict)
    ]


def normalize_host_context_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict) or not summary:
        return {}
    normalized = dict(summary)
    counts = dict(normalized.get("counts") or {})
    degraded_sources = list(normalized.get("degraded_sources") or [])
    empty_sources = list(normalized.get("empty_sources") or [])
    field_missing_sources = list(normalized.get("field_missing_sources") or [])
    source_status = dict(normalized.get("source_status") or {})
    active_sources = list(normalized.get("active_sources") or [])
    if not active_sources and counts:
        active_sources = [
            source
            for source in (
                "bible",
                "world",
                "knowledge",
                "story_knowledge",
                "storyline",
                "timeline",
                "chronicle",
                "foreshadow",
                "dialogue",
                "triples",
                "memory_engine",
            )
            if int(counts.get(source) or 0) > 0
        ]
        active_sources.extend(source for source, count in counts.items() if source not in active_sources and int(count or 0) > 0)

    usage = dict(normalized.get("plotpilot_context_usage") or {})
    observability_normalized = False
    if not usage:
        usage = _default_plotpilot_context_usage(degraded_sources, empty_sources, field_missing_sources)
        observability_normalized = True
    else:
        for key, value in _default_plotpilot_context_usage(degraded_sources, empty_sources, field_missing_sources).items():
            if key not in usage:
                usage[key] = value
                observability_normalized = True

    for key, value in {
        "active_sources": active_sources,
        "degraded_sources": degraded_sources,
        "empty_sources": empty_sources,
        "field_missing_sources": field_missing_sources,
        "source_status": source_status,
        "counts": counts,
        "plotpilot_context_usage": usage,
    }.items():
        if key not in normalized:
            observability_normalized = True
        normalized[key] = value
    if observability_normalized:
        normalized["observability_normalized"] = True
    return normalized


def _default_plotpilot_context_usage(
    degraded_sources: list[Any],
    empty_sources: list[Any],
    field_missing_sources: list[Any],
) -> dict[str, Any]:
    return {
        "source": "plotpilot_native_context_adapter",
        "mode": "strategy_only",
        "hit_counts_by_tier": {},
        "source_roles": {
            "story_knowledge": "chapter_after_sync",
            "triples": "graph_fact_source",
            "knowledge": "weak_recall_support",
        },
        "degraded_sources": degraded_sources,
        "empty_sources": empty_sources,
        "field_missing_sources": field_missing_sources,
        "long_context_duplicated": False,
    }


def _block_tier(block: dict[str, Any]) -> str:
    tier = str(block.get("tier") or "").strip()
    if tier:
        return tier
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return str(metadata.get("tier") or metadata.get("intended_tier") or "").strip()


def _block_chars(block: dict[str, Any]) -> int:
    if block.get("content_chars") is not None:
        try:
            return int(block.get("content_chars") or 0)
        except (TypeError, ValueError):
            return 0
    return len(str(block.get("content") or ""))


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
