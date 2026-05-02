"""Read-only PlotPilot host context reader for Evolution World."""
from __future__ import annotations

import json
from typing import Any

from plugins.platform.host_database import ReadOnlyHostDatabase


HOST_CONTEXT_SOURCES = (
    "bible",
    "world",
    "knowledge",
    "story_knowledge",
    "storyline",
    "timeline",
    "chronicle",
    "foreshadow",
    "dialogue",
    "triples",
    "memory_engine",
)


class HostContextReader:
    def __init__(self, host_database: ReadOnlyHostDatabase | None = None) -> None:
        self.host_database = host_database

    def read(
        self,
        novel_id: str,
        *,
        query: str = "",
        before_chapter: int | None = None,
        limit: int = 6,
    ) -> dict[str, Any]:
        if not novel_id or self.host_database is None:
            return _empty_context(novel_id, reason="host_database_unavailable")

        degraded: list[str] = []
        source_status = self._source_table_status()
        sections = {
            "bible": self._safe("bible", degraded, self._read_bible, novel_id, limit),
            "world": self._safe("world", degraded, self._read_world, novel_id, limit),
            "knowledge": self._safe("knowledge", degraded, self._read_knowledge, novel_id, query, before_chapter, limit),
            "story_knowledge": self._safe("story_knowledge", degraded, self._read_story_knowledge, novel_id, before_chapter, limit),
            "storyline": self._safe("storyline", degraded, self._read_storylines, novel_id, limit),
            "timeline": self._safe("timeline", degraded, self._read_chronicles, novel_id, before_chapter, limit),
            "chronicle": self._safe("chronicle", degraded, self._read_chronicles, novel_id, before_chapter, limit),
            "foreshadow": self._safe("foreshadow", degraded, self._read_foreshadow, novel_id, limit),
            "dialogue": self._safe("dialogue", degraded, self._read_dialogue_samples, novel_id, before_chapter, limit),
            "triples": self._safe("triples", degraded, self._read_triples, novel_id, query, before_chapter, limit),
            "memory_engine": self._safe("memory_engine", degraded, self._read_memory_engine, novel_id, before_chapter, limit),
        }
        degraded.extend(source for source in source_status.get("missing_sources", []) if source not in degraded)
        counts = {key: len(value) if isinstance(value, list) else 0 for key, value in sections.items()}
        active_sources = [key for key, count in counts.items() if count]
        empty_sources = [
            source
            for source, status in (source_status.get("sources") or {}).items()
            if status.get("status") in {"present", "partial"} and not int(counts.get(source) or 0)
        ]
        field_missing_sources = list(source_status.get("field_missing_sources") or [])
        usage = _build_plotpilot_context_usage(
            counts,
            degraded,
            empty_sources=empty_sources,
            field_missing_sources=field_missing_sources,
        )
        return {
            "schema_version": 1,
            "novel_id": novel_id,
            "source": "plotpilot_host_readonly",
            "query": str(query or "")[:240],
            "before_chapter": before_chapter,
            "active_sources": active_sources,
            "degraded_sources": degraded,
            "counts": counts,
            "source_status": source_status.get("sources") or {},
            "empty_sources": empty_sources,
            "field_missing_sources": field_missing_sources,
            "plotpilot_context_usage": usage,
            **sections,
        }

    def summary(self, context: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(context, dict):
            return {}
        return {
            "source": context.get("source") or "plotpilot_host_readonly",
            "active_sources": list(context.get("active_sources") or []),
            "degraded_sources": list(context.get("degraded_sources") or []),
            "counts": dict(context.get("counts") or {}),
            "source_status": dict(context.get("source_status") or {}),
            "empty_sources": list(context.get("empty_sources") or []),
            "field_missing_sources": list(context.get("field_missing_sources") or []),
            "before_chapter": context.get("before_chapter"),
            "plotpilot_context_usage": dict(context.get("plotpilot_context_usage") or {}),
        }

    def _safe(self, name: str, degraded: list[str], fn: Any, *args: Any) -> list[dict[str, Any]]:
        try:
            return fn(*args)
        except Exception:
            degraded.append(name)
            return []

    def _read_bible(self, novel_id: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        character_cols = _table_columns(self.host_database, "bible_characters")
        character_desc = _concat_columns(
            character_cols,
            ["description", "mental_state", "mental_state_reason", "verbal_tic", "idle_behavior"],
        )
        rows.extend(
            _rows(
                self.host_database,
                f"""
                SELECT {_column_or_literal(character_cols, "id", "name")} AS id,
                       name,
                       {character_desc} AS description,
                       'character' AS kind, 'bible_character' AS source_type
                FROM bible_characters
                WHERE novel_id = ?
                ORDER BY id
                LIMIT ?
                """,
                (novel_id, limit),
            )
        )
        location_cols = _table_columns(self.host_database, "bible_locations")
        rows.extend(
            _rows(
                self.host_database,
                f"""
                SELECT {_column_or_literal(location_cols, "id", "name")} AS id,
                       name,
                       {_column_or_literal(location_cols, "description")} AS description,
                       {_column_or_literal(location_cols, "location_type")} AS kind,
                       'bible_location' AS source_type
                FROM bible_locations
                WHERE novel_id = ?
                ORDER BY {_order_column(location_cols, "updated_at")} DESC, id
                LIMIT ?
                """,
                (novel_id, max(2, limit // 2)),
            )
        )
        return [_compact_item(row) for row in rows[: limit * 2]]

    def _read_world(self, novel_id: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        rows.extend(
            _rows(
                self.host_database,
                """
                SELECT id, name, description, setting_type AS kind, 'world_setting' AS source_type
                FROM bible_world_settings
                WHERE novel_id = ?
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (novel_id, limit),
            )
        )
        rows.extend(
            _rows(
                self.host_database,
                """
                SELECT id, name, description, location_type AS kind, 'location' AS source_type
                FROM bible_locations
                WHERE novel_id = ?
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (novel_id, limit),
            )
        )
        rows.extend(
            _rows(
                self.host_database,
                """
                SELECT id, event AS name, description, time_point AS kind, 'timeline_note' AS source_type
                FROM bible_timeline_notes
                WHERE novel_id = ?
                ORDER BY sort_order DESC, id
                LIMIT ?
                """,
                (novel_id, limit),
            )
        )
        return [_compact_item(item) for item in rows[: limit * 3]]

    def _read_knowledge(self, novel_id: str, query: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        cols = _table_columns(self.host_database, "knowledge")
        if not cols:
            return []
        rows = _rows(
            self.host_database,
            f"""
            SELECT {_column_or_literal(cols, "id", "novel_id")} AS id,
                   '知识库前提锁' AS name,
                   {_column_or_literal(cols, "premise_lock")} AS description,
                   {_column_or_literal(cols, "version")} AS kind,
                   'knowledge_premise' AS source_type
            FROM knowledge
            WHERE novel_id = ?
            LIMIT ?
            """,
            (novel_id, limit),
        )
        return [_compact_item(item) for item in rows if str(item.get("description") or "").strip()]

    def _read_triples(self, novel_id: str, query: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        cols = _table_columns(self.host_database, "triples")
        if not cols:
            return []
        terms = _terms(query)
        rows: list[dict[str, Any]] = []
        id_expr = _column_or_literal(cols, "id", "subject")
        desc_expr = _column_or_literal(cols, "description", "note", "object")
        confidence_expr = _column_or_literal(cols, "confidence", literal="0")
        updated_expr = _order_column(cols, "updated_at")
        chapter_filter = "AND (? IS NULL OR chapter_number IS NULL OR chapter_number < ?)" if "chapter_number" in cols else ""
        chapter_select = "chapter_number" if "chapter_number" in cols else "NULL AS chapter_number"
        searchable = [col for col in ("subject", "predicate", "object", "description", "note") if col in cols]
        if terms:
            for term in terms[:4]:
                like_clause = " OR ".join(f"{col} LIKE ?" for col in searchable) if searchable else "subject LIKE ?"
                like_params = tuple(f"%{term}%" for _ in (searchable or ["subject"]))
                rows.extend(
                    _rows(
                        self.host_database,
                        f"""
                        SELECT {id_expr} AS id, subject AS name, {desc_expr} AS description,
                               predicate AS kind, object, {chapter_select}, 'triple' AS source_type
                        FROM triples
                        WHERE novel_id = ?
                          AND ({like_clause})
                          {chapter_filter}
                        ORDER BY {confidence_expr} DESC, {updated_expr} DESC
                        LIMIT ?
                        """,
                        (novel_id, *like_params, *((before_chapter, before_chapter) if chapter_filter else ()), limit),
                    )
                )
        if not rows:
            rows.extend(
                _rows(
                    self.host_database,
                    f"""
                    SELECT {id_expr} AS id, subject AS name, {desc_expr} AS description,
                           predicate AS kind, object, {chapter_select}, 'triple' AS source_type
                    FROM triples
                    WHERE novel_id = ?
                      {chapter_filter}
                    ORDER BY {confidence_expr} DESC, {updated_expr} DESC
                    LIMIT ?
                    """,
                    (novel_id, *((before_chapter, before_chapter) if chapter_filter else ()), limit),
                )
            )
        return _dedupe_items([_compact_item(item) for item in rows], limit)

    def _read_story_knowledge(self, novel_id: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        cols = _table_columns(self.host_database, "chapter_summaries")
        if not cols:
            return []
        desc_expr = _concat_columns(cols, ["summary", "open_threads", "consistency_note"])
        kind_expr = _column_or_literal(cols, "key_events")
        beat_select = "cs.beat_sections" if "beat_sections" in cols else "'' AS beat_sections"
        micro_select = "cs.micro_beats" if "micro_beats" in cols else "'' AS micro_beats"
        chapter_filter = "AND (? IS NULL OR cs.chapter_number < ?)" if "chapter_number" in cols else ""
        rows = _rows(
            self.host_database,
            f"""
            SELECT cs.id, '第' || cs.chapter_number || '章叙事同步' AS name,
                   {desc_expr} AS description,
                   {kind_expr} AS kind, cs.chapter_number, {beat_select}, {micro_select},
                   'story_knowledge_chapter_sync' AS source_type
            FROM chapter_summaries cs
            JOIN knowledge k ON k.id = cs.knowledge_id
            WHERE k.novel_id = ?
              {chapter_filter}
            ORDER BY cs.chapter_number DESC
            LIMIT ?
            """,
            (novel_id, *((before_chapter, before_chapter) if chapter_filter else ()), limit),
        )
        items = []
        for row in rows:
            item = _compact_item(row)
            beats = _json_list_texts(row.get("beat_sections"), limit=3)
            micro = _json_list_texts(row.get("micro_beats"), limit=3)
            if beats:
                item["beat_sections"] = beats
            if micro:
                item["micro_beats"] = micro
            items.append(item)
        return items

    def _read_memory_engine(self, novel_id: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        table = "memory_engine_state" if _table_columns(self.host_database, "memory_engine_state") else "memory_engine_states"
        cols = _table_columns(self.host_database, table)
        if not cols:
            return []
        chapter_filter = "AND (? IS NULL OR last_updated_chapter < ?)" if "last_updated_chapter" in cols else ""
        rows = _rows(
            self.host_database,
            f"""
            SELECT novel_id AS id, 'MemoryEngine fact lock' AS name, state_json AS description,
                   {_column_or_literal(cols, "last_updated_chapter", literal="NULL")} AS chapter_number, 'memory_engine_state' AS source_type
            FROM {table}
            WHERE novel_id = ?
              {chapter_filter}
            LIMIT ?
            """,
            (novel_id, *((before_chapter, before_chapter) if chapter_filter else ()), max(1, limit // 2)),
        )
        items = []
        for row in rows:
            item = _compact_item(row)
            item["description"] = _compact_text(_memory_state_brief(row.get("description")))
            items.append(item)
        return items

    def _read_storylines(self, novel_id: str, limit: int) -> list[dict[str, Any]]:
        rows = _rows(
            self.host_database,
            """
            SELECT s.id, COALESCE(s.name, s.storyline_type) AS name, COALESCE(s.description, '') AS description,
                   s.storyline_type AS kind, s.status, s.current_milestone_index, s.last_active_chapter,
                   'storyline' AS source_type
            FROM storylines s
            WHERE s.novel_id = ?
            ORDER BY s.status, s.last_active_chapter DESC, s.id
            LIMIT ?
            """,
            (novel_id, limit),
        )
        if not rows:
            rows = _rows(
                self.host_database,
                """
                SELECT s.id, s.storyline_type AS name, '' AS description,
                       s.storyline_type AS kind, s.status, s.current_milestone_index, 0 AS last_active_chapter,
                       'storyline' AS source_type
                FROM storylines s
                WHERE s.novel_id = ?
                ORDER BY s.status, s.id
                LIMIT ?
                """,
                (novel_id, limit),
            )
        items = []
        for row in rows:
            milestones = _rows(
                self.host_database,
                """
                SELECT title, description, target_chapter_start, target_chapter_end
                FROM storyline_milestones
                WHERE storyline_id = ?
                ORDER BY milestone_order
                LIMIT 4
                """,
                (row.get("id"),),
            )
            item = _compact_item(row)
            item["milestones"] = [_compact_text(f"{m.get('title')}: {m.get('description')}") for m in milestones]
            items.append(item)
        return items

    def _source_table_status(self) -> dict[str, Any]:
        if self.host_database is None:
            return {"sources": {}, "missing_sources": [], "field_missing_sources": []}
        source_tables = {
            "bible": ("bible_characters", "bible_locations"),
            "world": ("bible_world_settings", "bible_locations", "bible_timeline_notes"),
            "knowledge": ("knowledge",),
            "story_knowledge": ("knowledge", "chapter_summaries"),
            "storyline": ("storylines", "storyline_milestones"),
            "timeline": ("timeline_registries", "bible_timeline_notes", "novel_snapshots"),
            "chronicle": ("timeline_registries", "bible_timeline_notes", "novel_snapshots"),
            "foreshadow": ("novel_foreshadow_registry",),
            "dialogue": ("narrative_events",),
            "triples": ("triples",),
            "memory_engine": ("memory_engine_state", "memory_engine_states"),
        }
        try:
            rows = self.host_database.fetch_all(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name IN ({})".format(
                    ",".join("?" for _ in sorted({table for tables in source_tables.values() for table in tables}))
                ),
                tuple(sorted({table for tables in source_tables.values() for table in tables})),
            )
        except Exception:
            return {
                "sources": {
                    source: {
                        "status": "unknown",
                        "required_tables": list(tables),
                        "present_tables": [],
                        "missing_tables": list(tables),
                        "missing_fields": {},
                    }
                    for source, tables in source_tables.items()
                },
                "missing_sources": list(source_tables),
                "field_missing_sources": [],
            }
        existing = {str(row.get("name") or "") for row in rows}
        field_requirements = {
            "bible": {"bible_characters": ("novel_id", "name"), "bible_locations": ("novel_id", "name")},
            "world": {"bible_world_settings": ("novel_id",), "bible_locations": ("novel_id",), "bible_timeline_notes": ("novel_id",)},
            "knowledge": {"knowledge": ("novel_id",)},
            "story_knowledge": {"knowledge": ("id", "novel_id"), "chapter_summaries": ("knowledge_id", "chapter_number")},
            "storyline": {"storylines": ("novel_id",), "storyline_milestones": ("storyline_id",)},
            "timeline": {"timeline_registries": ("novel_id",), "bible_timeline_notes": ("novel_id",), "novel_snapshots": ("novel_id",)},
            "chronicle": {"timeline_registries": ("novel_id",), "bible_timeline_notes": ("novel_id",), "novel_snapshots": ("novel_id",)},
            "foreshadow": {"novel_foreshadow_registry": ("novel_id",)},
            "dialogue": {"narrative_events": ("novel_id", "chapter_number")},
            "triples": {"triples": ("novel_id", "subject", "predicate", "object")},
            "memory_engine": {"memory_engine_state": ("novel_id",), "memory_engine_states": ("novel_id",)},
        }
        sources: dict[str, dict[str, Any]] = {}
        missing_sources = []
        field_missing_sources = []
        for source, tables in source_tables.items():
            present = [table for table in tables if table in existing]
            missing = [table for table in tables if table not in existing]
            if source == "memory_engine":
                status = "present" if present else "missing"
            else:
                status = "present" if not missing else "missing"
            missing_fields: dict[str, list[str]] = {}
            for table in present:
                required_fields = field_requirements.get(source, {}).get(table, ())
                if not required_fields:
                    continue
                columns = _table_columns(self.host_database, table)
                absent = [field for field in required_fields if field not in columns]
                if absent:
                    missing_fields[table] = absent
            if status == "present" and missing_fields:
                status = "partial"
                field_missing_sources.append(source)
            if status == "missing":
                missing_sources.append(source)
            sources[source] = {
                "status": status,
                "required_tables": list(tables),
                "present_tables": present,
                "missing_tables": missing,
                "missing_fields": missing_fields,
            }
        return {"sources": sources, "missing_sources": missing_sources, "field_missing_sources": field_missing_sources}

    def _read_chronicles(self, novel_id: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        items = []
        rows = _rows(
            self.host_database,
            """
            SELECT id, time_point AS name, event AS description, 'bible_timeline' AS source_type
            FROM bible_timeline_notes
            WHERE novel_id = ?
            ORDER BY sort_order DESC, id
            LIMIT ?
            """,
            (novel_id, limit),
        )
        items.extend(_compact_item(row) for row in rows)
        snapshot_rows = _rows(
            self.host_database,
            """
            SELECT id, name, description, created_at, 'snapshot' AS source_type
            FROM novel_snapshots
            WHERE novel_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (novel_id, max(2, limit // 2)),
        )
        items.extend(_compact_item(row) for row in snapshot_rows)
        timeline_rows = _rows(
            self.host_database,
            "SELECT data FROM timeline_registries WHERE novel_id = ? LIMIT 1",
            (novel_id,),
        )
        if timeline_rows:
            try:
                data = json.loads(timeline_rows[0].get("data") or "{}")
                events = data.get("events") if isinstance(data.get("events"), list) else []
                for event in events[-limit:]:
                    chapter = _int_or_none(event.get("chapter_number"))
                    if before_chapter and chapter and chapter >= before_chapter:
                        continue
                    items.append(
                        {
                            "id": str(event.get("id") or ""),
                            "name": str(event.get("timestamp") or f"第{chapter or '?'}章"),
                            "description": _compact_text(event.get("event")),
                            "source_type": "timeline_registry",
                            "chapter_number": chapter,
                        }
                    )
            except Exception:
                pass
        return items[: limit * 2]

    def _read_foreshadow(self, novel_id: str, limit: int) -> list[dict[str, Any]]:
        rows = _rows(
            self.host_database,
            "SELECT payload FROM novel_foreshadow_registry WHERE novel_id = ? LIMIT 1",
            (novel_id,),
        )
        if not rows:
            return []
        try:
            payload = json.loads(rows[0].get("payload") or "{}")
        except Exception:
            return []
        raw_items = []
        raw_items.extend(payload.get("foreshadowings") or [])
        raw_items.extend(payload.get("subtext_entries") or [])
        items = []
        for item in raw_items[: limit * 2]:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "id": str(item.get("id") or item.get("foreshadowing_id") or ""),
                    "name": str(item.get("title") or item.get("question") or item.get("description") or "伏笔")[:120],
                    "description": _compact_text(item.get("description") or item.get("question") or item.get("answer") or ""),
                    "kind": str(item.get("status") or item.get("importance") or ""),
                    "chapter_number": _int_or_none(item.get("chapter") or item.get("chapter_planted") or item.get("planted_chapter")),
                    "source_type": "foreshadow_ledger",
                }
            )
        return items[:limit]

    def _read_dialogue_samples(self, novel_id: str, before_chapter: int | None, limit: int) -> list[dict[str, Any]]:
        rows = _rows(
            self.host_database,
            """
            SELECT event_id AS id, chapter_number, event_summary AS description, mutations, tags, 'narrative_event' AS source_type
            FROM narrative_events
            WHERE novel_id = ?
              AND (? IS NULL OR chapter_number < ?)
            ORDER BY chapter_number DESC, timestamp_ts DESC
            LIMIT ?
            """,
            (novel_id, before_chapter, before_chapter, limit * 3),
        )
        items = []
        for row in rows:
            text = _dialogue_from_event(row)
            if not text:
                continue
            items.append(
                {
                    "id": str(row.get("id") or ""),
                    "name": f"第{row.get('chapter_number') or '?'}章对白样本",
                    "description": text,
                    "chapter_number": _int_or_none(row.get("chapter_number")),
                    "source_type": "dialogue_sample",
                }
            )
            if len(items) >= limit:
                break
        return items


def render_host_context_sections(context: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = _render_plotpilot_native_strategy(context)
    if strategy:
        return [
            {
                "id": "plotpilot_native_strategy",
                "title": "PlotPilot 原生上下文策略",
                "kind": "plotpilot_native_context_strategy",
                "priority": 77,
                "token_budget": 360,
                "content": strategy,
                "items": {
                    "active_sources": list(context.get("active_sources") or []),
                    "counts": dict(context.get("counts") or {}),
                    "usage": dict(context.get("plotpilot_context_usage") or {}),
                },
            }
        ]
    blocks = []
    source_specs = [
        ("world", "host_world_context", "PlotPilot 世界观与设定", "host_world_context", 72, 340),
        ("storyline", "host_storyline_context", "PlotPilot 故事线", "host_storyline_context", 74, 320),
        ("chronicle", "host_chronicle_context", "PlotPilot 编年史", "host_chronicle_context", 70, 300),
        ("foreshadow", "host_foreshadow_context", "PlotPilot 伏笔账本", "host_foreshadow_context", 75, 320),
        ("knowledge", "host_knowledge_context", "PlotPilot 知识库", "host_knowledge_context", 68, 340),
        ("dialogue", "host_dialogue_voice_context", "PlotPilot 对话沙盒声线", "host_dialogue_voice_context", 56, 220),
    ]
    for source, block_id, title, kind, priority, budget in source_specs:
        items = [item for item in context.get(source) or [] if isinstance(item, dict)]
        if not items:
            continue
        blocks.append(
            {
                "id": block_id,
                "title": title,
                "kind": kind,
                "priority": priority,
                "token_budget": budget,
                "content": _render_items(title, items),
                "items": items[:8],
            }
        )
    return blocks


def _render_plotpilot_native_strategy(context: dict[str, Any]) -> str:
    if not isinstance(context, dict):
        return ""
    hard: list[str] = []
    soft: list[str] = []
    latest_story = _latest_item(context.get("story_knowledge"))
    if latest_story:
        hard.append(f"承接章后同步：{_item_label(latest_story)}；不要重复展开已完成 beat，优先推进 open threads。")
        beats = latest_story.get("beat_sections") if isinstance(latest_story.get("beat_sections"), list) else []
        if beats:
            soft.append(f"已有分章节拍：{' / '.join(str(item) for item in beats[:3])}；本章只补缺口，不机械复述。")
    storyline = _latest_item(context.get("storyline"))
    if storyline:
        milestone = ""
        milestones = storyline.get("milestones") if isinstance(storyline.get("milestones"), list) else []
        if milestones:
            milestone = f"；当前里程碑={milestones[0]}"
        soft.append(f"遵守故事线：{_item_label(storyline)}{milestone}；场景选择服务 milestone 推进。")
    foreshadow = _latest_item(context.get("foreshadow"))
    if foreshadow:
        soft.append(f"伏笔账本：{_item_label(foreshadow)}；到期伏笔优先推进或回收，少开无关新悬念。")
    timeline = _latest_item(context.get("timeline") or context.get("chronicle"))
    if timeline:
        hard.append(f"编年史/时间线：{_item_label(timeline)}；若时空跳转，先写明确桥段，避免章节首尾回滚。")
    bible = _latest_item(context.get("bible"))
    if bible:
        hard.append(f"Bible 边界：{_item_label(bible)}；人物事实、地点规则和声线以 Bible/章后同步为准。")
    triples = _latest_item(context.get("triples") or context.get("knowledge"))
    if triples:
        hard.append(f"图谱/知识事实：{_item_label(triples)}；角色不得重新发现已知信息，也不得无证据突破知识边界。")
    dialogue = _latest_item(context.get("dialogue"))
    if dialogue:
        soft.append(f"对话声线：参考{_item_label(dialogue)}；保持说话方式一致，避免模板化沉默与重复反应句。")
    if not hard and not soft:
        return ""
    lines = ["Evolution 已读取 PlotPilot 原生资料；这里只给写作操作约束，不重复注入全文资料。"]
    if hard:
        lines.append("【必须遵守】")
        lines.extend(f"- {item}" for item in hard[:4])
    if soft:
        lines.append("【建议参考】")
        lines.extend(f"- {item}" for item in soft[:4])
    return "\n".join(lines)


def _rows(db: ReadOnlyHostDatabase | None, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        return db.fetch_all(sql, params)
    except Exception:
        return []


def _table_columns(db: ReadOnlyHostDatabase | None, table: str) -> set[str]:
    if db is None or not table:
        return set()
    try:
        rows = db.fetch_all("SELECT name FROM pragma_table_info(?)", (table,))
    except Exception:
        return set()
    return {str(row.get("name") or "") for row in rows if row.get("name")}


def _column_or_literal(cols: set[str], *candidates: str, literal: str = "''") -> str:
    for candidate in candidates:
        if candidate and candidate in cols:
            return candidate
    return literal


def _concat_columns(cols: set[str], candidates: list[str]) -> str:
    parts = [f"COALESCE({candidate}, '')" for candidate in candidates if candidate in cols]
    if not parts:
        return "''"
    return "TRIM(" + " || ' ' || ".join(parts) + ")"


def _order_column(cols: set[str], preferred: str) -> str:
    if preferred in cols:
        return preferred
    if "updated_at" in cols:
        return "updated_at"
    if "created_at" in cols:
        return "created_at"
    if "id" in cols:
        return "id"
    return "rowid"


def _compact_item(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("name") or row.get("subject") or row.get("id") or "").strip()
    desc = str(row.get("description") or row.get("object") or row.get("note") or "").strip()
    obj = str(row.get("object") or "").strip()
    if obj and obj not in desc:
        desc = f"{desc} -> {obj}" if desc else obj
    return {
        "id": str(row.get("id") or "")[:120],
        "name": name[:120],
        "description": _compact_text(desc),
        "kind": str(row.get("kind") or row.get("status") or "")[:80],
        "source_type": str(row.get("source_type") or "")[:80],
        "chapter_number": _int_or_none(row.get("chapter_number") or row.get("last_active_chapter")),
    }


def _render_items(title: str, items: list[dict[str, Any]]) -> str:
    lines = [f"{title}（只读摘要，按当前章节相关性压缩）："]
    for item in items[:8]:
        name = item.get("name") or item.get("id") or item.get("source_type") or "条目"
        desc = item.get("description") or item.get("kind") or ""
        source = item.get("source_type") or item.get("kind") or ""
        lines.append(f"- {name}｜{source}：{desc}")
    return "\n".join(lines)


def _latest_item(value: Any) -> dict[str, Any] | None:
    items = [item for item in (value or []) if isinstance(item, dict)]
    return items[0] if items else None


def _item_label(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("id") or item.get("source_type") or "条目")
    desc = str(item.get("description") or item.get("kind") or "")
    return f"{name}（{_compact_text(desc, 90)}）"


def _json_list_texts(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "[]"))
        except Exception:
            raw = []
    texts: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("summary") or item.get("description") or item.get("text") or item.get("beat") or ""
        else:
            text = item
        compact = _compact_text(text, 120)
        if compact:
            texts.append(compact)
        if len(texts) >= limit:
            break
    return texts


def _memory_state_brief(value: Any) -> str:
    try:
        data = json.loads(str(value or "{}"))
    except Exception:
        return str(value or "")
    if not isinstance(data, dict):
        return str(value or "")
    parts = []
    for key in ("fact_lock", "completed_beats", "revealed_clues", "open_threads"):
        item = data.get(key)
        if item:
            parts.append(f"{key}={_compact_text(item, 120)}")
    return "；".join(parts) or json.dumps(data, ensure_ascii=False)[:260]


def _build_plotpilot_context_usage(
    counts: dict[str, int],
    degraded: list[str],
    *,
    empty_sources: list[str] | None = None,
    field_missing_sources: list[str] | None = None,
) -> dict[str, Any]:
    source_tiers = {
        "t0_fact_locks": ["bible", "timeline", "foreshadow", "memory_engine"],
        "t1_story_graph": ["storyline", "triples", "story_knowledge"],
        "t2_recent_chapter_sync": ["story_knowledge", "chronicle", "dialogue"],
        "t3_recall_support": ["knowledge", "triples", "dialogue"],
    }
    source_roles = {
        "story_knowledge": "chapter_after_sync",
        "triples": "graph_fact_source",
        "knowledge": "weak_recall_support",
    }
    return {
        "source": "plotpilot_native_context_adapter",
        "mode": "strategy_only",
        "source_tiers": source_tiers,
        "source_roles": source_roles,
        "hit_counts_by_tier": {
            tier: sum(int(counts.get(source) or 0) for source in sources)
            for tier, sources in source_tiers.items()
        },
        "degraded_sources": list(degraded),
        "empty_sources": list(empty_sources or []),
        "field_missing_sources": list(field_missing_sources or []),
        "long_context_duplicated": False,
    }


def _dialogue_from_event(row: dict[str, Any]) -> str:
    summary = str(row.get("description") or "").strip()
    candidates = []
    for field in ("mutations", "tags"):
        try:
            data = json.loads(row.get(field) or "[]")
        except Exception:
            data = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    text = str(item.get("dialogue") or item.get("line") or item.get("text") or item.get("summary") or "").strip()
                    if text:
                        candidates.append(text)
                elif isinstance(item, str) and ("“" in item or "\"" in item or "说" in item):
                    candidates.append(item)
    return _compact_text("；".join(candidates[:3]) or summary)


def _terms(query: str) -> list[str]:
    terms = []
    current = []
    for char in str(query or ""):
        if "\u4e00" <= char <= "\u9fff" or char.isalnum():
            current.append(char)
            continue
        if len(current) >= 2:
            terms.append("".join(current))
        current = []
    if len(current) >= 2:
        terms.append("".join(current))
    return _dedupe([term[-12:] for term in terms if len(term) <= 24])


def _dedupe_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = str(item.get("id") or item.get("name") or item.get("description") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _dedupe(items: list[Any]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _compact_text(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _empty_context(novel_id: str, *, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "source": "plotpilot_host_readonly",
        "active_sources": [],
        "degraded_sources": [reason],
        "counts": {key: 0 for key in HOST_CONTEXT_SOURCES},
        "source_status": {},
        "empty_sources": [],
        "field_missing_sources": [],
        "plotpilot_context_usage": _build_plotpilot_context_usage({key: 0 for key in HOST_CONTEXT_SOURCES}, [reason]),
        **{key: [] for key in HOST_CONTEXT_SOURCES},
    }
