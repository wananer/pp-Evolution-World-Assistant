"""File-backed sidecar storage for stateful PlotPilot plugins."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STORAGE_ROOT = _PROJECT_ROOT / "data" / "plugins"
_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


class PluginStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _DEFAULT_STORAGE_ROOT

    def read_json(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], default: Any = None) -> Any:
        path = self._path(plugin_name, scope)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: Any) -> Path:
        path = self._path(plugin_name, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def append_jsonl(self, plugin_name: str, scope: Union[list[str], Tuple[str, ...]], value: dict[str, Any]) -> Path:
        path = self._path(plugin_name, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        return path

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
    def _safe_segment(value: str) -> str:
        segment = str(value or "").strip()
        if not segment or segment in {".", ".."}:
            raise ValueError("invalid storage path segment")
        if any(ch not in _SAFE_CHARS for ch in segment):
            raise ValueError(f"unsafe storage path segment: {segment}")
        return segment
