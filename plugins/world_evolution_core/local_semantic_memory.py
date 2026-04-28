"""Local semantic memory adapter for Evolution World.

This adapter lets Evolution lean on PlotPilot's local vector collections and
read-only host database instead of expanding every prompt with large in-memory
state or asking the LLM to rediscover old facts.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from plugins.platform.host_database import ReadOnlyHostDatabase

if TYPE_CHECKING:
    from application.ai.vector_retrieval_facade import VectorRetrievalFacade
    from domain.ai.services.embedding_service import EmbeddingService
    from domain.ai.services.vector_store import VectorStore
else:
    VectorRetrievalFacade = Any
    EmbeddingService = Any
    VectorStore = Any

logger = logging.getLogger(__name__)


class LocalSemanticMemory:
    def __init__(
        self,
        *,
        host_database: ReadOnlyHostDatabase | None = None,
        vector_store: VectorStore | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.host_database = host_database
        self.vector_store = vector_store
        self.embedding_service = embedding_service
        self._facade: VectorRetrievalFacade | None = None
        self._dependency_resolution_attempted = vector_store is not None or embedding_service is not None
        self._last_vector_collections: dict[str, Any] = {}

    def search(
        self,
        novel_id: str,
        query: str,
        *,
        before_chapter: int | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        if not novel_id or not text:
            return {"items": [], "source": "empty_query", "vector_enabled": False, "collection_status": {}}

        vector_items = self._search_vectors(novel_id, text, before_chapter=before_chapter, limit=limit)
        if vector_items:
            return {
                "items": _dedupe_items(vector_items)[:limit],
                "source": "local_vector",
                "vector_enabled": True,
                "collection_status": dict(self._last_vector_collections),
            }

        db_items = self._search_host_keywords(novel_id, text, before_chapter=before_chapter, limit=limit)
        return {
            "items": db_items[:limit],
            "source": "host_keyword" if db_items else "none",
            "vector_enabled": self._facade is not None,
            "collection_status": dict(self._last_vector_collections),
        }

    def _search_vectors(
        self,
        novel_id: str,
        query: str,
        *,
        before_chapter: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        facade = self._ensure_facade()
        if facade is None:
            self._last_vector_collections = {
                "enabled": False,
                "checked": [],
                "missing": [],
                "queried": [],
            }
            return []

        items: list[dict[str, Any]] = []
        checked: list[str] = []
        missing: list[str] = []
        queried: list[str] = []
        for collection, source_type in (
            (f"novel_{novel_id}_chunks", "chapter_or_bible_vector"),
            (f"novel_{novel_id}_triples", "triple_vector"),
            (f"novel_{novel_id}_knowledge", "knowledge_vector"),
            (f"novel_{novel_id}_world", "worldbuilding_vector"),
            (f"novel_{novel_id}_storylines", "storyline_vector"),
            (f"novel_{novel_id}_foreshadows", "foreshadow_vector"),
            (f"novel_{novel_id}_dialogues", "dialogue_voice_vector"),
        ):
            checked.append(collection)
            if _known_local_collection_missing(self.vector_store, collection):
                missing.append(collection)
                continue
            queried.append(collection)
            for hit in _safe_vector_search(facade, collection, query, limit=limit):
                payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
                chapter_number = _int_or_none(payload.get("chapter_number"))
                if before_chapter and chapter_number and chapter_number >= before_chapter:
                    continue
                item = _semantic_item_from_payload(payload, source_type=source_type, score=hit.get("score"))
                if item:
                    items.append(item)
        items.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
        self._last_vector_collections = {
            "enabled": True,
            "checked": checked,
            "missing": missing,
            "queried": queried,
        }
        return items

    def _search_host_keywords(
        self,
        novel_id: str,
        query: str,
        *,
        before_chapter: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self.host_database is None:
            return []
        terms = _extract_query_terms(query)[:8]
        if not terms:
            return []
        rows: list[dict[str, Any]] = []
        for term in terms:
            params: list[Any] = [novel_id, f"%{term}%", f"%{term}%", f"%{term}%", f"%{term}%"]
            chapter_filter = ""
            if before_chapter:
                chapter_filter = "AND (chapter_number IS NULL OR chapter_number < ?)"
                params.append(before_chapter)
            try:
                rows.extend(
                    self.host_database.fetch_all(
                        f"""
                        SELECT id, subject, predicate, object, description, chapter_number, confidence
                        FROM triples
                        WHERE novel_id = ?
                          AND (subject LIKE ? OR predicate LIKE ? OR object LIKE ? OR description LIKE ?)
                          {chapter_filter}
                        ORDER BY confidence DESC, updated_at DESC
                        LIMIT 8
                        """,
                        tuple(params),
                    )
                )
            except Exception as exc:
                logger.debug("Evolution host triple keyword search skipped: %s", exc)
            rows.extend(_safe_keyword_rows(self.host_database, "bible_world_settings", novel_id, term, limit))
            rows.extend(_safe_keyword_rows(self.host_database, "bible_locations", novel_id, term, limit))
            rows.extend(_safe_keyword_rows(self.host_database, "storylines", novel_id, term, limit))
            rows.extend(_safe_keyword_rows(self.host_database, "bible_timeline_notes", novel_id, term, limit))
            rows.extend(_safe_foreshadow_keyword_rows(self.host_database, novel_id, term, limit))

        items = []
        seen: set[str] = set()
        for row in rows:
            key = str(row.get("id") or f"{row.get('subject')}:{row.get('predicate')}:{row.get('object')}")
            if key in seen:
                continue
            seen.add(key)
            summary = _format_triple_text(row)
            if not summary:
                continue
            items.append(
                {
                    "source_type": str(row.get("source_type") or "host_triple_keyword"),
                    "id": key,
                    "chapter_number": _int_or_none(row.get("chapter_number")),
                    "text": summary,
                    "score": float(row.get("confidence") or 0.5),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _ensure_facade(self) -> VectorRetrievalFacade | None:
        if self._facade is not None:
            return self._facade
        if not self._dependency_resolution_attempted:
            self._dependency_resolution_attempted = True
            try:
                from interfaces.api.dependencies import get_embedding_service, get_vector_store

                self.vector_store = self.vector_store or get_vector_store()
                self.embedding_service = self.embedding_service or get_embedding_service()
            except Exception as exc:
                logger.debug("Evolution vector dependencies unavailable: %s", exc)
        if self.vector_store is None or self.embedding_service is None:
            return None
        try:
            from application.ai.vector_retrieval_facade import VectorRetrievalFacade as RuntimeVectorRetrievalFacade
        except Exception as exc:
            logger.debug("Evolution vector retrieval facade unavailable: %s", exc)
            return None
        self._facade = RuntimeVectorRetrievalFacade(self.vector_store, self.embedding_service)
        return self._facade


def _safe_vector_search(facade: VectorRetrievalFacade, collection: str, query: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        return facade.sync_search(collection, query, limit=limit)
    except Exception as exc:
        logger.debug("Evolution vector search skipped collection=%s: %s", collection, exc)
        return []


def _known_local_collection_missing(vector_store: VectorStore | None, collection: str) -> bool:
    collections = getattr(vector_store, "collections", None)
    if isinstance(collections, dict):
        return collection not in collections
    if isinstance(collections, (list, tuple, set)):
        return collection not in collections
    return False


def _safe_keyword_rows(db: ReadOnlyHostDatabase | None, table: str, novel_id: str, term: str, limit: int) -> list[dict[str, Any]]:
    if db is None:
        return []
    specs = {
        "bible_world_settings": ("name", "description", "setting_type", "world_setting"),
        "bible_locations": ("name", "description", "location_type", "location"),
        "storylines": ("COALESCE(name, storyline_type)", "COALESCE(description, progress_summary, '')", "storyline_type", "storyline"),
        "bible_timeline_notes": ("event", "description", "time_point", "chronicle"),
    }
    if table not in specs:
        return []
    name_col, desc_col, kind_col, source_type = specs[table]
    try:
        rows = db.fetch_all(
            f"""
            SELECT id, {name_col} AS subject, {kind_col} AS predicate, {desc_col} AS object,
                   {desc_col} AS description, NULL AS chapter_number, 0.55 AS confidence,
                   '{source_type}' AS source_type
            FROM {table}
            WHERE novel_id = ?
              AND ({name_col} LIKE ? OR {desc_col} LIKE ?)
            LIMIT ?
            """,
            (novel_id, f"%{term}%", f"%{term}%", limit),
        )
    except Exception:
        if table != "storylines":
            return []
        try:
            rows = db.fetch_all(
                """
                SELECT id, storyline_type AS subject, storyline_type AS predicate, status AS object,
                       status AS description, NULL AS chapter_number, 0.5 AS confidence,
                       'storyline' AS source_type
                FROM storylines
                WHERE novel_id = ?
                  AND (storyline_type LIKE ? OR status LIKE ?)
                LIMIT ?
                """,
                (novel_id, f"%{term}%", f"%{term}%", limit),
            )
        except Exception:
            return []
    return rows


def _safe_foreshadow_keyword_rows(db: ReadOnlyHostDatabase | None, novel_id: str, term: str, limit: int) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        rows = db.fetch_all("SELECT payload FROM novel_foreshadow_registry WHERE novel_id = ? LIMIT 1", (novel_id,))
    except Exception:
        return []
    if not rows:
        return []
    try:
        payload = json.loads(rows[0].get("payload") or "{}")
    except Exception:
        return []
    raw_items = []
    raw_items.extend(payload.get("foreshadowings") or [])
    raw_items.extend(payload.get("subtext_entries") or [])
    result = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(key) or "") for key in ("title", "description", "question", "answer", "status"))
        if term not in text:
            continue
        result.append(
            {
                "id": str(item.get("id") or item.get("foreshadowing_id") or ""),
                "subject": str(item.get("title") or item.get("question") or "伏笔"),
                "predicate": str(item.get("status") or "foreshadow"),
                "object": str(item.get("description") or item.get("answer") or ""),
                "description": text[:260],
                "chapter_number": _int_or_none(item.get("chapter") or item.get("chapter_planted") or item.get("planted_chapter")),
                "confidence": 0.55,
                "source_type": "foreshadow_ledger",
            }
        )
        if len(result) >= limit:
            break
    return result


def _semantic_item_from_payload(payload: dict[str, Any], *, source_type: str, score: Any) -> dict[str, Any] | None:
    text = str(payload.get("text") or "").strip()
    if source_type == "triple_vector":
        text = text or _format_triple_text(payload)
    if not text:
        return None
    return {
        "source_type": source_type,
        "id": str(payload.get("triple_id") or payload.get("id") or ""),
        "kind": str(payload.get("kind") or ""),
        "chapter_number": _int_or_none(payload.get("chapter_number")),
        "text": text[:260],
        "subject": str(payload.get("subject") or ""),
        "predicate": str(payload.get("predicate") or ""),
        "object": str(payload.get("object") or ""),
        "score": float(score or payload.get("confidence") or 0.0),
    }


def _format_triple_text(record: dict[str, Any]) -> str:
    subject = str(record.get("subject") or "").strip()
    predicate = str(record.get("predicate") or "").strip()
    obj = str(record.get("object") or "").strip()
    description = str(record.get("description") or "").strip()
    parts = []
    if subject and predicate and obj:
        parts.append(f"{subject} —{predicate}→ {obj}")
    if description:
        parts.append(description)
    return "；".join(parts)


def _extract_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    current = []
    for char in str(query or ""):
        if "\u4e00" <= char <= "\u9fff" or char.isalnum():
            current.append(char)
            continue
        if len(current) >= 2:
            terms.extend(_term_variants("".join(current)))
        current = []
    if len(current) >= 2:
        terms.extend(_term_variants("".join(current)))
    return _dedupe_strings([term[-12:] for term in terms if len(term) <= 24])


def _term_variants(term: str) -> list[str]:
    if len(term) <= 8:
        return [term]
    variants = [term[-12:]]
    for size in (2, 3, 4):
        for index in range(0, max(len(term) - size + 1, 0), size):
            part = term[index : index + size]
            if len(part) >= 2:
                variants.append(part)
    return variants


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("id") or item.get("text") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
