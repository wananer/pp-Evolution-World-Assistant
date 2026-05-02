"""Shared runtime support for PlotPilot plugins."""
from __future__ import annotations

from .context_bridge import dispatch_hook_sync, render_context_blocks
from .hook_dispatcher import clear_hooks, dispatch_hook, dispatch_hook_sync_best_effort, list_hooks, register_hook
from .host_integration import (
    build_generation_context_patch,
    collect_chapter_review_context_with_plugins,
    notify_chapter_committed,
    notify_chapter_review_completed,
    review_chapter_with_plugins,
)
from .host_database import ReadOnlyHostDatabase, create_default_readonly_host_database
from .host_facade import PlotPilotPluginHost
from .job_registry import PluginJobRecord, PluginJobRegistry
from .plugin_storage import PluginStorage, default_plugin_storage_root
from .runtime_types import PluginHookPayload, PluginHookResult

__all__ = [
    "PlotPilotPluginHost",
    "PluginHookPayload",
    "PluginHookResult",
    "PluginJobRecord",
    "PluginJobRegistry",
    "PluginStorage",
    "ReadOnlyHostDatabase",
    "build_generation_context_patch",
    "clear_hooks",
    "collect_chapter_review_context_with_plugins",
    "create_default_readonly_host_database",
    "default_plugin_storage_root",
    "dispatch_hook",
    "dispatch_hook_sync",
    "dispatch_hook_sync_best_effort",
    "list_hooks",
    "register_hook",
    "notify_chapter_committed",
    "notify_chapter_review_completed",
    "render_context_blocks",
    "review_chapter_with_plugins",
]
