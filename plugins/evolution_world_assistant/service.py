"""Minimal PlotPilot-side workflow service for Evolution World Assistant."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from plugins.platform.job_registry import PluginJobRecord, PluginJobRegistry
from plugins.platform.plugin_storage import PluginStorage

PLUGIN_NAME = "evolution_world_assistant"


class EvolutionWorldAssistantService:
    def __init__(self, storage: PluginStorage | None = None, jobs: PluginJobRegistry | None = None) -> None:
        self.storage = storage or PluginStorage()
        self.jobs = jobs or PluginJobRegistry(self.storage)

    async def after_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        content = _extract_content(payload)
        if not novel_id or not chapter_number or not content:
            return {"ok": True, "skipped": True, "reason": "missing novel_id/chapter_number/content"}

        content_hash = str(payload.get("content_hash") or _hash_text(content))
        dedup_key = self.jobs.build_dedup_key(
            PLUGIN_NAME,
            "after_commit",
            novel_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            trigger_type=str(payload.get("trigger_type") or "auto"),
        )
        facts = self._extract_minimal_facts(novel_id, chapter_number, content, content_hash)
        self.storage.write_json(
            PLUGIN_NAME,
            ["novels", novel_id, "facts", f"chapter_{chapter_number}.json"],
            facts,
        )
        self.storage.append_jsonl(
            PLUGIN_NAME,
            ["novels", novel_id, "events.jsonl"],
            {"type": "chapter_committed", "chapter_number": chapter_number, "content_hash": content_hash, "at": _now()},
        )
        self.jobs.append(
            PluginJobRecord(
                plugin_name=PLUGIN_NAME,
                hook_name="after_commit",
                novel_id=novel_id,
                chapter_number=chapter_number,
                trigger_type=str(payload.get("trigger_type") or "auto"),
                dedup_key=dedup_key,
                content_hash=content_hash,
                status="succeeded",
                input_json={"chapter_number": chapter_number},
                output_json={"facts_path": f"facts/chapter_{chapter_number}.json"},
            )
        )
        return {"ok": True, "data": facts}

    async def before_context_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id:
            return {"ok": True, "skipped": True, "reason": "missing novel_id"}

        summary = self._build_context_summary(novel_id, chapter_number)
        if not summary:
            return {"ok": True, "skipped": True, "reason": "no evolution state yet"}

        return {
            "ok": True,
            "context_blocks": [
                {
                    "plugin_name": PLUGIN_NAME,
                    "title": "Evolution World State",
                    "content": summary,
                    "priority": 60,
                    "token_budget": 1200,
                    "metadata": {"novel_id": novel_id, "chapter_number": chapter_number},
                }
            ],
        }

    async def manual_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        if not novel_id:
            return {"ok": False, "error": "missing novel_id"}
        return {"ok": True, "skipped": True, "reason": "manual rebuild is reserved for Phase 2", "data": {"novel_id": novel_id}}

    async def rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        novel_id = str(payload.get("novel_id") or "").strip()
        chapter_number = _int_or_none(payload.get("chapter_number"))
        if not novel_id or not chapter_number:
            return {"ok": False, "error": "missing novel_id/chapter_number"}
        return {"ok": True, "skipped": True, "reason": "rollback is reserved for Phase 2", "data": {"novel_id": novel_id, "chapter_number": chapter_number}}

    def _extract_minimal_facts(self, novel_id: str, chapter_number: int, content: str, content_hash: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "content_hash": content_hash,
            "source": "after_commit",
            "at": _now(),
            "facts": {
                "summary": content.strip()[:500],
                "characters": [],
                "locations": [],
                "world_events": [],
            },
        }

    def _build_context_summary(self, novel_id: str, chapter_number: int | None) -> str:
        facts_root = self.storage.root / PLUGIN_NAME / "novels" / novel_id / "facts"
        if not facts_root.exists():
            return ""
        facts = []
        for path in sorted(facts_root.glob("chapter_*.json")):
            data = self.storage.read_json(PLUGIN_NAME, ["novels", novel_id, "facts", path.name], default={})
            source_chapter = _int_or_none(data.get("chapter_number"))
            if chapter_number and source_chapter and source_chapter >= chapter_number:
                continue
            summary = ((data.get("facts") or {}).get("summary") or "").strip()
            if summary:
                facts.append(f"- 第{source_chapter}章：{summary}")
        return "\n".join(facts[-5:])


def _extract_content(payload: dict[str, Any]) -> str:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return str(payload.get("content") or nested.get("content") or nested.get("chapter_content") or "").strip()


def _hash_text(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()[:16]


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
