"""Runtime contracts shared by PlotPilot plugin platform components."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

PluginHookName = Literal[
    "before_context_build",
    "before_generation",
    "after_generation",
    "after_commit",
    "manual_rebuild",
    "rollback",
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


class PluginHookResult(TypedDict):
    plugin_name: str
    hook_name: str
    ok: bool
    skipped: NotRequired[bool]
    reason: NotRequired[str]
    context_blocks: NotRequired[list[PluginContextBlock]]
    data: NotRequired[dict[str, Any]]
    error: NotRequired[str]
