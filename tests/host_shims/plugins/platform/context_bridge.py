"""Helpers that adapt plugin hook results into PlotPilot context/runtime flows."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

from .hook_dispatcher import dispatch_hook, dispatch_hook_sync_best_effort

logger = logging.getLogger(__name__)


def dispatch_hook_sync(hook_name: str, payload: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Best-effort synchronous bridge for sync context builders.

    Most PlotPilot context construction code is synchronous. Plugin hooks are async-capable,
    so this helper only runs them when no event loop is already active. Async workflows can
    call ``dispatch_hook`` directly; sync code gets a safe no-op inside an active loop instead
    of risking nested-loop crashes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(dispatch_hook(hook_name, payload or {}))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    return dispatch_hook_sync_best_effort(hook_name, payload or {})


def render_context_blocks(results: Iterable[dict[str, Any]], *, max_chars: int = 6000) -> str:
    """Render successful plugin context blocks into a compact prompt section."""
    sections: list[str] = []
    for result in results:
        if not result.get("ok", True) or result.get("skipped"):
            continue
        blocks = result.get("context_blocks") or []
        for block in blocks:
            content = str(block.get("content") or "").strip()
            if not content:
                continue
            title = str(block.get("title") or result.get("plugin_name") or "插件上下文").strip()
            sections.append(f"【{title}】\n{content}")
    rendered = "\n\n".join(sections).strip()
    if max_chars > 0 and len(rendered) > max_chars:
        return rendered[:max_chars] + "..."
    return rendered
