"""Agent-first orchestration helpers for Evolution World.

This module keeps the LLM-facing contract small and structured. The service
owns model resolution/audit; the orchestrator normalizes decisions, renders
T0/T1 blocks, and applies self-evolution gene patches.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable, Optional

from .context_patch import TIER_T0, TIER_T1


ORCHESTRATOR_SCHEMA_VERSION = 1
AgentRunner = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class AgentOrchestrator:
    def __init__(self, *, run_agent: AgentRunner) -> None:
        self.run_agent = run_agent

    def decide_planning(
        self,
        *,
        novel_id: str,
        purpose: str,
        planning_payload: dict[str, Any],
        fallback_content: str,
        evidence_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "novel_id": novel_id,
            "purpose": purpose,
            "planning_payload": _compact(planning_payload, 2200),
            "fallback_content": fallback_content[:3000],
            "evidence_refs": evidence_refs[:12],
        }
        decision = self._call_agent("agent_before_story_planning", payload, fallback_intent="planning_lock")
        if not decision.get("t0_constraints"):
            decision["t0_constraints"] = [fallback_content[:1200]] if fallback_content else []
        return decision

    def decide_context(
        self,
        *,
        novel_id: str,
        chapter_number: int | None,
        outline: str,
        patch_summary: str,
        knowledge: dict[str, Any],
        tier_summary: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "outline": outline,
            "knowledge_items": _knowledge_prompt_items(knowledge),
            "fallback_patch_summary": patch_summary[:3600],
            "tier_summary": tier_summary,
        }
        decision = self._call_agent("agent_before_context_build", payload, fallback_intent="context_control")
        if not decision.get("t0_constraints") and not decision.get("t1_strategy"):
            decision["t0_constraints"] = [patch_summary[:1400]] if patch_summary else []
            decision["t1_strategy"] = ["Agent 未返回可用控制卡，使用 Evolution 确定性短策略兜底。"]
        return decision

    def observe_after_commit(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        chapter_summary: dict[str, Any],
        native_after_commit: dict[str, Any],
        knowledge: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "chapter_summary": _compact(chapter_summary, 1800),
            "native_after_commit": native_after_commit,
            "knowledge_items": _knowledge_prompt_items(knowledge, limit=6),
        }
        return self._call_agent("agent_after_commit", payload, fallback_intent="observe")

    def decide_review(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        deterministic_issues: list[dict[str, Any]],
        evidence: dict[str, Any],
        knowledge: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "deterministic_issues": _compact(deterministic_issues, 2600),
            "evidence": _compact(evidence, 2600),
            "knowledge_items": _knowledge_prompt_items(knowledge, limit=8),
        }
        return self._call_agent("agent_review_chapter", payload, fallback_intent="review")

    def decide_reflection(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        issues: list[dict[str, Any]],
        capsules: list[dict[str, Any]],
        active_genes: list[dict[str, Any]],
        knowledge: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "issues": _compact(issues, 2600),
            "capsules": _compact(capsules, 2400),
            "active_genes": _compact(active_genes, 2400),
            "knowledge_items": _knowledge_prompt_items(knowledge, limit=8),
        }
        decision = self._call_agent("agent_after_chapter_review", payload, fallback_intent="reflect")
        if not isinstance(decision.get("gene_patches"), list):
            decision["gene_patches"] = []
        return decision

    def _call_agent(self, phase: str, payload: dict[str, Any], *, fallback_intent: str) -> dict[str, Any]:
        result = self.run_agent(phase, _build_decision_prompt(phase, payload), payload)
        decision = normalize_agent_decision(result.get("structured") if isinstance(result, dict) else {}, fallback_intent=fallback_intent)
        rejected = str(decision.get("degraded_reason") or "") == "agent_decision_rejected_sensitive_content"
        ok = bool((result or {}).get("ok")) and not rejected
        decision["agent_result"] = {
            "ok": ok,
            "status": "agent_api" if ok else "degraded",
            "error": str((result or {}).get("error") or decision.get("degraded_reason") or "")[:300],
            "token_usage": (result or {}).get("token_usage") or {},
            "model": str((result or {}).get("model") or ""),
        }
        if not ok:
            decision["degraded_reason"] = decision.get("degraded_reason") or decision["agent_result"]["error"] or "agent_api_unavailable"
        return decision


def normalize_agent_decision(value: Any, *, fallback_intent: str) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    if _contains_sensitive_payload(data):
        return {
            "schema_version": ORCHESTRATOR_SCHEMA_VERSION,
            "intent": fallback_intent,
            "evidence_refs": [],
            "t0_constraints": [],
            "t1_strategy": [],
            "actions": [],
            "issues": [],
            "gene_patches": [],
            "degraded_reason": "agent_decision_rejected_sensitive_content",
        }
    return {
        "schema_version": ORCHESTRATOR_SCHEMA_VERSION,
        "intent": str(data.get("intent") or fallback_intent)[:80],
        "evidence_refs": _list_of_dicts(data.get("evidence_refs"), limit=12),
        "t0_constraints": _list_of_strings(data.get("t0_constraints"), limit=8, item_limit=360),
        "t1_strategy": _list_of_strings(data.get("t1_strategy"), limit=8, item_limit=360),
        "actions": _list_of_dicts(data.get("actions"), limit=12),
        "issues": _list_of_dicts(data.get("issues"), limit=12),
        "gene_patches": _list_of_dicts(data.get("gene_patches"), limit=8),
        "degraded_reason": str(data.get("degraded_reason") or "")[:300],
    }


def decision_to_context_blocks(decision: dict[str, Any], *, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    t0 = "\n".join(f"- {item}" for item in decision.get("t0_constraints") or [] if str(item).strip()).strip()
    t1 = "\n".join(f"- {item}" for item in decision.get("t1_strategy") or [] if str(item).strip()).strip()
    base_meta = {
        **metadata,
        "agent_orchestrated": True,
        "agent_intent": decision.get("intent"),
        "agent_degraded_reason": decision.get("degraded_reason") or "",
        "evidence_refs": decision.get("evidence_refs") or [],
    }
    if t0:
        blocks.append(
            {
                "plugin_name": "world_evolution_core",
                "id": "agent_orchestrated_t0",
                "kind": "hard_constraint",
                "tier": TIER_T0,
                "title": "Evolution Agent T0 硬约束",
                "content": t0,
                "priority": 90,
                "token_budget": 900,
                "metadata": {**base_meta, "tier": TIER_T0, "injection_layer": "t0_hard_constraints"},
            }
        )
    if t1:
        blocks.append(
            {
                "plugin_name": "world_evolution_core",
                "id": "agent_orchestrated_t1",
                "kind": "agent_strategy",
                "tier": TIER_T1,
                "title": "Evolution Agent T1 软策略",
                "content": t1,
                "priority": 70,
                "token_budget": 700,
                "metadata": {**base_meta, "tier": TIER_T1, "injection_layer": "t1_soft_strategy"},
            }
        )
    return blocks


def apply_gene_patches(
    *,
    novel_id: str,
    chapter_number: int,
    genes: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = at or _now()
    by_id = {str(gene.get("id") or ""): dict(gene) for gene in genes if gene.get("id")}
    versions: list[dict[str, Any]] = []
    for patch in patches:
        gene_id = _safe_gene_id(str(patch.get("gene_id") or patch.get("id") or ""))
        strategy = _list_of_strings(patch.get("strategy") or patch.get("strategy_draft"), limit=8, item_limit=260)
        if not gene_id or not strategy:
            continue
        previous = by_id.get(gene_id) or {}
        version = int(previous.get("version") or 0) + 1
        updated = {
            **previous,
            "type": "Gene",
            "schema_version": 1,
            "id": gene_id,
            "title": str(patch.get("title") or previous.get("title") or gene_id)[:120],
            "category": str(patch.get("category") or previous.get("category") or "agent_evolved")[:80],
            "signals_match": _list_of_strings(patch.get("signals_match") or previous.get("signals_match"), limit=12, item_limit=80) or ["agent_evolved"],
            "strategy": strategy,
            "priority": _clamp_int(patch.get("priority"), 1, 100, int(previous.get("priority") or 70)),
            "version": version,
            "status": "active",
            "created_by_agent": True,
            "source_ref": {"chapter_number": chapter_number, "reason": str(patch.get("reason") or "")[:220]},
            "updated_at": now,
            "created_at": previous.get("created_at") or now,
        }
        by_id[gene_id] = updated
        versions.append(
            {
                "type": "GeneVersion",
                "schema_version": 1,
                "gene_id": gene_id,
                "novel_id": novel_id,
                "version": version,
                "status": "active",
                "strategy": strategy,
                "source_ref": updated["source_ref"],
                "previous_hash": _hash_json(previous) if previous else "",
                "created_by_agent": True,
                "chapter_number": chapter_number,
                "created_at": now,
            }
        )
    return sorted(by_id.values(), key=lambda item: (-int(item.get("priority") or 0), str(item.get("id") or ""))), versions


def _build_decision_prompt(phase: str, payload: dict[str, Any]) -> str:
    return (
        "你是 Evolution Agent Orchestrator，正在完全接管 Evolution 插件决策。"
        "你不能写正文，不能改 PlotPilot 主库，只能输出结构化 JSON 决策。\n"
        "必须返回 JSON 对象，字段：intent, evidence_refs, t0_constraints, t1_strategy, actions, issues, gene_patches, degraded_reason。\n"
        "t0_constraints 是必须遵守的硬约束；t1_strategy 是建议参考的软策略。"
        "gene_patches 会立即生效，只有在确有证据时输出。\n\n"
        f"【phase】{phase}\n"
        f"【payload】\n{json.dumps(payload, ensure_ascii=False, default=str)[:9000]}"
    )


def _knowledge_prompt_items(knowledge: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    items = []
    for item in (knowledge or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "chunk_id": item.get("chunk_id"),
                "source_type": item.get("source_type"),
                "chapter_number": item.get("chapter_number"),
                "title": item.get("title"),
                "text": str(item.get("text") or "")[:420],
                "score": item.get("score"),
                "source_refs": item.get("source_refs") or [],
            }
        )
        if len(items) >= limit:
            break
    return items


def _compact(value: Any, limit: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated_json": text[:limit]}


def _list_of_strings(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text[:item_limit])
        if len(result) >= limit:
            break
    return result


def _list_of_dicts(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(_redact_dict(item))
        if len(result) >= limit:
            break
    return result


def _redact_dict(value: dict[str, Any]) -> dict[str, Any]:
    redacted = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if "key" in lowered or "secret" in lowered or "token" in lowered:
            redacted[key] = "[redacted]"
        else:
            redacted[key] = item
    return redacted


def _contains_sensitive_payload(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str).lower()
    markers = ("sk-", "private key", "api2-secret", "agent-secret", "api_key", "authorization", "bearer ")
    return any(marker in text for marker in markers)


def _safe_gene_id(value: str) -> str:
    if not value:
        return ""
    text = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)
    return text[:100].strip("_")


def _hash_json(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _clamp_int(value: Any, lower: int, upper: int, default: int) -> int:
    try:
        return max(lower, min(upper, int(value)))
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
