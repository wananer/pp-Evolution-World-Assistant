"""Shared runtime support for PlotPilot plugins."""
from __future__ import annotations

from .hook_dispatcher import clear_hooks, dispatch_hook, list_hooks, register_hook
from .job_registry import PluginJobRecord, PluginJobRegistry
from .plugin_storage import PluginStorage
from .runtime_types import PluginHookPayload, PluginHookResult

__all__ = [
    "PluginHookPayload",
    "PluginHookResult",
    "PluginJobRecord",
    "PluginJobRegistry",
    "PluginStorage",
    "clear_hooks",
    "dispatch_hook",
    "list_hooks",
    "register_hook",
]
