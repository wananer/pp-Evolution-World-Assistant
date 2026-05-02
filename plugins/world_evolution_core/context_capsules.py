"""Context capsule helpers for Evolution prompt injection."""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


HARD_CONTEXT_KINDS = {"chapter_state_bridge", "continuity_risk"}
STABLE_CONTEXT_KINDS = {"usage_protocol"}


def canonicalize(value: Any) -> str:
    """Return deterministic JSON for hashing context payloads."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return "sha256:" + sha256(canonicalize(value).encode("utf-8")).hexdigest()


def enrich_blocks_with_capsules(
    blocks: list[dict[str, Any]],
    *,
    novel_id: str,
    chapter_number: int | None,
    previous_records: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach capsule metadata and remove redundant context blocks.

    The hard handoff/risk blocks are never removed by historical de-dupe because
    they protect chapter continuity. Stable protocol blocks may be skipped once
    the same content has already been injected for this novel.
    """
    previous_hashes = _previous_hashes(previous_records or [])
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_semantic: dict[str, int] = {}

    for block in blocks:
        enriched = _enrich_block(block, novel_id=novel_id, chapter_number=chapter_number)
        block_hash = str(enriched["content_hash"])
        semantic_key = str(enriched["semantic_key"])
        kind = str(enriched.get("kind") or "")

        if block_hash in seen_hashes:
            skipped.append(_skip_record(enriched, "duplicate_content_in_patch"))
            continue

        existing_index = seen_semantic.get(semantic_key)
        if existing_index is not None:
            existing = selected[existing_index]
            if _should_replace(existing, enriched):
                skipped.append(_skip_record(existing, "replaced_by_higher_priority_semantic_duplicate"))
                selected[existing_index] = enriched
            else:
                skipped.append(_skip_record(enriched, "semantic_duplicate_in_patch"))
            seen_hashes.add(block_hash)
            continue

        if kind in STABLE_CONTEXT_KINDS and block_hash in previous_hashes:
            skipped.append(_skip_record(enriched, "stable_protocol_already_injected"))
            continue

        seen_semantic[semantic_key] = len(selected)
        seen_hashes.add(block_hash)
        selected.append(enriched)

    return selected, skipped


def build_injection_record(
    *,
    novel_id: str,
    chapter_number: int | None,
    blocks: list[dict[str, Any]],
    skipped_blocks: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "at": at,
        "selected": [_audit_item(block) for block in blocks],
        "skipped": skipped_blocks,
        "selected_count": len(blocks),
        "skipped_count": len(skipped_blocks),
        "estimated_token_budget": sum(int(block.get("token_budget") or 0) for block in blocks),
    }


def _enrich_block(block: dict[str, Any], *, novel_id: str, chapter_number: int | None) -> dict[str, Any]:
    enriched = dict(block)
    payload = {
        "kind": enriched.get("kind"),
        "title": enriched.get("title"),
        "content": enriched.get("content"),
        "items": enriched.get("items"),
    }
    block_hash = content_hash(payload)
    semantic_key = _semantic_key(enriched)
    enriched["semantic_key"] = semantic_key
    enriched["content_hash"] = block_hash
    enriched["capsule_id"] = f"cap_{_slug(novel_id)}_{_slug(semantic_key)}_{block_hash.split(':', 1)[1][:12]}"
    enriched["capsule"] = {
        "schema_version": 1,
        "capsule_id": enriched["capsule_id"],
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "kind": enriched.get("kind"),
        "tier": _block_tier(enriched),
        "semantic_key": semantic_key,
        "content_hash": block_hash,
        "priority": int(enriched.get("priority") or 0),
        "scope": _scope_for_kind(str(enriched.get("kind") or "")),
    }
    return enriched


def _semantic_key(block: dict[str, Any]) -> str:
    kind = str(block.get("kind") or block.get("id") or "context")
    block_id = str(block.get("id") or kind)
    items = block.get("items")
    if kind == "chapter_state_bridge" and isinstance(items, dict):
        chapters = items.get("chapters") if isinstance(items.get("chapters"), list) else []
        latest = chapters[-1] if chapters else {}
        chapter_number = latest.get("chapter_number") if isinstance(latest, dict) else None
        return f"chapter_handoff:{chapter_number or 'latest'}"
    if kind == "focus_character_state" and isinstance(items, list):
        names = ",".join(str(item.get("name")) for item in items if isinstance(item, dict) and item.get("name"))
        return f"character_state:{names or block_id}"
    if kind == "chapter_facts" and isinstance(items, list):
        chapters = ",".join(str(item.get("chapter_number")) for item in items if isinstance(item, dict) and item.get("chapter_number"))
        return f"chapter_facts:{chapters or block_id}"
    return f"{kind}:{block_id}"


def _scope_for_kind(kind: str) -> str:
    if kind == "chapter_state_bridge":
        return "chapter"
    if kind in {"focus_character_state", "background_character_constraint"}:
        return "character"
    if kind == "usage_protocol":
        return "novel"
    return "arc"


def _previous_hashes(records: list[dict[str, Any]]) -> set[str]:
    hashes: set[str] = set()
    for record in records:
        for item in record.get("selected") or []:
            if isinstance(item, dict) and item.get("content_hash"):
                hashes.add(str(item.get("content_hash")))
    return hashes


def _should_replace(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    existing_kind = str(existing.get("kind") or "")
    incoming_kind = str(incoming.get("kind") or "")
    if existing_kind in HARD_CONTEXT_KINDS and incoming_kind not in HARD_CONTEXT_KINDS:
        return False
    if incoming_kind in HARD_CONTEXT_KINDS and existing_kind not in HARD_CONTEXT_KINDS:
        return True
    return int(incoming.get("priority") or 0) > int(existing.get("priority") or 0)


def _skip_record(block: dict[str, Any], reason: str) -> dict[str, Any]:
    item = _audit_item(block)
    item["reason"] = reason
    return item


def _audit_item(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": block.get("id"),
        "kind": block.get("kind"),
        "tier": _block_tier(block),
        "title": block.get("title"),
        "semantic_key": block.get("semantic_key"),
        "content_hash": block.get("content_hash"),
        "capsule_id": block.get("capsule_id"),
        "priority": block.get("priority"),
        "token_budget": block.get("token_budget"),
        "content_chars": len(str(block.get("content") or "")),
    }


def _block_tier(block: dict[str, Any]) -> str:
    tier = str(block.get("tier") or "").strip()
    if tier:
        return tier
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return str(metadata.get("tier") or "").strip()


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or ""))[:80].strip("_") or "context"
