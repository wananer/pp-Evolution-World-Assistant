"""Host integration entry points for PlotPilot plugin effects.

Core PlotPilot code should call this module, not individual plugin modules or raw
hook dispatchers. Plugins remain mounted behind the platform and contribute only
through registered hooks.
"""
from __future__ import annotations

import logging
from typing import Any

from .context_bridge import dispatch_hook_sync, render_context_blocks
from .hook_dispatcher import dispatch_hook

logger = logging.getLogger(__name__)


def build_generation_context_patch(
    novel_id: str,
    chapter_number: int,
    outline: str,
    *,
    source: str = "context_budget_allocator",
    max_chars: int = 6000,
) -> str:
    """Return plugin-provided context text for prompt assembly."""
    try:
        results = dispatch_hook_sync(
            "before_context_build",
            {
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "trigger_type": source,
                "source": source,
                "payload": {"outline": outline},
            },
        )
        return render_context_blocks(results, max_chars=max_chars)
    except Exception as exc:
        logger.warning("Plugin context patch failed novel=%s ch=%s: %s", novel_id, chapter_number, exc)
        return ""


async def notify_chapter_committed(
    novel_id: str,
    chapter_number: int,
    content: str,
    *,
    source: str = "chapter_aftermath_pipeline",
) -> list[dict[str, Any]]:
    """Notify plugins that a chapter has been committed/saved."""
    try:
        return await dispatch_hook(
            "after_commit",
            {
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "trigger_type": source,
                "source": source,
                "payload": {"content": content},
            },
        )
    except Exception as exc:
        logger.warning("Plugin after_commit failed novel=%s ch=%s: %s", novel_id, chapter_number, exc)
        return [{"plugin_name": "plugin_platform", "hook_name": "after_commit", "ok": False, "error": str(exc)}]


async def review_chapter_with_plugins(
    novel_id: str,
    chapter_number: int,
    content: str,
    *,
    source: str = "chapter_review_service",
) -> list[dict[str, Any]]:
    """Ask plugins to contribute chapter review issues/suggestions."""
    try:
        return await dispatch_hook(
            "review_chapter",
            {
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "trigger_type": source,
                "source": source,
                "payload": {"content": content},
            },
        )
    except Exception as exc:
        logger.warning("Plugin review_chapter failed novel=%s ch=%s: %s", novel_id, chapter_number, exc)
        return [{"plugin_name": "plugin_platform", "hook_name": "review_chapter", "ok": False, "error": str(exc)}]
