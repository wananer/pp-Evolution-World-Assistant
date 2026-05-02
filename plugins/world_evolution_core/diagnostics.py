"""Read-only risk diagnostics for Evolution World."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from typing import Any

from plugins.platform.hook_dispatcher import list_hooks

from .personality_palette import palette_missing_fields, personality_palette_status

PLUGIN_NAME = "world_evolution_core"
DIAGNOSTICS_SCHEMA_VERSION = 1
TIER_T0 = "intended_t0"
TIER_T1 = "intended_t1"
EXPECTED_HOOKS = {
    "after_novel_created",
    "before_story_planning",
    "before_context_build",
    "after_commit",
    "manual_rebuild",
    "rollback",
    "before_chapter_review",
    "after_chapter_review",
    "review_chapter",
}


def build_diagnostics(
    *,
    novel_id: str,
    repository: Any,
    host_context_summary: dict[str, Any] | None = None,
    semantic_recall_summary: dict[str, Any] | None = None,
    agent_status: dict[str, Any] | None = None,
    route_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted diagnostic snapshot for a single novel."""
    host_context_summary = host_context_summary or {}
    semantic_recall_summary = semantic_recall_summary or {}
    agent_status = agent_status or {}
    route_map = route_map or {}
    hooks = list_hooks()
    risks: list[dict[str, Any]] = []

    _check_plugin_runtime(risks, hooks)
    _check_host_context(risks, host_context_summary)
    host_feature_alignment = _host_feature_alignment(host_context_summary)
    _check_host_feature_alignment(risks, host_feature_alignment)
    _check_semantic_recall(risks, novel_id, semantic_recall_summary)
    _check_dependency_status(risks)
    _check_agent_assets(risks, agent_status)
    _check_context_injection(risks, repository, novel_id)
    _check_route_map(risks, route_map)
    _check_character_pollution(risks, repository, novel_id)
    _check_recent_failures(risks, agent_status)
    _check_settings_conflict(risks, repository)
    context_budget_summary = _context_budget_summary(repository, novel_id)
    review_candidate_summary = _review_candidate_summary(repository, novel_id)
    injection_gate_summary = _injection_gate_summary(repository, novel_id)
    plugin_leakage_check = _plugin_leakage_check(repository, novel_id, agent_status)
    planning_alignment = agent_status.get("planning_alignment") if isinstance(agent_status.get("planning_alignment"), dict) else {}
    native_context_alignment = agent_status.get("native_context_alignment") if isinstance(agent_status.get("native_context_alignment"), dict) else {}
    agent_takeover_health = _agent_takeover_health(agent_status)
    knowledge_coverage = agent_status.get("knowledge_base") if isinstance(agent_status.get("knowledge_base"), dict) else {}
    knowledge_freshness = _knowledge_freshness(repository, novel_id, knowledge_coverage)
    gene_mutation_audit = agent_status.get("auto_evolution") if isinstance(agent_status.get("auto_evolution"), dict) else {}
    palette_status = agent_status.get("personality_palette_status") if isinstance(agent_status.get("personality_palette_status"), dict) else {}
    degraded_agent_tools = _degraded_agent_tools(agent_status)

    risks.sort(key=lambda item: (_severity_rank(item.get("severity")), str(item.get("source") or "")))
    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "novel_id": novel_id,
        "architecture_mode": "agent_first_hybrid",
        "generated_at": _now(),
        "summary": _risk_summary(risks),
        "runtime": {
            "plugin_name": PLUGIN_NAME,
            "enabled": _plugin_enabled(),
            "registered_hooks": {hook: [name for name in names if name == PLUGIN_NAME] for hook, names in hooks.items() if PLUGIN_NAME in names},
            "missing_hooks": sorted(EXPECTED_HOOKS.difference(hook for hook, names in hooks.items() if PLUGIN_NAME in names)),
            "duplicate_hooks": sorted(hook for hook, names in hooks.items() if names.count(PLUGIN_NAME) > 1),
        },
        "host_context_summary": _redact(host_context_summary),
        "host_feature_alignment": _redact(host_feature_alignment),
        "planning_alignment": _redact(planning_alignment),
        "native_context_alignment": _redact(native_context_alignment),
        "agent_takeover_health": _redact(agent_takeover_health),
        "knowledge_coverage": _redact(knowledge_coverage),
        "gene_mutation_audit": _redact(gene_mutation_audit),
        "personality_palette_status": _redact(palette_status),
        "degraded_agent_tools": _redact(degraded_agent_tools),
        "degraded_sources": list(host_context_summary.get("degraded_sources") or []),
        "empty_sources": list(host_context_summary.get("empty_sources") or []),
        "field_missing_sources": list(host_context_summary.get("field_missing_sources") or []),
        "semantic_recall_summary": _redact(semantic_recall_summary),
        "dependency_status": dependency_status(),
        "agent_asset_counts": dict(agent_status.get("asset_counts") or {}),
        "plugin_leakage_check": plugin_leakage_check,
        "context_budget_summary": context_budget_summary,
        "review_candidate_summary": review_candidate_summary,
        "injection_gate_summary": injection_gate_summary,
        "knowledge_freshness": knowledge_freshness,
        "risks": risks,
    }


def _check_plugin_runtime(risks: list[dict[str, Any]], hooks: dict[str, list[str]]) -> None:
    if not _plugin_enabled():
        risks.append(_risk("warning", "plugin_runtime", "插件当前被平台禁用，Evolution hook 不会参与主流程。", "开启插件后再进行写作压力测试。", "plugin_hooks"))
    missing = sorted(EXPECTED_HOOKS.difference(hook for hook, names in hooks.items() if PLUGIN_NAME in names))
    if missing:
        risks.append(_risk("critical", "plugin_runtime", f"Evolution 缺少 hook 注册：{', '.join(missing[:6])}", "检查插件加载与 __init__.py hook 注册。", "plugin_hooks", {"missing_hooks": missing}))
    duplicates = sorted(hook for hook, names in hooks.items() if names.count(PLUGIN_NAME) > 1)
    if duplicates:
        risks.append(_risk("warning", "plugin_runtime", f"Evolution hook 重复注册：{', '.join(duplicates[:6])}", "避免重复 include/import 导致 hook 执行多次。", "plugin_hooks", {"duplicate_hooks": duplicates}))


def _check_host_context(risks: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    degraded = [str(item) for item in summary.get("degraded_sources") or [] if str(item)]
    empty = [str(item) for item in summary.get("empty_sources") or [] if str(item)]
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    source_status = summary.get("source_status") if isinstance(summary.get("source_status"), dict) else {}
    if degraded:
        risks.append(_risk("warning", "host_context", f"宿主只读信息源缺表降级：{', '.join(degraded)}", "缺表时继续写作，但实验报告需标注对应 PlotPilot 功能未初始化或 schema 不兼容。", "host_context", {"degraded_sources": degraded, "source_status": source_status}))
    if not counts:
        risks.append(_risk("info", "host_context", "尚未形成外部信息源摘要。", "触发一次上下文构建或章节审查后会刷新。", "host_context"))
        return
    if empty:
        risks.append(_risk("info", "host_context", f"宿主表存在但暂无命中：{', '.join(empty[:8])}", "这表示 schema 可读但本小说尚未沉淀对应资料；不同于缺表降级。", "host_context_empty", {"empty_sources": empty, "counts": counts}))
    field_missing = [str(item) for item in summary.get("field_missing_sources") or [] if str(item)]
    if field_missing:
        risks.append(_risk("info", "host_context", f"宿主表存在但字段不完整：{', '.join(field_missing[:8])}", "Evolution 会使用兼容字段或降级摘要；实验报告需标注 schema 版本差异。", "host_context_schema", {"field_missing_sources": field_missing, "source_status": source_status}))
    if not any(int(value or 0) for value in counts.values()):
        risks.append(_risk("info", "host_context", "外部信息源均未命中。", "如果本书已配置世界观/故事线/伏笔，需检查 novel_id 隔离或宿主读取映射。", "host_context", {"counts": counts}))


def _host_feature_alignment(summary: dict[str, Any]) -> dict[str, Any]:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    usage = summary.get("plotpilot_context_usage") if isinstance(summary.get("plotpilot_context_usage"), dict) else {}
    native_sources = [
        "bible",
        "story_knowledge",
        "storyline",
        "foreshadow",
        "timeline",
        "dialogue",
        "triples",
        "memory_engine",
    ]
    return {
        "adapter": "plotpilot_native_context_adapter",
        "mode": usage.get("mode") or "strategy_only",
        "native_sources": {source: int(counts.get(source) or 0) for source in native_sources},
        "tier_hits": dict(usage.get("hit_counts_by_tier") or {}),
        "long_context_duplicated": bool(usage.get("long_context_duplicated")),
        "degraded_sources": list(summary.get("degraded_sources") or []),
        "empty_sources": list(summary.get("empty_sources") or []),
        "field_missing_sources": list(summary.get("field_missing_sources") or []),
        "source_status": dict(summary.get("source_status") or {}),
    }


def _context_budget_summary(repository: Any, novel_id: str) -> dict[str, Any]:
    records = repository.list_context_injection_records(novel_id, limit=1)
    latest = records[-1] if records else {}
    blocks = _context_blocks_from_record(latest)
    block_ids = [str(block.get("id") or block.get("title") or "") for block in blocks if block]
    token_budget = sum(int(block.get("token_budget") or 0) for block in blocks)
    tier_counts = {TIER_T0: 0, TIER_T1: 0, "unknown": 0}
    tier_chars = {TIER_T0: 0, TIER_T1: 0, "unknown": 0}
    block_tiers: list[dict[str, Any]] = []
    for block in blocks:
        tier = _block_tier(block)
        bucket = tier if tier in {TIER_T0, TIER_T1} else "unknown"
        chars = _block_chars(block)
        tier_counts[bucket] += 1
        tier_chars[bucket] += chars
        block_tiers.append({"id": block.get("id"), "kind": block.get("kind"), "tier": tier or "unknown", "chars": chars})
    return {
        "has_context_injection": bool(records),
        "block_count": len(blocks),
        "token_budget": token_budget,
        "t0_block_count": tier_counts[TIER_T0],
        "t1_block_count": tier_counts[TIER_T1],
        "tier_unknown_count": tier_counts["unknown"],
        "t0_chars": tier_chars[TIER_T0],
        "t1_chars": tier_chars[TIER_T1],
        "tier_unknown_chars": tier_chars["unknown"],
        "block_tiers": block_tiers,
        "duplicate_block_ids": _duplicates(item for item in block_ids if item),
        "strategy_only": any(block.get("id") == "plotpilot_native_strategy" for block in blocks),
        "latest_chapter": latest.get("chapter_number") if isinstance(latest, dict) else None,
        "legacy_record_normalized": bool(records) and not bool((latest or {}).get("blocks")) and bool(blocks),
    }


def _context_blocks_from_record(record: Any) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    candidates = [
        record.get("blocks"),
        record.get("selected"),
        record.get("context_blocks"),
    ]
    patch = record.get("context_patch") if isinstance(record.get("context_patch"), dict) else {}
    candidates.append(patch.get("blocks"))
    for value in candidates:
        blocks = [block for block in (value or []) if isinstance(block, dict)]
        if blocks:
            return blocks
    return []


def _review_candidate_summary(repository: Any, novel_id: str) -> dict[str, Any]:
    try:
        candidates = repository.list_review_candidates(novel_id, limit=500)
    except Exception:
        candidates = []
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for candidate in candidates:
        by_status[str(candidate.get("status") or "unknown")] = by_status.get(str(candidate.get("status") or "unknown"), 0) + 1
        by_type[str(candidate.get("candidate_type") or "unknown")] = by_type.get(str(candidate.get("candidate_type") or "unknown"), 0) + 1
        by_risk[str(candidate.get("risk_level") or "unknown")] = by_risk.get(str(candidate.get("risk_level") or "unknown"), 0) + 1
    return {
        "total": len(candidates),
        "pending": by_status.get("pending", 0),
        "by_status": by_status,
        "by_type": by_type,
        "by_risk": by_risk,
    }


def _injection_gate_summary(repository: Any, novel_id: str) -> dict[str, Any]:
    records = repository.list_context_injection_records(novel_id, limit=1)
    latest = records[-1] if records else {}
    decision = latest.get("gate_decision") if isinstance(latest, dict) and isinstance(latest.get("gate_decision"), dict) else {}
    return {
        "has_decision": bool(decision),
        "should_inject": bool(decision.get("should_inject")),
        "reasons": list(decision.get("reasons") or []),
        "skipped_reasons": list(decision.get("skipped_reasons") or []),
        "pending_review_count": int(decision.get("pending_review_count") or 0),
        "t0_chars": int(decision.get("t0_chars") or 0),
        "t1_chars": int(decision.get("t1_chars") or 0),
        "skipped_block_count": int(decision.get("skipped_block_count") or latest.get("skipped_count") or 0),
        "latest_chapter": latest.get("chapter_number") if isinstance(latest, dict) else None,
    }


def _knowledge_freshness(repository: Any, novel_id: str, coverage: dict[str, Any]) -> dict[str, Any]:
    facts = repository.list_fact_snapshots(novel_id, limit=0)
    latest_fact = max((_int_or_none(item.get("chapter_number")) or 0 for item in facts), default=0)
    chunks = repository.list_agent_knowledge_chunks(novel_id, limit=0)
    latest_chunk = max((_int_or_none(item.get("chapter_number")) or 0 for item in chunks), default=0)
    return {
        "latest_fact_chapter": latest_fact,
        "latest_knowledge_chapter": latest_chunk,
        "is_stale": bool(latest_fact and latest_chunk < latest_fact),
        "document_count": int(coverage.get("document_count") or 0),
        "chunk_count": int(coverage.get("chunk_count") or 0),
    }


def _agent_takeover_health(agent_status: dict[str, Any]) -> dict[str, Any]:
    orchestration = agent_status.get("agent_orchestration") if isinstance(agent_status.get("agent_orchestration"), dict) else {}
    knowledge = agent_status.get("knowledge_base") if isinstance(agent_status.get("knowledge_base"), dict) else {}
    auto = agent_status.get("auto_evolution") if isinstance(agent_status.get("auto_evolution"), dict) else {}
    decision_count = int(orchestration.get("decision_count") or 0)
    degraded = int(orchestration.get("degraded_decision_count") or 0)
    return {
        "mode": "agent_first_hybrid",
        "decision_boundary": orchestration.get("decision_boundary") or "agent_orchestrator",
        "decision_count": decision_count,
        "degraded_decision_count": degraded,
        "healthy": bool(decision_count and int(knowledge.get("chunk_count") or 0) > 0),
        "knowledge_chunk_count": int(knowledge.get("chunk_count") or 0),
        "auto_evolution_mode": auto.get("mode") or "immediate",
        "gene_version_count": int(auto.get("gene_version_count") or 0),
    }


def _degraded_agent_tools(agent_status: dict[str, Any]) -> list[dict[str, Any]]:
    orchestration = agent_status.get("agent_orchestration") if isinstance(agent_status.get("agent_orchestration"), dict) else {}
    degraded = int(orchestration.get("degraded_decision_count") or 0)
    if not degraded:
        return []
    return [
        {
            "tool": "agent_orchestrator",
            "degraded_decision_count": degraded,
            "latest_phase": orchestration.get("latest_phase"),
            "latest_status": orchestration.get("latest_status"),
        }
    ]


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


def _plugin_leakage_check(repository: Any, novel_id: str, agent_status: dict[str, Any]) -> dict[str, Any]:
    asset_counts = agent_status.get("asset_counts") if isinstance(agent_status.get("asset_counts"), dict) else {}
    injection_count = len(repository.list_context_injection_records(novel_id, limit=5))
    review_count = len(repository.list_review_records(novel_id, limit=5))
    learned_count = (
        int(asset_counts.get("events") or 0)
        + int(asset_counts.get("capsules") or 0)
        + int(asset_counts.get("reflections") or 0)
        + int(asset_counts.get("gene_candidates") or 0)
    )
    return {
        "plugin_name": PLUGIN_NAME,
        "enabled": _plugin_enabled(),
        "context_injection_records": injection_count,
        "review_records": review_count,
        "agent_learning_assets": learned_count,
        "has_evolution_activity": bool(injection_count or review_count or learned_count),
    }


def _check_host_feature_alignment(risks: list[dict[str, Any]], alignment: dict[str, Any]) -> None:
    native_sources = alignment.get("native_sources") if isinstance(alignment.get("native_sources"), dict) else {}
    if native_sources and not any(int(value or 0) for value in native_sources.values()):
        risks.append(_risk("info", "host_feature_alignment", "PlotPilot 原生资料适配器尚未命中 Bible/知识库/故事线/伏笔/时间线数据。", "新建实验小说前先初始化原生设定，或在报告中标注原生资料参与不足。", "plotpilot_native_context_adapter", {"native_sources": native_sources}))
    if alignment.get("long_context_duplicated"):
        risks.append(_risk("warning", "host_feature_alignment", "检测到原生资料可能被长文本重复注入。", "Evolution 应只输出短策略块，避免和 PlotPilot 洋葱上下文重复。", "context_budget", alignment))


def _check_semantic_recall(risks: list[dict[str, Any]], novel_id: str, summary: dict[str, Any]) -> None:
    if not summary:
        risks.append(_risk("info", "semantic_recall", "尚未形成语义召回摘要。", "触发一次上下文构建后会刷新向量/keyword 状态。", "semantic_recall"))
        return
    if not summary.get("vector_enabled"):
        risks.append(_risk("warning", "semantic_recall", "本地向量能力未启用，当前只能依赖 SQL keyword 或外部信息源摘要。", "确认 faiss/torch/sentence-transformers 和 embedding 配置。", "semantic_recall"))
    elif int(summary.get("item_count") or 0) == 0:
        risks.append(_risk("info", "semantic_recall", "向量能力可用，但当前小说没有语义召回命中。", "后续章节提交/索引生成后再观察；也可检查 novel_id 对应向量集合。", "semantic_recall", {"novel_id": novel_id, "source": summary.get("source")}))
    collection_status = summary.get("collection_status") if isinstance(summary.get("collection_status"), dict) else {}
    missing = [str(item) for item in collection_status.get("missing") or [] if str(item)]
    queried = [str(item) for item in collection_status.get("queried") or [] if str(item)]
    if missing:
        risks.append(_risk("info", "semantic_recall", f"部分向量集合不存在：{len(missing)} 个。", "只查询已存在集合；缺失集合通常表示该类资料尚未索引。", "semantic_recall", {"missing_collections": missing[:12], "queried_collections": queried[:12]}))


def _check_dependency_status(risks: list[dict[str, Any]]) -> None:
    status = dependency_status()
    missing = [name for name, ok in status.items() if not ok]
    if missing:
        risks.append(_risk("warning", "dependencies", f"本地向量依赖缺失：{', '.join(missing)}", "缺失时 Evolution 会降级到宿主只读/keyword 检索。", "semantic_recall", {"dependencies": status}))


def _check_agent_assets(risks: list[dict[str, Any]], status: dict[str, Any]) -> None:
    counts = status.get("asset_counts") if isinstance(status.get("asset_counts"), dict) else {}
    if int(counts.get("genes") or 0) == 0:
        risks.append(_risk("critical", "agent_assets", "默认 Gene 未加载，智能体无法选择策略。", "检查 agent_assets.default_genes 与 repository.list_agent_genes。", "agent_memory"))
    top_genes = status.get("top_genes") if isinstance(status.get("top_genes"), list) else []
    duplicate_gene_ids = _duplicates(str(item.get("id") or "") for item in top_genes if isinstance(item, dict))
    if duplicate_gene_ids:
        risks.append(_risk("warning", "agent_assets", f"状态页发现重复 Gene：{', '.join(duplicate_gene_ids[:6])}", "保持 Gene id 唯一，避免策略贡献统计偏移。", "agent_memory", {"duplicate_gene_ids": duplicate_gene_ids}))
    candidates = status.get("gene_candidates") if isinstance(status.get("gene_candidates"), list) else []
    if len(candidates) > 20:
        risks.append(_risk("info", "agent_assets", "候选 Gene 数量偏多，可能需要人工合并。", "保留只读待审，不自动提升正式 Gene。", "agent_memory", {"candidate_count": len(candidates)}))


def _check_context_injection(risks: list[dict[str, Any]], repository: Any, novel_id: str) -> None:
    records = repository.list_context_injection_records(novel_id, limit=5)
    if not records:
        risks.append(_risk("info", "context_injection", "暂无上下文注入记录。", "开始生成章节后应出现注入记录。", "context_injection"))
        return
    latest = records[-1]
    block_ids = [str(block.get("id") or block.get("title") or "") for block in latest.get("blocks") or [] if isinstance(block, dict)]
    duplicate_ids = _duplicates(item for item in block_ids if item)
    if duplicate_ids:
        risks.append(_risk("warning", "context_injection", f"上下文块重复：{', '.join(duplicate_ids[:6])}", "合并重复块或调整 capsule 去重键。", "context_injection", {"duplicate_block_ids": duplicate_ids}))
    total_budget = sum(int(block.get("token_budget") or 0) for block in latest.get("blocks") or [] if isinstance(block, dict))
    if total_budget > 6000:
        risks.append(_risk("warning", "context_injection", f"最近一次上下文 token budget 偏高：{total_budget}", "优先压缩 host context、semantic memory、agent strategy 块。", "context_budget", {"token_budget": total_budget}))


def _check_route_map(risks: list[dict[str, Any]], route_map: dict[str, Any]) -> None:
    degraded = route_map.get("diagnostic_degraded") if isinstance(route_map.get("diagnostic_degraded"), dict) else {}
    if degraded:
        risks.append(_risk("warning", "route_map", "路线图诊断降级，当前无法完整检查行进冲突。", "检查 story graph 数据和路线图构建逻辑；写作流程不应被诊断阻塞。", "route_conflict", degraded))
        return
    aggregate = route_map.get("aggregate") if isinstance(route_map.get("aggregate"), dict) else {}
    hard = int(aggregate.get("hard_conflict_count") or 0)
    total = int(aggregate.get("conflict_count") or len(route_map.get("conflicts") or []))
    conflict_breakdown = _route_conflict_breakdown(route_map.get("conflicts") or [])
    if hard:
        risks.append(_risk("critical", "route_map", f"路线图存在 {hard} 个硬冲突。", _route_suggestion(conflict_breakdown), "route_conflict", {"hard_conflict_count": hard, "conflict_breakdown": conflict_breakdown}))
    elif total:
        risks.append(_risk("warning", "route_map", f"路线图存在 {total} 个待审冲突。", _route_suggestion(conflict_breakdown), "route_conflict", {"conflict_count": total, "conflict_breakdown": conflict_breakdown}))


def _check_character_pollution(risks: list[dict[str, Any]], repository: Any, novel_id: str) -> None:
    cards = (
        repository.list_all_character_cards(novel_id).get("items", [])
        if hasattr(repository, "list_all_character_cards")
        else repository.list_character_cards(novel_id).get("items", [])
    )
    invalid = [card for card in cards if str(card.get("status") or "") == "invalid_entity" or str(card.get("entity_type") or "") == "non_person"]
    if invalid:
        risks.append(_risk("warning", "character_cards", f"人物卡中有 {len(invalid)} 个污染实体已标记 invalid_entity。", "这些实体应只作为 world facts/props 参考，不进入角色卡主视图或上下文注入。", "character_cards", {"invalid_entities": [_invalid_character_evidence(card) for card in invalid[:8]], "invalid_count": len(invalid)}))
    active_cards = [card for card in cards if not _invalid_character_card(card)]
    if active_cards:
        status = personality_palette_status(active_cards)
        if status.get("character_count"):
            risks.append(
                _risk(
                    "info",
                    "character_cards",
                    f"性格调色盘覆盖率：{status.get('complete_count', 0)}/{status.get('character_count', 0)}。",
                    "覆盖率低时，Evolution 会从原生 Bible 或章节行为中保守补全；完整后才注入具体调色盘短策略。",
                    "personality_palette",
                    status,
                )
            )
    missing_palette = [card for card in active_cards if _palette_missing(card)]
    if missing_palette:
        severity = "warning" if len(missing_palette) >= 3 else "info"
        risks.append(_risk(severity, "character_cards", f"{len(missing_palette)} 张人物卡性格调色盘不完整。", "重点角色下一次出场时应根据行为推断底色、主色调和点缀，不要只写单一标签。", "personality_palette", {"missing_palette": [_palette_evidence(card) for card in missing_palette[:8]], "missing_count": len(missing_palette)}))


def _route_conflict_breakdown(conflicts: list[Any]) -> dict[str, int]:
    aliases = {
        "location_jump_without_bridge": "missing_transition",
        "repeated_arrival": "repeated_arrival",
        "boundary_rollback": "boundary_rollback",
    }
    result: dict[str, int] = {}
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        key = aliases.get(str(conflict.get("type") or ""), str(conflict.get("type") or "route_conflict") or "route_conflict")
        result[key] = result.get(key, 0) + 1
    return result


def _route_suggestion(breakdown: dict[str, int]) -> str:
    if breakdown.get("repeated_arrival"):
        return "优先处理重复抵达：下一章开头承接在场状态；如需重新进入，补足离开、转场和再次抵达。"
    if breakdown.get("missing_transition"):
        return "优先补移动桥段：写清路线、时间消耗、跳时提示或视角桥接。"
    if breakdown.get("boundary_rollback"):
        return "优先核对章节首尾：上一章终点和下一章开头必须连续，跳时空需显式说明。"
    return "在下一章上下文中注入移动桥段或状态解释。"


def _invalid_character_card(card: dict[str, Any]) -> bool:
    return str(card.get("status") or "") == "invalid_entity" or str(card.get("entity_type") or "") == "non_person"


def _invalid_character_evidence(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(card.get("name") or ""),
        "first_seen_chapter": card.get("first_seen_chapter"),
        "last_seen_chapter": card.get("last_seen_chapter"),
        "invalid_reason": str(card.get("invalid_reason") or ""),
    }


def _palette_missing(card: dict[str, Any]) -> bool:
    return bool(palette_missing_fields(card.get("personality_palette") if isinstance(card, dict) else {}))


def _palette_evidence(card: dict[str, Any]) -> dict[str, Any]:
    palette = card.get("personality_palette") if isinstance(card.get("personality_palette"), dict) else {}
    return {
        "name": str(card.get("name") or ""),
        "last_seen_chapter": card.get("last_seen_chapter"),
        "missing_fields": palette_missing_fields(palette),
        "source": str(palette.get("source") or "unspecified"),
    }


def _check_recent_failures(risks: list[dict[str, Any]], status: dict[str, Any]) -> None:
    events = status.get("recent_events") if isinstance(status.get("recent_events"), list) else []
    failures = [event for event in events if isinstance(event, dict) and (event.get("outcome") or {}).get("status") == "failed"]
    if failures:
        risks.append(_risk("warning", "agent_events", f"最近有 {len(failures)} 个智能体事件失败。", "查看 failed outcome，确认 agent API/控制卡/反思器降级路径。", "agent_memory", {"failed_event_ids": [str(event.get("id") or "") for event in failures[-5:]]}))


def _check_settings_conflict(risks: list[dict[str, Any]], repository: Any) -> None:
    settings = repository.get_settings()
    api2 = settings.get("api2_control_card") if isinstance(settings.get("api2_control_card"), dict) else {}
    legacy_api2 = settings.get("api2") if isinstance(settings.get("api2"), dict) else {}
    api2_enabled = bool(api2.get("enabled") or legacy_api2.get("enabled"))
    if api2_enabled:
        risks.append(_risk("info", "settings", "检测到旧 API2 配置残留；API2 已不再参与 Evolution 运行。", "请在智能体 API 中重新配置二号模型；旧 API2 设置仅作为兼容数据保留。", "settings"))


def _risk(severity: str, source: str, message: str, suggestion: str, affected_feature: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "severity": severity,
        "source": source,
        "message": message,
        "suggestion": suggestion,
        "affected_feature": affected_feature,
        "evidence": _redact(evidence or {}),
    }


def _risk_summary(risks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "critical": sum(1 for item in risks if item.get("severity") == "critical"),
        "warning": sum(1 for item in risks if item.get("severity") == "warning"),
        "info": sum(1 for item in risks if item.get("severity") == "info"),
        "total": len(risks),
    }


def _plugin_enabled() -> bool:
    try:
        from plugins.loader import is_plugin_enabled

        return bool(is_plugin_enabled(PLUGIN_NAME))
    except Exception:
        return True


def dependency_status() -> dict[str, bool]:
    return {
        "faiss": importlib.util.find_spec("faiss") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
        "sentence_transformers": importlib.util.find_spec("sentence_transformers") is not None,
    }


def _duplicates(values: Any) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return sorted(dupes)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_str = str(key)
            if any(token in key_str.lower() for token in ("api_key", "apikey", "secret", "token", "password", "authorization")):
                redacted[key] = "[redacted]" if item else ""
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value[:40]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


def _severity_rank(value: Any) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(str(value), 3)


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
