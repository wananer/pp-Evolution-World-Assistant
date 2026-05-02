"""Runtime contracts shared by PlotPilot plugin platform components."""
from __future__ import annotations

from typing import Any, Literal, TypedDict

PluginHookName = Literal[
    "before_context_build",
    "before_generation",
    "after_generation",
    "after_commit",
    "manual_rebuild",
    "rollback",
    "after_novel_created",
    "before_story_planning",
    "before_chapter_review",
    "review_chapter",
    "after_chapter_review",
]


class PluginHookPayload(TypedDict, total=False):
    plugin_name: str
    novel_id: str
    chapter_id: str
    chapter_number: int
    request_id: str
    trigger_type: str
    source: str
    content_hash: str
    payload: dict[str, Any]
    at: str


class PluginContextBlock(TypedDict, total=False):
    plugin_name: str
    title: str
    content: str
    priority: int
    token_budget: int
    metadata: dict[str, Any]


class PluginHookResult(TypedDict, total=False):
    plugin_name: str
    hook_name: str
    ok: bool
    skipped: bool
    reason: str
    context_blocks: list[PluginContextBlock]
    data: dict[str, Any]
    error: str
