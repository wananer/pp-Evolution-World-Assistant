"""Stable host facade for PlotPilot plugins.

The facade intentionally exposes a small, stable surface so stateful plugins do
not import deep host internals directly. Host applications can pass adapters for
novel/chapter readers, LLM calls, and event emitters as the platform matures.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional, Union, Tuple

from .host_database import ReadOnlyHostDatabase, create_default_readonly_host_database
from .hook_dispatcher import dispatch_hook
from .plugin_storage import PluginStorage
from .runtime_types import PluginHookPayload, PluginHookResult

Reader = Callable[..., Any]
AsyncReader = Callable[..., Awaitable[Any]]


class PlotPilotPluginHost:
    def __init__(
        self,
        *,
        storage: Optional[PluginStorage] = None,
        novel_reader: Optional[Union[Reader, AsyncReader]] = None,
        chapter_reader: Optional[Union[Reader, AsyncReader]] = None,
        chapter_lister: Optional[Union[Reader, AsyncReader]] = None,
        llm_caller: Optional[Union[Reader, AsyncReader]] = None,
        event_emitter: Optional[Union[Reader, AsyncReader]] = None,
        host_database: Optional[ReadOnlyHostDatabase] = None,
    ) -> None:
        self.storage = storage or PluginStorage()
        self.host_database = host_database or create_default_readonly_host_database()
        self._novel_reader = novel_reader
        self._chapter_reader = chapter_reader
        self._chapter_lister = chapter_lister
        self._llm_caller = llm_caller
        self._event_emitter = event_emitter

    async def get_novel(self, novel_id: str) -> Any:
        if self._novel_reader is None:
            raise RuntimeError("novel_reader is not configured")
        return await _maybe_await(self._novel_reader(novel_id))

    async def get_chapter(self, novel_id: str, chapter_number: int) -> Any:
        if self._chapter_reader is None:
            raise RuntimeError("chapter_reader is not configured")
        return await _maybe_await(self._chapter_reader(novel_id, chapter_number))

    async def list_chapters(self, novel_id: str) -> Any:
        if self._chapter_lister is None:
            raise RuntimeError("chapter_lister is not configured")
        return await _maybe_await(self._chapter_lister(novel_id))

    async def call_llm(self, request: dict[str, Any]) -> Any:
        if self._llm_caller is None:
            raise RuntimeError("llm_caller is not configured")
        return await _maybe_await(self._llm_caller(request))

    async def emit_event(self, name: str, payload: Optional[dict[str, Any]] = None) -> Any:
        if self._event_emitter is None:
            return None
        return await _maybe_await(self._event_emitter(name, payload or {}))

    async def dispatch_hook(self, hook_name: str, payload: Optional[PluginHookPayload] = None) -> list[PluginHookResult]:
        return await dispatch_hook(hook_name, payload or {})

    def read_host_rows(self, sql: str, params: tuple[Any, ...] = (), *, limit: int | None = None) -> list[dict[str, Any]]:
        if self.host_database is None:
            raise RuntimeError("host_database is not configured")
        return self.host_database.fetch_all(sql, params, limit=limit)

    def read_host_row(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if self.host_database is None:
            raise RuntimeError("host_database is not configured")
        return self.host_database.fetch_one(sql, params)

    def read_plugin_state(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], default: Any = None) -> Any:
        return self.storage.read_json(plugin_name, scope, default=default)

    def write_plugin_state(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: Any) -> Any:
        return self.storage.write_json(plugin_name, scope, value)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
