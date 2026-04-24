"""In-process hook registry for zero-intrusion PlotPilot plugins."""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from .runtime_types import PluginHookPayload, PluginHookResult

logger = logging.getLogger(__name__)

HookHandler = Callable[[PluginHookPayload], PluginHookResult | Awaitable[PluginHookResult] | dict[str, Any] | Awaitable[dict[str, Any]] | None]
_HOOKS: dict[str, list[tuple[str, HookHandler]]] = defaultdict(list)


def register_hook(plugin_name: str, hook_name: str, handler: HookHandler) -> None:
    if not plugin_name or not hook_name:
        raise ValueError("plugin_name and hook_name are required")
    if not callable(handler):
        raise TypeError("hook handler must be callable")
    existing = _HOOKS[hook_name]
    if any(name == plugin_name and registered is handler for name, registered in existing):
        return
    existing.append((plugin_name, handler))


def clear_hooks() -> None:
    _HOOKS.clear()


def list_hooks() -> dict[str, list[str]]:
    return {hook_name: [plugin_name for plugin_name, _ in handlers] for hook_name, handlers in _HOOKS.items()}


async def dispatch_hook(hook_name: str, payload: PluginHookPayload | None = None) -> list[PluginHookResult]:
    results: list[PluginHookResult] = []
    for plugin_name, handler in list(_HOOKS.get(hook_name, [])):
        hook_payload: PluginHookPayload = {**(payload or {}), "plugin_name": plugin_name}
        try:
            raw_result = handler(hook_payload)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            results.append(_normalize_result(plugin_name, hook_name, raw_result))
        except Exception as exc:  # pragma: no cover - defensive logging path
            logger.exception("Plugin hook failed: %s.%s", plugin_name, hook_name)
            results.append(
                {
                    "plugin_name": plugin_name,
                    "hook_name": hook_name,
                    "ok": False,
                    "error": str(exc),
                }
            )
    return results


def _normalize_result(plugin_name: str, hook_name: str, raw_result: Any) -> PluginHookResult:
    if raw_result is None:
        return {"plugin_name": plugin_name, "hook_name": hook_name, "ok": True}
    if not isinstance(raw_result, dict):
        return {
            "plugin_name": plugin_name,
            "hook_name": hook_name,
            "ok": False,
            "error": "hook result must be a dict or None",
        }
    return {
        "plugin_name": str(raw_result.get("plugin_name") or plugin_name),
        "hook_name": str(raw_result.get("hook_name") or hook_name),
        "ok": bool(raw_result.get("ok", True)),
        **{key: value for key, value in raw_result.items() if key not in {"plugin_name", "hook_name", "ok"}},
    }
