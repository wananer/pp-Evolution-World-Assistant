"""SQLite-backed sidecar storage for stateful PlotPilot plugins."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
_GLOBAL_NOVEL_ID = "__global__"


def default_plugin_storage_root() -> Path:
    """Return the dedicated read/write area owned by the plugin platform."""
    try:
        from application.paths import DATA_DIR

        return Path(DATA_DIR) / "plugin_platform"
    except Exception:
        return _PROJECT_ROOT / "data" / "plugin_platform"


class PluginStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_plugin_storage_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "plugin_platform.db"
        self._ensure_schema()

    def read_json(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], default: Any = None) -> Any:
        safe_plugin, novel_id, scope_key = self._record_key(plugin_name, scope)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM plugin_state WHERE plugin_name = ? AND novel_id = ? AND scope = ?",
                (safe_plugin, novel_id, scope_key),
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def write_json(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: Any) -> Path:
        safe_plugin, novel_id, scope_key = self._record_key(plugin_name, scope, value=value)
        chapter_number, entity_id, entity_name = self._metadata_from_value(value)
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plugin_state (
                    plugin_name, novel_id, scope, value_json, chapter_number, entity_id, entity_name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plugin_name, novel_id, scope) DO UPDATE SET
                    value_json = excluded.value_json,
                    chapter_number = excluded.chapter_number,
                    entity_id = excluded.entity_id,
                    entity_name = excluded.entity_name,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_plugin,
                    novel_id,
                    scope_key,
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    chapter_number,
                    entity_id,
                    entity_name,
                    now,
                    now,
                ),
            )
            conn.commit()
        path = self._path(plugin_name, scope)
        return path

    def append_jsonl(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: dict[str, Any]) -> Path:
        safe_plugin, novel_id, scope_key = self._record_key(plugin_name, scope, value=value)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plugin_log (plugin_name, novel_id, scope, value_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (safe_plugin, novel_id, scope_key, json.dumps(value, ensure_ascii=False, sort_keys=True), _utc_now_iso()),
            )
            conn.commit()
        path = self._path(plugin_name, scope)
        return path

    def list_json(
        self,
        plugin_name: str,
        prefix: Union[list[str], Tuple[str, ...]],
        *,
        limit: int | None = None,
        reverse: bool = False,
        before_chapter: int | None = None,
    ) -> list[Any]:
        safe_plugin, novel_id, scope_prefix = self._record_key(plugin_name, prefix)
        like_prefix = f"{scope_prefix}/%"
        clauses = ["plugin_name = ?", "novel_id = ?", "(scope = ? OR scope LIKE ?)"]
        params: list[Any] = [safe_plugin, novel_id, scope_prefix, like_prefix]
        if before_chapter is not None:
            clauses.append("chapter_number IS NOT NULL AND chapter_number < ?")
            params.append(int(before_chapter))
        direction = "DESC" if reverse else "ASC"
        sql = f"""
            SELECT value_json FROM plugin_state
            WHERE {' AND '.join(clauses)}
            ORDER BY chapter_number {direction}, scope {direction}
        """
        if limit is not None:
            if int(limit) <= 0:
                return []
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["value_json"]) for row in rows]

    def read_jsonl(
        self,
        plugin_name: str,
        scope: Union[list[str], Tuple[str, ...]],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        safe_plugin, novel_id, scope_key = self._record_key(plugin_name, scope)
        has_novel_scope = len(scope) >= 2 and str(scope[0]) == "novels"
        sql = """
            SELECT value_json FROM plugin_log
            WHERE plugin_name = ? AND scope = ?
            ORDER BY id ASC
        """
        params: tuple[Any, ...] = (safe_plugin, scope_key)
        if has_novel_scope:
            sql = """
                SELECT value_json FROM plugin_log
                WHERE plugin_name = ? AND novel_id = ? AND scope = ?
                ORDER BY id ASC
            """
            params = (safe_plugin, novel_id, scope_key)
        if limit is not None:
            if has_novel_scope:
                sql = """
                    SELECT value_json FROM (
                        SELECT id, value_json FROM plugin_log
                        WHERE plugin_name = ? AND novel_id = ? AND scope = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    ORDER BY id ASC
                """
                params = (safe_plugin, novel_id, scope_key, int(limit))
            else:
                sql = """
                    SELECT value_json FROM (
                        SELECT id, value_json FROM plugin_log
                        WHERE plugin_name = ? AND scope = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    ORDER BY id ASC
                """
                params = (safe_plugin, scope_key, int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            value = json.loads(row["value_json"])
            if isinstance(value, dict):
                items.append(value)
        return items

    def delete_json(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]]) -> bool:
        safe_plugin, novel_id, scope_key = self._record_key(plugin_name, scope)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM plugin_state WHERE plugin_name = ? AND novel_id = ? AND scope = ?",
                (safe_plugin, novel_id, scope_key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_json_prefix(self, plugin_name: str, prefix: Union[list[str], Tuple[str, ...]]) -> int:
        safe_plugin, novel_id, scope_prefix = self._record_key(plugin_name, prefix)
        like_prefix = f"{scope_prefix}/%"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM plugin_state
                WHERE plugin_name = ? AND novel_id = ? AND (scope = ? OR scope LIKE ?)
                """,
                (safe_plugin, novel_id, scope_prefix, like_prefix),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plugin_state (
                    plugin_name TEXT NOT NULL,
                    novel_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    chapter_number INTEGER,
                    entity_id TEXT,
                    entity_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plugin_name, novel_id, scope)
                );
                CREATE INDEX IF NOT EXISTS idx_plugin_state_novel
                    ON plugin_state(plugin_name, novel_id);
                CREATE TABLE IF NOT EXISTS plugin_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_name TEXT NOT NULL,
                    novel_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plugin_log_scope
                    ON plugin_log(plugin_name, novel_id, scope, id);
                """
            )
            self._ensure_state_columns(conn)
            conn.commit()

    def _ensure_state_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plugin_state)").fetchall()}
        for name, definition in (
            ("chapter_number", "INTEGER"),
            ("entity_id", "TEXT"),
            ("entity_name", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE plugin_state ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plugin_state_chapter ON plugin_state(plugin_name, novel_id, chapter_number, scope)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plugin_state_entity ON plugin_state(plugin_name, novel_id, entity_id)"
        )
        self._backfill_state_metadata(conn)

    def _backfill_state_metadata(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT plugin_name, novel_id, scope, value_json
            FROM plugin_state
            WHERE chapter_number IS NULL
              AND (scope LIKE '%/facts/%' OR scope LIKE '%/characters/%')
            """
        ).fetchall()
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
            chapter_number, entity_id, entity_name = self._metadata_from_value(value)
            if chapter_number is None and entity_id is None and entity_name is None:
                continue
            conn.execute(
                """
                UPDATE plugin_state
                SET chapter_number = ?, entity_id = ?, entity_name = ?
                WHERE plugin_name = ? AND novel_id = ? AND scope = ?
                """,
                (chapter_number, entity_id, entity_name, row["plugin_name"], row["novel_id"], row["scope"]),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _record_key(
        self,
        plugin_name: str,
        scope: Union[list[str], Tuple[str, ...]],
        *,
        value: Any = None,
    ) -> tuple[str, str, str]:
        safe_plugin = self._safe_segment(plugin_name)
        safe_scope = [self._safe_segment(segment) for segment in scope]
        if not safe_scope:
            raise ValueError("scope must not be empty")
        novel_id = self._novel_id_from_scope(safe_scope, value)
        return safe_plugin, novel_id, "/".join(safe_scope)

    def _path(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]]) -> Path:
        safe_plugin = self._safe_segment(plugin_name)
        safe_scope = [self._safe_segment(segment) for segment in scope]
        if not safe_scope:
            raise ValueError("scope must not be empty")
        path = (self.root / safe_plugin / Path(*safe_scope)).resolve()
        root = (self.root / safe_plugin).resolve()
        if root != path and root not in path.parents:
            raise ValueError("storage path escaped plugin root")
        return path

    @staticmethod
    def _novel_id_from_scope(scope: list[str], value: Any = None) -> str:
        if len(scope) >= 2 and scope[0] == "novels":
            return scope[1]
        if isinstance(value, dict) and value.get("novel_id"):
            return PluginStorage._safe_segment(str(value.get("novel_id")))
        return _GLOBAL_NOVEL_ID

    @staticmethod
    def _safe_segment(value: str) -> str:
        segment = str(value or "").strip()
        if not segment or segment in {".", ".."}:
            raise ValueError("invalid storage path segment")
        if any(ch not in _SAFE_CHARS for ch in segment):
            raise ValueError(f"unsafe storage path segment: {segment}")
        return segment

    @staticmethod
    def _metadata_from_value(value: Any) -> tuple[int | None, str | None, str | None]:
        if not isinstance(value, dict):
            return None, None, None
        chapter_number = _positive_int_or_none(value.get("chapter_number") or value.get("last_seen_chapter"))
        entity_id = _nonempty_text(value.get("character_id") or value.get("id"))
        entity_name = _nonempty_text(value.get("name"))
        return chapter_number, entity_id, entity_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonempty_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
