"""Stable host facade for PlotPilot plugins.

The facade intentionally exposes a small, stable surface so stateful plugins do
not import deep host internals directly. Host applications can pass adapters for
novel/chapter readers, LLM calls, and event emitters as the platform matures.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional, Union, Tuple
import re

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
        plugin_name: Optional[str] = None,
        storage: Optional[PluginStorage] = None,
        novel_reader: Optional[Union[Reader, AsyncReader]] = None,
        chapter_reader: Optional[Union[Reader, AsyncReader]] = None,
        chapter_lister: Optional[Union[Reader, AsyncReader]] = None,
        llm_caller: Optional[Union[Reader, AsyncReader]] = None,
        event_emitter: Optional[Union[Reader, AsyncReader]] = None,
        host_database: Optional[ReadOnlyHostDatabase] = None,
        allow_raw_host_sql: bool = False,
        allow_cross_plugin_storage: bool = False,
    ) -> None:
        self.plugin_name = plugin_name
        self.storage = storage or PluginStorage()
        self.host_database = host_database or create_default_readonly_host_database()
        self.allow_raw_host_sql = allow_raw_host_sql
        self.allow_cross_plugin_storage = allow_cross_plugin_storage
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
        if not self.allow_raw_host_sql:
            raise PermissionError("Raw host SQL is disabled; use read_host_table or configured readers")
        if self.host_database is None:
            raise RuntimeError("host_database is not configured")
        return self.host_database.fetch_all(sql, params, limit=limit)

    def read_host_row(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if not self.allow_raw_host_sql:
            raise PermissionError("Raw host SQL is disabled; use read_host_table or configured readers")
        if self.host_database is None:
            raise RuntimeError("host_database is not configured")
        return self.host_database.fetch_one(sql, params)

    def read_host_table(
        self,
        table: str,
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        novel_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.host_database is None:
            raise RuntimeError("host_database is not configured")
        safe_table = _safe_table_identifier(table)
        safe_columns = [_safe_column_identifier(column) for column in (columns or ["*"])]
        if safe_columns != ["*"] and len(safe_columns) != len(set(safe_columns)):
            raise ValueError("Duplicate column names are not allowed")
        projection = ", ".join(safe_columns)
        sql = f"SELECT {projection} FROM {safe_table}"
        params: tuple[Any, ...] = ()
        if novel_id is not None:
            sql += " WHERE novel_id = ?"
            params = (novel_id,)
        sql += " LIMIT ?"
        params = (*params, max(1, min(int(limit), 500)))
        return self.host_database.fetch_all(sql, params)

    def read_host_table_row(
        self,
        table: str,
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        novel_id: str | None = None,
    ) -> Optional[dict[str, Any]]:
        rows = self.read_host_table(table, columns=columns, novel_id=novel_id, limit=1)
        return rows[0] if rows else None

    def read_plugin_state(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], default: Any = None) -> Any:
        self._assert_plugin_storage_scope(plugin_name)
        return self.storage.read_json(plugin_name, scope, default=default)

    def write_plugin_state(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: Any) -> Any:
        self._assert_plugin_storage_scope(plugin_name)
        return self.storage.write_json(plugin_name, scope, value)

    def read_own_plugin_state(self, scope: Union[list[str], Tuple[str, ...]], default: Any = None) -> Any:
        return self.storage.read_json(self._require_plugin_name(), scope, default=default)

    def write_own_plugin_state(self, scope: Union[list[str], Tuple[str, ...]], value: Any) -> Any:
        return self.storage.write_json(self._require_plugin_name(), scope, value)

    def _require_plugin_name(self) -> str:
        if not self.plugin_name:
            raise RuntimeError("plugin_name is required for own plugin storage access")
        return self.plugin_name

    def _assert_plugin_storage_scope(self, plugin_name: str) -> None:
        if self.allow_cross_plugin_storage or not self.plugin_name:
            return
        if plugin_name != self.plugin_name:
            raise PermissionError("Cross-plugin storage access is disabled; use own plugin storage APIs")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return identifier


def _safe_column_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if identifier == "*":
        return identifier
    return _safe_table_identifier(identifier)
