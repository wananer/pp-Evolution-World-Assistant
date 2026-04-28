"""Diagnostics orchestration for Evolution World."""
from __future__ import annotations

import logging
from typing import Any, Callable

from .diagnostics import build_diagnostics

logger = logging.getLogger(__name__)


class DiagnosticsService:
    """Build and persist read-only diagnostics without touching the writing path."""

    def __init__(
        self,
        *,
        repository: Any,
        route_map_provider: Callable[[str], dict[str, Any]],
    ) -> None:
        self.repository = repository
        self.route_map_provider = route_map_provider

    def get_diagnostics(self, novel_id: str) -> dict[str, Any]:
        snapshot = build_diagnostics(
            novel_id=novel_id,
            repository=self.repository,
            host_context_summary=self.repository.get_host_context_summary(novel_id),
            semantic_recall_summary=self.repository.get_semantic_recall_summary(novel_id),
            agent_status=self.repository.get_agent_status(novel_id),
            route_map=self._route_map(novel_id),
        )
        self._save_snapshot(novel_id, snapshot)
        return snapshot

    def _route_map(self, novel_id: str) -> dict[str, Any]:
        try:
            return self.route_map_provider(novel_id)
        except Exception as exc:
            logger.warning("Evolution diagnostics route map degraded for %s: %s", novel_id, exc)
            return {
                "aggregate": {},
                "conflicts": [],
                "diagnostic_degraded": {
                    "source": "route_map",
                    "error": str(exc)[:240],
                },
            }

    def _save_snapshot(self, novel_id: str, snapshot: dict[str, Any]) -> None:
        try:
            self.repository.save_diagnostics_snapshot(novel_id, snapshot)
        except Exception as exc:
            logger.warning("Evolution diagnostics snapshot write failed for %s: %s", novel_id, exc)
            snapshot.setdefault("risks", []).append(
                {
                    "severity": "warning",
                    "source": "diagnostics",
                    "message": "诊断快照写入失败，本次结果仅作为即时响应返回。",
                    "suggestion": "检查插件专属存储权限和 diagnostics/history.jsonl 写入状态。",
                    "affected_feature": "diagnostics",
                    "evidence": {"error": str(exc)[:240]},
                }
            )
            summary = snapshot.setdefault("summary", {"critical": 0, "warning": 0, "info": 0, "total": 0})
            summary["warning"] = int(summary.get("warning") or 0) + 1
            summary["total"] = int(summary.get("total") or 0) + 1
