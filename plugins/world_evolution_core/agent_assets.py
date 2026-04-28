"""Lightweight GEP-style agent assets for Evolution World.

The design borrows the Gene/Capsule/Event loop from EvoMap/evolver, but keeps
the runtime local to this plugin and deterministic for PlotPilot hooks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional


AGENT_SCHEMA_VERSION = 1


DEFAULT_GENES: list[dict[str, Any]] = [
    {
        "type": "Gene",
        "id": "gene_chapter_bridge_continuity",
        "category": "continuity",
        "title": "章节承接",
        "signals_match": ["chapter_bridge", "ending_state", "state_reset"],
        "strategy": [
            "下一章开头必须承接上一章结尾的时间、地点、在场角色和物件状态。",
            "如果需要跳时空，先写转场、延迟或视角桥接，不要让人物重新进入已经抵达的地点。",
        ],
        "priority": 92,
    },
    {
        "type": "Gene",
        "id": "gene_route_conflict_guard",
        "category": "route",
        "title": "路线冲突守卫",
        "signals_match": ["route_conflict", "location_jump", "repeat_entry"],
        "strategy": [
            "写作前检查人物上一章终点；移动必须有路线、时间消耗或明确省略。",
            "同一人物同一地点的重复进入只能作为回忆、确认或二次行动，不能无解释重置状态。",
        ],
        "priority": 90,
    },
    {
        "type": "Gene",
        "id": "gene_character_cognition_boundary",
        "category": "character_logic",
        "title": "角色认知边界",
        "signals_match": ["character_cognition", "knowledge_boundary", "misbelief", "capability_boundary", "bible_context", "memory_engine_context"],
        "strategy": [
            "角色只能使用他已经知道、看见、推断或被告知的信息。",
            "能力突破需要试错、代价、外部帮助或失败风险，不能静默升级为全知全能。",
        ],
        "priority": 86,
    },
    {
        "type": "Gene",
        "id": "gene_personality_palette_consistency",
        "category": "character_voice",
        "title": "性格调色盘一致性",
        "signals_match": ["personality_palette", "character_voice", "palette_missing", "palette_drift"],
        "strategy": [
            "人物行为要由底色、主色调和点缀共同驱动；不要只写单一标签。",
            "当人物违背既有性格时，给出情境压力、关系触发或成长变化。",
        ],
        "priority": 76,
    },
    {
        "type": "Gene",
        "id": "gene_repetition_phrase_guard",
        "category": "style",
        "title": "重复模板句守卫",
        "signals_match": ["repetition_phrase", "style_repetition", "phrase_guard", "dialogue_voice_context"],
        "strategy": [
            "避免复用高频模板句，尤其是没有说话、没有回答、沉默了几秒、深吸一口气。",
            "用动作、视线、空间调度或具体物件替代空泛反应。",
        ],
        "priority": 68,
    },
    {
        "type": "Gene",
        "id": "gene_plotpilot_native_context_alignment",
        "category": "native_context",
        "title": "PlotPilot 原生资料协同",
        "signals_match": [
            "bible_context",
            "story_knowledge_context",
            "storyline_context",
            "foreshadow_context",
            "timeline_context",
            "triples_context",
            "memory_engine_context",
        ],
        "strategy": [
            "优先使用 PlotPilot Bible、章后叙事同步、故事线、伏笔账本和时间线作为事实源。",
            "Evolution 只补充短约束：承接、路线、伏笔推进、角色边界和重复表达控制，不重复塞入长资料。",
        ],
        "priority": 88,
    },
]


SOLIDIFIABLE_ISSUE_TYPES = {
    "evolution_character_cognition",
    "evolution_character_belief",
    "evolution_character_capability",
    "evolution_character_logic",
    "evolution_plot_continuity",
    "evolution_boundary_state",
    "evolution_entity_pollution",
    "evolution_character_pollution",
    "evolution_location_pollution",
    "evolution_style_repetition",
    "evolution_palette_missing",
    "evolution_palette_drift",
    "evolution_character_role_shift",
    "evolution_bible_context",
    "evolution_worldbuilding_context",
    "evolution_knowledge_context",
    "evolution_story_knowledge_context",
    "evolution_storyline_context",
    "evolution_timeline_context",
    "evolution_chronicle_context",
    "evolution_foreshadow_context",
    "evolution_dialogue_voice_context",
    "evolution_triples_context",
    "evolution_memory_engine_context",
}


def default_genes() -> list[dict[str, Any]]:
    return [dict(gene, strategy=list(gene.get("strategy") or []), signals_match=list(gene.get("signals_match") or [])) for gene in DEFAULT_GENES]


def build_commit_event(
    *,
    novel_id: str,
    chapter_number: int,
    content_hash: str,
    snapshot: dict[str, Any],
    story_graph: dict[str, Any],
    at: Optional[str] = None,
) -> dict[str, Any]:
    signals = ["chapter_committed"]
    if snapshot.get("characters"):
        signals.append("character_state")
    if snapshot.get("locations"):
        signals.append("route_tracking")
    if story_graph.get("conflicts"):
        signals.extend(["route_conflict", "location_jump"])
    return {
        "type": "EvolutionEvent",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": _event_id("commit", novel_id, chapter_number, content_hash),
        "intent": "observe",
        "hook_name": "after_commit",
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "signals": _dedupe(signals),
        "genes_used": [],
        "capsule_id": None,
        "outcome": {
            "status": "success",
            "characters": list(snapshot.get("characters") or [])[:12],
            "locations": list(snapshot.get("locations") or [])[:12],
            "route_conflict_count": len(story_graph.get("conflicts") or []),
        },
        "meta": {"at": at or _now(), "content_hash": content_hash},
    }


def extract_context_signals(
    *,
    outline: str = "",
    chapter_summaries: Optional[list[dict[str, Any]]] = None,
    route_map: Optional[dict[str, Any]] = None,
    semantic_memory: Optional[dict[str, Any]] = None,
    review_records: Optional[list[dict[str, Any]]] = None,
    host_context: Optional[dict[str, Any]] = None,
) -> list[str]:
    text = str(outline or "")
    signals = ["context_build"]
    summaries = chapter_summaries or []
    if summaries:
        signals.extend(["chapter_bridge", "ending_state"])
    if any(token in text for token in ["上一章", "承接", "结尾", "继续", "进入", "抵达", "离开"]):
        signals.append("chapter_bridge")
    if any(token in text for token in ["地点", "路线", "抵达", "进入", "离开", "赶到", "回到"]):
        signals.append("route_tracking")
    if isinstance(route_map, dict) and route_map.get("conflicts"):
        signals.extend(["route_conflict", "location_jump"])
    if isinstance(semantic_memory, dict) and semantic_memory.get("items"):
        signals.append("semantic_memory")
        if semantic_memory.get("vector_enabled"):
            signals.append("semantic_embedding_recall")
    if isinstance(host_context, dict):
        active = set(str(item) for item in host_context.get("active_sources") or [])
        if "bible" in active:
            signals.append("bible_context")
        if "world" in active:
            signals.append("worldbuilding_context")
        if "knowledge" in active:
            signals.append("knowledge_context")
        if "story_knowledge" in active:
            signals.append("story_knowledge_context")
        if "storyline" in active:
            signals.append("storyline_context")
        if "timeline" in active:
            signals.append("timeline_context")
        if "chronicle" in active:
            signals.append("chronicle_context")
        if "foreshadow" in active:
            signals.append("foreshadow_context")
        if "dialogue" in active:
            signals.append("dialogue_voice_context")
        if "triples" in active:
            signals.append("triples_context")
        if "memory_engine" in active:
            signals.append("memory_engine_context")
    if any(_int_or_none(record.get("issue_count")) for record in (review_records or [])):
        signals.append("review_feedback")
    if any(token in text for token in ["性格", "口吻", "调色盘", "情绪", "成长"]):
        signals.extend(["personality_palette", "character_voice"])
    return _dedupe(signals)


def select_agent_assets(
    *,
    novel_id: str,
    chapter_number: Optional[int],
    signals: list[str],
    genes: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
    outline: str = "",
    max_genes: int = 3,
    max_capsules: int = 4,
    at: Optional[str] = None,
) -> dict[str, Any]:
    signal_set = set(signals)
    gene_scores = []
    for gene in genes:
        matches = signal_set.intersection(str(item) for item in gene.get("signals_match") or [])
        if not matches:
            continue
        score = (
            int(gene.get("priority") or 50)
            + len(matches) * 8
            + _gene_positive_score(gene)
            - _mild_failure_penalty(gene)
        )
        gene_scores.append((score, str(gene.get("id") or ""), {**gene, "matched_signals": sorted(matches)}))
    gene_scores.sort(key=lambda item: (-item[0], item[1]))
    selected_genes = [item[2] for item in gene_scores[:max_genes]]

    capsule_scores = []
    outline_text = str(outline or "")
    for capsule in capsules:
        capsule_signals = {str(item) for item in capsule.get("signals") or []}
        matches = signal_set.intersection(capsule_signals)
        text_match = _capsule_text_matches(capsule, outline_text)
        if not matches and not text_match:
            continue
        score = len(matches) * 20 + int(capsule.get("success_count") or 0) * 4 - int(capsule.get("failure_count") or 0) * 6
        if text_match:
            score += 12
        capsule_scores.append((score, str(capsule.get("id") or ""), {**capsule, "matched_signals": sorted(matches)}))
    capsule_scores.sort(key=lambda item: (-item[0], item[1]))
    selected_capsules = [item[2] for item in capsule_scores[:max_capsules]]

    record = {
        "type": "AgentSelection",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": _event_id("select", novel_id, chapter_number or 0, "|".join(signals)),
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "signals": signals,
        "selected_gene_ids": [str(item.get("id") or "") for item in selected_genes],
        "selected_capsule_ids": [str(item.get("id") or "") for item in selected_capsules],
        "selected_genes": selected_genes,
        "selected_capsules": selected_capsules,
        "rationale": _selection_rationale(selected_genes, selected_capsules),
        "at": at or _now(),
    }
    return record


def render_agent_selection(selection: Optional[dict[str, Any]]) -> str:
    if not isinstance(selection, dict):
        return ""
    genes = [item for item in selection.get("selected_genes") or [] if isinstance(item, dict)]
    capsules = [item for item in selection.get("selected_capsules") or [] if isinstance(item, dict)]
    if not genes and not capsules:
        return ""
    lines = [
        "Evolution 智能体已根据本章信号选择策略。以下内容只用于约束连续性与表达，不要逐条复述。"
    ]
    if genes:
        lines.append("【策略 Gene】")
        for gene in genes[:3]:
            strategy = "；".join(str(item) for item in (gene.get("strategy") or [])[:2] if str(item).strip())
            title = str(gene.get("title") or gene.get("id") or "Gene")
            if strategy:
                lines.append(f"- {title}：{strategy}")
    if capsules:
        lines.append("【经验 Capsule】")
        for capsule in capsules[:4]:
            title = str(capsule.get("title") or capsule.get("id") or "Capsule")
            guidance = str(capsule.get("guidance") or capsule.get("summary") or "").strip()
            source = capsule.get("last_seen_chapter") or capsule.get("chapter_number")
            suffix = f"（最近第{source}章）" if source else ""
            if guidance:
                lines.append(f"- {title}{suffix}：{guidance}")
    return "\n".join(lines)


def build_selection_event(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "EvolutionEvent",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": _event_id("inject", selection.get("novel_id"), selection.get("chapter_number") or 0, selection.get("id")),
        "intent": "inject",
        "hook_name": "before_context_build",
        "novel_id": selection.get("novel_id"),
        "chapter_number": selection.get("chapter_number"),
        "signals": list(selection.get("signals") or []),
        "genes_used": list(selection.get("selected_gene_ids") or []),
        "capsule_id": None,
        "outcome": {
            "status": "success",
            "capsules_used": list(selection.get("selected_capsule_ids") or []),
        },
        "meta": {"at": selection.get("at") or _now(), "selection_id": selection.get("id")},
    }


def solidify_capsules_from_review(
    *,
    novel_id: str,
    chapter_number: int,
    issues: list[dict[str, Any]],
    existing_capsules: list[dict[str, Any]],
    at: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_by_id = {str(item.get("id") or ""): item for item in existing_capsules if item.get("id")}
    solidified: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    now = at or _now()
    for issue in issues:
        if not _is_solidifiable_issue(issue):
            continue
        capsule_id = _capsule_id(issue)
        previous = existing_by_id.get(capsule_id) or {}
        capsule = {
            "type": "Capsule",
            "schema_version": AGENT_SCHEMA_VERSION,
            "id": capsule_id,
            "title": _capsule_title(issue),
            "category": _capsule_category(issue),
            "signals": _signals_for_issue(issue),
            "summary": str(issue.get("description") or "").strip()[:260],
            "guidance": str(issue.get("suggestion") or "").strip()[:260],
            "source_issue_type": str(issue.get("issue_type") or ""),
            "severity": str(issue.get("severity") or ""),
            "evidence": list(issue.get("evidence") or [])[:4],
            "chapter_number": chapter_number,
            "first_seen_chapter": previous.get("first_seen_chapter") or chapter_number,
            "last_seen_chapter": chapter_number,
            "success_count": int(previous.get("success_count") or 0) + 1,
            "failure_count": int(previous.get("failure_count") or 0),
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
        }
        solidified.append(capsule)
        existing_by_id[capsule_id] = capsule
        events.append(
            {
                "type": "EvolutionEvent",
                "schema_version": AGENT_SCHEMA_VERSION,
                "id": _event_id("solidify", novel_id, chapter_number, capsule_id),
                "intent": "solidify",
                "hook_name": "after_chapter_review",
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "signals": capsule["signals"],
                "genes_used": [],
                "capsule_id": capsule_id,
                "outcome": {"status": "success", "severity": capsule["severity"]},
                "meta": {"at": now, "source_issue_type": capsule["source_issue_type"]},
            }
        )
    return solidified, events


def build_reflection_record(
    *,
    novel_id: str,
    chapter_number: int,
    capsules: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    content: str = "",
    structured: Optional[dict[str, Any]] = None,
    source: str = "fallback",
    model: str = "",
    token_usage: Optional[dict[str, Any]] = None,
    ok: bool = True,
    error: str = "",
    at: Optional[str] = None,
) -> dict[str, Any]:
    now = at or _now()
    issue_types = _dedupe([issue.get("issue_type") for issue in issues])
    capsule_ids = [str(item.get("id") or "") for item in capsules if item.get("id")]
    pattern = str((structured or {}).get("problem_pattern") or _reflection_pattern(issues, capsules))
    root_cause = str((structured or {}).get("root_cause") or _reflection_root_cause(issues))
    next_constraints = (structured or {}).get("next_chapter_constraints")
    if not isinstance(next_constraints, list):
        next_constraints = _reflection_constraints(capsules, issues)
    evidence_refs = (structured or {}).get("evidence_refs")
    if not isinstance(evidence_refs, list):
        evidence_refs = _reflection_evidence_refs(issues)
    suggest_gene_candidate = bool((structured or {}).get("suggest_gene_candidate") or _should_reflection_suggest_candidate(capsules))
    digest = sha256("|".join([novel_id, str(chapter_number), "|".join(capsule_ids), pattern, root_cause]).encode("utf-8")).hexdigest()[:16]
    return {
        "type": "Reflection",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": f"ref_{digest}",
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "source": source,
        "ok": ok,
        "model": model,
        "capsule_ids": capsule_ids,
        "issue_types": issue_types,
        "problem_pattern": pattern[:260],
        "root_cause": root_cause[:360],
        "next_chapter_constraints": [str(item).strip()[:220] for item in next_constraints if str(item).strip()][:6],
        "evidence_refs": evidence_refs[:8],
        "suggest_gene_candidate": suggest_gene_candidate,
        "content": str(content or "").strip()[:1200],
        "error": str(error or "")[:300],
        "token_usage": token_usage or {},
        "created_at": now,
        "updated_at": now,
    }


def evaluate_strategy_effectiveness(
    *,
    novel_id: str,
    chapter_number: int,
    issues: list[dict[str, Any]],
    selection: Optional[dict[str, Any]],
    genes: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
    at: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Update selected Gene/Capsule counters from the review result.

    A selected strategy fails when a review issue shares one of its signals.
    If no matching issue appears, the selected strategy gets a conservative
    success. This keeps the loop measurable without letting the agent rewrite
    its own Gene definitions.
    """
    if not isinstance(selection, dict) or not selection:
        return genes, capsules, {}
    issue_signals = set()
    failure_reasons: list[str] = []
    for issue in issues:
        signals = _signals_for_issue(issue)
        issue_signals.update(signals)
        if signals:
            failure_reasons.append(str(issue.get("description") or issue.get("issue_type") or "")[:180])

    selected_gene_ids = {str(item) for item in selection.get("selected_gene_ids") or [] if str(item)}
    selected_capsule_ids = {str(item) for item in selection.get("selected_capsule_ids") or [] if str(item)}
    updated_gene_ids: list[str] = []
    updated_capsule_ids: list[str] = []
    failures: list[str] = []
    successes: list[str] = []
    protected: list[str] = []
    helpful: list[str] = []
    needs_improvement: list[str] = []

    def matched(asset: dict[str, Any]) -> bool:
        return bool(issue_signals.intersection(str(item) for item in asset.get("signals_match") or asset.get("signals") or []))

    next_genes: list[dict[str, Any]] = []
    for gene in genes:
        gene_id = str(gene.get("id") or "")
        item = dict(gene)
        if gene_id in selected_gene_ids:
            item["hit_count"] = int(item.get("hit_count") or 0) + 1
            if matched(item):
                item["failure_count"] = int(item.get("failure_count") or 0) + 1
                item["last_failure_chapter"] = chapter_number
                item["last_failure_reason"] = "; ".join(reason for reason in failure_reasons if reason)[:260]
                item["last_improvement_advice"] = _improvement_advice(item, failure_reasons)
                item["positive_score"] = max(0, int(item.get("positive_score") or 0) - 1)
                failures.append(gene_id)
                needs_improvement.append(gene_id)
            else:
                item["success_count"] = int(item.get("success_count") or 0) + 1
                item["protected_count"] = int(item.get("protected_count") or 0) + 1
                item["positive_score"] = int(item.get("positive_score") or 0) + 2
                item["last_positive_reason"] = _positive_reason(item, issues)
                item["last_success_chapter"] = chapter_number
                successes.append(gene_id)
                protected.append(gene_id)
                if not issues:
                    item["helpful_count"] = int(item.get("helpful_count") or 0) + 1
                    item["positive_score"] = int(item.get("positive_score") or 0) + 1
                    helpful.append(gene_id)
            item["updated_at"] = at or _now()
            updated_gene_ids.append(gene_id)
        next_genes.append(item)

    next_capsules: list[dict[str, Any]] = []
    for capsule in capsules:
        capsule_id = str(capsule.get("id") or "")
        item = dict(capsule)
        if capsule_id in selected_capsule_ids:
            item["hit_count"] = int(item.get("hit_count") or 0) + 1
            if matched(item):
                item["failure_count"] = int(item.get("failure_count") or 0) + 1
                item["last_failure_chapter"] = chapter_number
                item["last_failure_reason"] = "; ".join(reason for reason in failure_reasons if reason)[:260]
                failures.append(capsule_id)
            else:
                item["success_count"] = int(item.get("success_count") or 0) + 1
                item["last_success_chapter"] = chapter_number
                successes.append(capsule_id)
            item["updated_at"] = at or _now()
            updated_capsule_ids.append(capsule_id)
        next_capsules.append(item)

    event = {
        "type": "EvolutionEvent",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": _event_id("evaluate", novel_id, chapter_number, selection.get("id"), "|".join(sorted(issue_signals))),
        "intent": "evaluate",
        "hook_name": "after_chapter_review",
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "signals": sorted(issue_signals) or ["no_review_issue"],
        "genes_used": sorted(selected_gene_ids),
        "capsule_id": None,
        "outcome": {
            "status": "success",
            "successes": successes,
            "protected": protected,
            "helpful": helpful,
            "failures": failures,
            "needs_improvement": needs_improvement,
            "updated_gene_ids": updated_gene_ids,
            "updated_capsule_ids": updated_capsule_ids,
            "issue_count": len(issues),
        },
        "meta": {"at": at or _now(), "selection_id": selection.get("id")},
    }
    return next_genes, next_capsules, event


def consolidate_agent_memory(
    *,
    novel_id: str,
    chapter_number: int,
    genes: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
    reflections: list[dict[str, Any]],
    existing_candidates: list[dict[str, Any]],
    at: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    now = at or _now()
    existing_by_id = {str(item.get("id") or ""): item for item in existing_candidates if item.get("id")}
    candidates: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for key, grouped in _group_capsules_for_candidates(capsules).items():
        total_seen = sum(max(1, int(item.get("success_count") or 0)) for item in grouped)
        if total_seen < 2:
            continue
        candidate = _candidate_from_capsules(
            novel_id,
            chapter_number,
            key,
            grouped,
            reflections,
            existing_by_id.get(_candidate_id("capsules", key)),
            now,
        )
        if candidate["id"] not in existing_by_id:
            candidates.append(candidate)
            events.append(_candidate_event(novel_id, chapter_number, candidate, now))
            existing_by_id[candidate["id"]] = candidate

    for gene in genes:
        failures = int(gene.get("failure_count") or 0)
        positives = int(gene.get("protected_count") or 0) + int(gene.get("helpful_count") or 0)
        if failures < 2 or positives >= failures:
            continue
        candidate = _candidate_from_gene(
            novel_id,
            chapter_number,
            gene,
            reflections,
            existing_by_id.get(_candidate_id("gene", str(gene.get("id") or ""))),
            now,
        )
        if candidate["id"] not in existing_by_id:
            candidates.append(candidate)
            events.append(_candidate_event(novel_id, chapter_number, candidate, now))
            existing_by_id[candidate["id"]] = candidate

    memory_index = build_memory_index(
        novel_id=novel_id,
        genes=genes,
        capsules=capsules,
        reflections=reflections,
        candidates=list(existing_by_id.values()),
        at=now,
    )
    return candidates, memory_index, events


def build_memory_index(
    *,
    novel_id: str,
    genes: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
    reflections: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    at: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "type": "MemoryIndex",
        "schema_version": AGENT_SCHEMA_VERSION,
        "novel_id": novel_id,
        "updated_at": at or _now(),
        "summary": {
            "genes": len(genes),
            "capsules": len(capsules),
            "reflections": len(reflections),
            "gene_candidates": len(candidates),
            "pending_gene_candidates": sum(1 for item in candidates if item.get("status") == "pending_review"),
        },
        "top_gene_ids": [str(item.get("id") or "") for item in sorted(genes, key=lambda item: -_gene_positive_score(item))[:8]],
        "top_capsule_ids": [str(item.get("id") or "") for item in sorted(capsules, key=_capsule_memory_rank, reverse=True)[:8]],
        "latest_reflection_ids": [str(item.get("id") or "") for item in reflections[-8:]],
        "candidate_ids": [str(item.get("id") or "") for item in candidates[-8:]],
        "items": (
            [_memory_item("gene", item) for item in sorted(genes, key=lambda item: -_gene_positive_score(item))[:8]]
            + [_memory_item("capsule", item) for item in sorted(capsules, key=_capsule_memory_rank, reverse=True)[:8]]
            + [_memory_item("reflection", item) for item in reflections[-8:]]
            + [_memory_item("gene_candidate", item) for item in candidates[-8:]]
        )[:32],
    }


def summarize_agent_status(
    *,
    genes: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
    events: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    reflections: Optional[list[dict[str, Any]]] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
    memory_index: Optional[dict[str, Any]] = None,
    host_context_summary: Optional[dict[str, Any]] = None,
    semantic_recall_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    reflections = reflections or []
    candidates = candidates or []
    memory_index = memory_index or {}
    latest_selection = selections[-1] if selections else None
    latest_solidified = [event for event in events if event.get("intent") == "solidify"][-5:]
    latest_learning = [event for event in events if event.get("intent") in {"solidify", "evaluate", "reflect", "candidate"}][-8:]
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "asset_counts": {
            "genes": len(genes),
            "capsules": len(capsules),
            "events": len(events),
            "selections": len(selections),
            "reflections": len(reflections),
            "gene_candidates": len(candidates),
        },
        "memory_layers": {
            "episodic": len(events) + len(selections),
            "semantic": len(capsules),
            "procedural": len(genes) + len(capsules),
            "reflective": len(reflections) + len(candidates),
        },
        "memory_index_summary": memory_index.get("summary") or {},
        "host_context_summary": host_context_summary or {},
        "plotpilot_context_usage": (host_context_summary or {}).get("plotpilot_context_usage") or {},
        "semantic_recall_summary": semantic_recall_summary or {},
        "recent_events": events[-10:],
        "recent_selections": selections[-5:],
        "latest_selection": latest_selection,
        "latest_solidified": latest_solidified,
        "latest_learning": latest_learning,
        "latest_reflections": [_clean_status_asset(item) for item in reflections[-6:]],
        "gene_candidates": [_clean_status_asset(item) for item in candidates[-8:]],
        "top_genes": [_clean_status_asset(item) for item in sorted(
            genes,
            key=lambda item: (
                -(int(item.get("hit_count") or 0)),
                -(int(item.get("positive_score") or 0)),
                -(int(item.get("protected_count") or 0)),
                -(int(item.get("helpful_count") or 0)),
                str(item.get("id") or ""),
            ),
        )[:8]],
        "top_capsules": [_clean_status_asset(item) for item in sorted(
            capsules,
            key=lambda item: (
                -_capsule_memory_rank(item),
                str(item.get("id") or ""),
            ),
        )[:8]],
    }


def _is_solidifiable_issue(issue: dict[str, Any]) -> bool:
    severity = str(issue.get("severity") or "").strip()
    if severity not in {"warning", "critical", "error"}:
        return False
    issue_type = str(issue.get("issue_type") or "")
    if issue_type in SOLIDIFIABLE_ISSUE_TYPES:
        return bool(issue.get("evidence")) or issue_type == "evolution_plot_continuity"
    return issue_type.startswith("evolution_route_") and bool(issue.get("evidence"))


def _signals_for_issue(issue: dict[str, Any]) -> list[str]:
    issue_type = str(issue.get("issue_type") or "")
    signals = ["review_feedback"]
    if issue_type.startswith("evolution_route_"):
        signals.extend(["route_conflict", "location_jump", "chapter_bridge"])
    if "boundary" in issue_type:
        signals.extend(["chapter_bridge", "ending_state", "state_reset"])
    if "cognition" in issue_type or "belief" in issue_type:
        signals.extend(["character_cognition", "knowledge_boundary"])
    if "capability" in issue_type:
        signals.append("capability_boundary")
    if "continuity" in issue_type:
        signals.extend(["chapter_bridge", "state_reset"])
    if "logic" in issue_type:
        signals.extend(["character_cognition", "capability_boundary"])
    if "entity_pollution" in issue_type or "character_pollution" in issue_type:
        signals.extend(["entity_pollution", "character_card_hygiene"])
    if "location_pollution" in issue_type:
        signals.extend(["location_pollution", "route_tracking"])
    if "style_repetition" in issue_type:
        signals.extend(["style_repetition", "repetition_phrase", "phrase_guard"])
    if "palette_missing" in issue_type:
        signals.extend(["personality_palette", "palette_missing", "character_voice"])
    if "palette_drift" in issue_type:
        signals.extend(["personality_palette", "palette_drift", "character_voice"])
    if "role_shift" in issue_type:
        signals.extend(["character_cognition", "character_role_shift"])
    if "bible_context" in issue_type:
        signals.append("bible_context")
    if "worldbuilding" in issue_type:
        signals.append("worldbuilding_context")
    if "knowledge_context" in issue_type:
        signals.append("knowledge_context")
    if "story_knowledge" in issue_type:
        signals.append("story_knowledge_context")
    if "storyline" in issue_type:
        signals.append("storyline_context")
    if "timeline" in issue_type:
        signals.append("timeline_context")
    if "chronicle" in issue_type:
        signals.append("chronicle_context")
    if "foreshadow" in issue_type:
        signals.append("foreshadow_context")
    if "dialogue_voice" in issue_type:
        signals.append("dialogue_voice_context")
    if "triples" in issue_type:
        signals.append("triples_context")
    if "memory_engine" in issue_type:
        signals.append("memory_engine_context")
    return _dedupe(signals)


def _capsule_id(issue: dict[str, Any]) -> str:
    issue_type = str(issue.get("issue_type") or "")
    suggestion = str(issue.get("suggestion") or "")
    evidence = str(issue.get("evidence") or "")
    digest = sha256("|".join([issue_type, suggestion, evidence[:420]]).encode("utf-8")).hexdigest()[:16]
    return f"cap_{digest}"


def _capsule_category(issue: dict[str, Any]) -> str:
    issue_type = str(issue.get("issue_type") or "")
    if issue_type.startswith("evolution_route_"):
        return "route"
    if "boundary" in issue_type:
        return "continuity"
    if "entity_pollution" in issue_type:
        return "entity_hygiene"
    if "location_pollution" in issue_type:
        return "location_hygiene"
    if "style_repetition" in issue_type:
        return "style"
    if "worldbuilding" in issue_type:
        return "worldbuilding"
    if "knowledge_context" in issue_type:
        return "knowledge"
    if "storyline" in issue_type:
        return "storyline"
    if "chronicle" in issue_type:
        return "chronicle"
    if "foreshadow" in issue_type:
        return "foreshadow"
    if "dialogue_voice" in issue_type:
        return "dialogue_voice"
    if "role_shift" in issue_type:
        return "character_logic"
    if "cognition" in issue_type or "belief" in issue_type or "logic" in issue_type:
        return "character_logic"
    if "capability" in issue_type:
        return "capability"
    if "continuity" in issue_type:
        return "continuity"
    return "review"


def _capsule_title(issue: dict[str, Any]) -> str:
    category = _capsule_category(issue)
    labels = {
        "route": "路线冲突经验",
        "character_logic": "角色认知经验",
        "capability": "能力边界经验",
        "continuity": "状态承接经验",
        "entity_hygiene": "人物卡清洗经验",
        "location_hygiene": "地点抽取清洗经验",
        "style": "重复表达控制经验",
        "worldbuilding": "世界观约束经验",
        "knowledge": "知识库约束经验",
        "storyline": "故事线协同经验",
        "chronicle": "编年史承接经验",
        "foreshadow": "伏笔账本协同经验",
        "dialogue_voice": "对话声线经验",
    }
    return labels.get(category, "审查经验")


def _selection_rationale(genes: list[dict[str, Any]], capsules: list[dict[str, Any]]) -> str:
    parts = []
    if genes:
        parts.append("matched genes " + ", ".join(str(item.get("id")) for item in genes))
    if capsules:
        parts.append("reused capsules " + ", ".join(str(item.get("id")) for item in capsules))
    return "; ".join(parts) if parts else "no matching agent assets"


def _capsule_text_matches(capsule: dict[str, Any], text: str) -> bool:
    if not text:
        return False
    haystack = " ".join(str(capsule.get(key) or "") for key in ["title", "summary", "guidance", "category"])
    return any(token and token in text for token in _terms(haystack))


def _capsule_memory_rank(capsule: dict[str, Any]) -> int:
    evidence_quality = min(len(capsule.get("evidence") or []), 4) * 5
    recurrence = int(capsule.get("success_count") or 0) * 6
    positive = int(capsule.get("hit_count") or 0) + int(capsule.get("success_count") or 0) * 2
    failures = int(capsule.get("failure_count") or 0) * 3
    recency = min(int(capsule.get("last_seen_chapter") or capsule.get("chapter_number") or 0), 100)
    return evidence_quality + recurrence + positive + recency - failures


def _gene_positive_score(gene: dict[str, Any]) -> int:
    return (
        int(gene.get("positive_score") or 0)
        + int(gene.get("protected_count") or 0) * 4
        + int(gene.get("helpful_count") or 0) * 3
        + int(gene.get("success_count") or 0)
    )


def _mild_failure_penalty(gene: dict[str, Any]) -> int:
    failures = int(gene.get("failure_count") or 0)
    positives = int(gene.get("protected_count") or 0) + int(gene.get("helpful_count") or 0)
    if failures <= 0:
        return 0
    penalty = min(failures, 3) * 2
    if failures > positives + 2:
        penalty += failures - positives - 2
    return penalty


def _positive_reason(gene: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    title = str(gene.get("title") or gene.get("id") or "Gene")
    if not issues:
        return f"{title} 已选中且本章审查未发现问题，视为一次有效保护。"
    return f"{title} 已选中，且本章未复发对应问题。"


def _improvement_advice(gene: dict[str, Any], failure_reasons: list[str]) -> str:
    title = str(gene.get("title") or gene.get("id") or "Gene")
    reason = "; ".join(item for item in failure_reasons if item).strip()
    if reason:
        return f"{title} 仍需增强：{reason[:180]}"
    return f"{title} 仍需增强对应策略。"


def _reflection_pattern(issues: list[dict[str, Any]], capsules: list[dict[str, Any]]) -> str:
    issue_types = _dedupe([issue.get("issue_type") for issue in issues])
    if issue_types:
        return "、".join(issue_types[:4])
    categories = _dedupe([capsule.get("category") for capsule in capsules])
    return "、".join(categories[:4]) or "未分类审查经验"


def _reflection_root_cause(issues: list[dict[str, Any]]) -> str:
    descriptions = [str(issue.get("description") or "").strip() for issue in issues if str(issue.get("description") or "").strip()]
    if descriptions:
        return "；".join(descriptions[:2])
    return "本轮审查发现可复用问题，需要在下一章上下文中前置约束。"


def _reflection_constraints(capsules: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[str]:
    constraints = [str(capsule.get("guidance") or capsule.get("summary") or "").strip() for capsule in capsules]
    if not constraints:
        constraints = [str(issue.get("suggestion") or issue.get("description") or "").strip() for issue in issues]
    return constraints[:6]


def _reflection_evidence_refs(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for issue in issues:
        evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
        for item in evidence[:2]:
            if isinstance(item, dict):
                refs.append({key: item.get(key) for key in list(item.keys())[:4]})
            else:
                refs.append({"value": str(item)[:180]})
    return refs[:8]


def _should_reflection_suggest_candidate(capsules: list[dict[str, Any]]) -> bool:
    return any(int(capsule.get("success_count") or 0) >= 2 for capsule in capsules)


def _group_capsules_for_candidates(capsules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for capsule in capsules:
        key = str(capsule.get("source_issue_type") or capsule.get("category") or "").strip()
        if not key:
            continue
        grouped.setdefault(key, []).append(capsule)
    return grouped


def _candidate_from_capsules(
    novel_id: str,
    chapter_number: int,
    key: str,
    capsules: list[dict[str, Any]],
    reflections: list[dict[str, Any]],
    previous: Optional[dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    source_ids = [str(item.get("id") or "") for item in capsules if item.get("id")]
    source_reflection_ids = _reflection_ids_for_capsules(reflections, source_ids)
    signals = _dedupe([signal for capsule in capsules for signal in (capsule.get("signals") or [])])
    guidance = _dedupe([capsule.get("guidance") or capsule.get("summary") for capsule in capsules])[:4]
    candidate_id = _candidate_id("capsules", key)
    return {
        "type": "GeneCandidate",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": candidate_id,
        "novel_id": novel_id,
        "status": str((previous or {}).get("status") or "pending_review"),
        "title": _candidate_title_for_key(key),
        "category": str(capsules[0].get("category") or "review") if capsules else "review",
        "signals_match": signals,
        "strategy_draft": guidance or ["把重复出现的审查问题前置为写作约束。"],
        "source": "capsule_consolidation",
        "source_issue_types": _dedupe([capsule.get("source_issue_type") for capsule in capsules]),
        "source_capsule_ids": source_ids[:12],
        "source_reflection_ids": _dedupe(list((previous or {}).get("source_reflection_ids") or []) + source_reflection_ids)[:12],
        "trigger_reason": f"同类问题 {key} 已累计出现 {sum(max(1, int(item.get('success_count') or 0)) for item in capsules)} 次，建议沉淀候选 Gene。",
        "evidence_summary": _dedupe([capsule.get("summary") for capsule in capsules])[:4],
        "created_chapter": int((previous or {}).get("created_chapter") or chapter_number),
        "last_seen_chapter": chapter_number,
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
    }


def _candidate_from_gene(
    novel_id: str,
    chapter_number: int,
    gene: dict[str, Any],
    reflections: list[dict[str, Any]],
    previous: Optional[dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    gene_id = str(gene.get("id") or "")
    candidate_id = _candidate_id("gene", gene_id)
    title = str(gene.get("title") or gene_id or "Gene")
    return {
        "type": "GeneCandidate",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": candidate_id,
        "novel_id": novel_id,
        "status": str((previous or {}).get("status") or "pending_review"),
        "title": f"增强：{title}",
        "category": str(gene.get("category") or "strategy"),
        "signals_match": [str(item) for item in (gene.get("signals_match") or [])],
        "strategy_draft": list(gene.get("strategy") or [])[:3] + [str(gene.get("last_improvement_advice") or "增加更明确的前置约束与审查标准。")],
        "source": "gene_needs_improvement",
        "source_issue_types": [],
        "source_gene_id": gene_id,
        "source_capsule_ids": [],
        "source_reflection_ids": _dedupe(list((previous or {}).get("source_reflection_ids") or []) + _latest_reflection_ids(reflections))[:12],
        "trigger_reason": f"{title} 待改进次数高于正向保护，建议增强候选策略。",
        "evidence_summary": [str(gene.get("last_failure_reason") or gene.get("last_improvement_advice") or "")[:260]],
        "created_chapter": int((previous or {}).get("created_chapter") or chapter_number),
        "last_seen_chapter": chapter_number,
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
    }


def _reflection_ids_for_capsules(reflections: list[dict[str, Any]], capsule_ids: list[str]) -> list[str]:
    wanted = set(capsule_ids)
    result = []
    for reflection in reflections:
        if wanted.intersection(str(item) for item in reflection.get("capsule_ids") or []):
            result.append(str(reflection.get("id") or ""))
    return [item for item in result if item]


def _latest_reflection_ids(reflections: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id") or "") for item in reflections[-4:] if item.get("id")]


def _candidate_title_for_key(key: str) -> str:
    if "route" in key or "boundary" in key:
        return "候选 Gene：章节承接与路线保护"
    if "cognition" in key or "belief" in key:
        return "候选 Gene：角色认知边界增强"
    if "capability" in key:
        return "候选 Gene：能力边界增强"
    if "style_repetition" in key:
        return "候选 Gene：重复表达控制增强"
    if "pollution" in key:
        return "候选 Gene：抽取污染清洗增强"
    return "候选 Gene：审查经验增强"


def _candidate_id(prefix: str, key: str) -> str:
    digest = sha256("|".join([prefix, key]).encode("utf-8")).hexdigest()[:16]
    return f"genc_{digest}"


def _candidate_event(novel_id: str, chapter_number: int, candidate: dict[str, Any], now: str) -> dict[str, Any]:
    return {
        "type": "EvolutionEvent",
        "schema_version": AGENT_SCHEMA_VERSION,
        "id": _event_id("candidate", novel_id, chapter_number, candidate.get("id")),
        "intent": "candidate",
        "hook_name": "after_chapter_review",
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "signals": list(candidate.get("signals_match") or []),
        "genes_used": [],
        "capsule_id": None,
        "outcome": {"status": "pending_review", "candidate_id": candidate.get("id")},
        "meta": {"at": now, "source": candidate.get("source")},
    }


def _memory_item(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or item.get("problem_pattern") or item.get("id") or "")[:120],
        "category": str(item.get("category") or ""),
        "signals": list(item.get("signals_match") or item.get("signals") or [])[:8],
        "score": _gene_positive_score(item) if kind == "gene" else _capsule_memory_rank(item) if kind == "capsule" else 0,
        "chapter_number": item.get("last_seen_chapter") or item.get("chapter_number") or item.get("created_chapter"),
        "summary": str(item.get("guidance") or item.get("summary") or item.get("root_cause") or item.get("trigger_reason") or "")[:220],
    }


def _terms(text: str) -> list[str]:
    candidates = ["C307", "黑塔", "钥匙", "地点", "路线", "承接", "性格", "调色盘", "知道", "能力", "进入", "离开"]
    return [item for item in candidates if item in text]


def _clean_status_asset(asset: dict[str, Any]) -> dict[str, Any]:
    item = dict(asset)
    reason = str(item.get("last_failure_reason") or "")
    if _contains_polluted_status_text(reason):
        item["last_failure_reason"] = ""
        item["last_failure_reason_hidden"] = True
    return item


def _contains_polluted_status_text(text: str) -> bool:
    return any(token in text for token in ("金属牌上一记录", "查询记录", "地点列表检测到疑似半句残片：专门", "近期角色 金属牌", "近期角色 方向"))


def _event_id(prefix: str, *parts: Any) -> str:
    digest = sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"evt_{prefix}_{digest}"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _dedupe(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
