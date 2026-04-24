"""Minimal file-backed plugin job registry with dedup support."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from .plugin_storage import PluginStorage

JobStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PluginJobRecord:
    plugin_name: str
    hook_name: str
    novel_id: str
    trigger_type: str
    dedup_key: str
    id: str = field(default_factory=lambda: str(uuid4()))
    chapter_id: Optional[str] = None
    chapter_number: Optional[int] = None
    request_id: Optional[str] = None
    content_hash: Optional[str] = None
    status: JobStatus = "pending"
    input_json: dict[str, Any] = field(default_factory=dict)
    output_json: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PluginJobRegistry:
    def __init__(self, storage: Optional[PluginStorage] = None) -> None:
        self.storage = storage or PluginStorage()

    def append(self, record: PluginJobRecord) -> None:
        self.storage.append_jsonl(record.plugin_name, ["jobs.jsonl"], record.to_dict())

    def build_dedup_key(
        self,
        plugin_name: str,
        hook_name: str,
        novel_id: str,
        chapter_number: Optional[int] = None,
        content_hash: Optional[str] = None,
        trigger_type: str = "auto",
    ) -> str:
        parts = [plugin_name, hook_name, novel_id, str(chapter_number or ""), content_hash or "", trigger_type]
        return ":".join(parts)
