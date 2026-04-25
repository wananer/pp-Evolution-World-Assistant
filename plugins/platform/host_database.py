"""Read-only access to the host PlotPilot database for plugins.

Plugins should use this facade instead of importing PlotPilot persistence
internals. The facade opens SQLite in read-only mode and accepts only read
queries, while PluginStorage remains the writable plugin-platform area.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


class ReadOnlyHostDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        rows = self.fetch_all(sql, params, limit=1)
        return rows[0] if rows else None

    def fetch_all(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        _assert_read_only_sql(sql)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
            return [dict(row) for row in rows]

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("Host database is read-only through the plugin platform")

    def transaction(self) -> None:
        raise PermissionError("Host database transactions are not exposed to plugins")

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Host database not found: {self.db_path}")
        uri = f"file:{quote(str(self.db_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn


def create_default_readonly_host_database() -> ReadOnlyHostDatabase | None:
    try:
        from application.paths import get_db_path

        return ReadOnlyHostDatabase(get_db_path())
    except Exception:
        return None


def _assert_read_only_sql(sql: str) -> None:
    normalized = (sql or "").strip()
    if not normalized:
        raise ValueError("SQL must not be empty")
    if ";" in normalized.rstrip(";"):
        raise PermissionError("Multiple SQL statements are not allowed")

    first_token = normalized.lstrip(" \t\r\n(").split(None, 1)[0].lower()
    if first_token not in {"select", "with"}:
        raise PermissionError("Only SELECT/WITH read queries are allowed")
