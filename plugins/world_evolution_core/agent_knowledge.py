"""Agent-owned full-project knowledge index for Evolution World.

The index lives in PluginStorage through EvolutionWorldRepository. It mirrors
PlotPilot and Evolution facts into compact documents/chunks so the Agent can
retrieve evidence instead of receiving full-book context in every prompt.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Optional


KNOWLEDGE_SCHEMA_VERSION = 1
DEFAULT_CHUNK_CHARS = 900
DEFAULT_CHUNK_OVERLAP = 120


class AgentKnowledgeBase:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def index_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        *,
        title: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self.index_document(
            novel_id,
            source_type="chapter_full_text",
            source_id=f"chapter_{chapter_number}",
            title=title or f"第{chapter_number}章全文",
            text=content,
            chapter_number=chapter_number,
            metadata=metadata or {},
            source_refs=[{"source_type": "chapter", "chapter_number": chapter_number}],
        )

    def index_host_context(self, novel_id: str, context: dict[str, Any]) -> dict[str, Any]:
        documents = 0
        chunks = 0
        for source_type in (
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
        ):
            for item in context.get(source_type) or []:
                if not isinstance(item, dict):
                    continue
                text = _item_text(item)
                if not text:
                    continue
                source_id = str(item.get("id") or item.get("name") or _hash_text(text))[:120]
                result = self.index_document(
                    novel_id,
                    source_type=source_type,
                    source_id=source_id,
                    title=str(item.get("name") or item.get("title") or source_id),
                    text=text,
                    chapter_number=_int_or_none(item.get("chapter_number")),
                    metadata={"host_source_type": item.get("source_type"), "indexed_from": "host_context"},
                    source_refs=[{"source_type": source_type, "source_id": source_id, "chapter_number": item.get("chapter_number")}],
                )
                documents += int(result.get("document_indexed") or 0)
                chunks += int(result.get("chunk_count") or 0)
        return {"documents_indexed": documents, "chunks_indexed": chunks}

    def index_agent_assets(
        self,
        novel_id: str,
        *,
        genes: Iterable[dict[str, Any]] = (),
        capsules: Iterable[dict[str, Any]] = (),
        reflections: Iterable[dict[str, Any]] = (),
        candidates: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        documents = 0
        chunks = 0
        specs = [
            ("gene", genes),
            ("capsule", capsules),
            ("reflection", reflections),
            ("gene_candidate", candidates),
        ]
        for source_type, items in specs:
            for item in items:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("id") or _hash_text(str(item)))[:120]
                text = _asset_text(source_type, item)
                if not text:
                    continue
                result = self.index_document(
                    novel_id,
                    source_type=source_type,
                    source_id=source_id,
                    title=str(item.get("title") or item.get("problem_pattern") or source_id),
                    text=text,
                    chapter_number=_int_or_none(item.get("chapter_number") or item.get("last_seen_chapter")),
                    metadata={"indexed_from": "agent_assets"},
                    source_refs=[{"source_type": source_type, "source_id": source_id}],
                )
                documents += int(result.get("document_indexed") or 0)
                chunks += int(result.get("chunk_count") or 0)
        return {"documents_indexed": documents, "chunks_indexed": chunks}

    def index_host_chapters(self, novel_id: str, host_database: Any, *, limit: int = 2000) -> dict[str, Any]:
        documents = 0
        chunks = 0
        chapter_count = 0
        for chapter in read_host_chapters_for_knowledge(host_database, novel_id, limit=limit):
            chapter_number = _int_or_none(chapter.get("chapter_number"))
            if not chapter_number:
                continue
            result = self.index_document(
                novel_id,
                source_type="chapter_full_text",
                source_id=f"chapter_{chapter_number}",
                title=str(chapter.get("title") or f"第{chapter_number}章全文"),
                text=str(chapter.get("content") or ""),
                chapter_number=chapter_number,
                metadata={"indexed_from": "knowledge_rebuild", "host_chapter_id": chapter.get("id")},
                source_refs=[{"source_type": "chapter", "chapter_number": chapter_number, "source_id": chapter.get("id")}],
            )
            chapter_count += 1
            documents += int(result.get("document_indexed") or 0)
            chunks += int(result.get("chunk_count") or 0)
        return {"documents_indexed": documents, "chunks_indexed": chunks, "chapter_count": chapter_count}

    def index_document(
        self,
        novel_id: str,
        *,
        source_type: str,
        source_id: str,
        title: str,
        text: str,
        chapter_number: int | None = None,
        metadata: Optional[dict[str, Any]] = None,
        source_refs: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        clean_text = _clean_text(text)
        if not novel_id or not clean_text:
            return {"document_indexed": False, "chunk_count": 0}
        now = _now()
        source_type = _safe_label(source_type or "unknown")
        source_id = _safe_label(source_id or _hash_text(clean_text))
        doc_id = f"doc_{_hash_text('|'.join([novel_id, source_type, source_id]))[:24]}"
        document = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "id": doc_id,
            "novel_id": novel_id,
            "source_type": source_type,
            "source_id": source_id,
            "chapter_number": chapter_number,
            "title": str(title or source_id)[:200],
            "text": clean_text[:12000],
            "hash": _hash_text(clean_text),
            "metadata": metadata or {},
            "source_refs": source_refs or [],
            "created_at": now,
            "updated_at": now,
        }
        chunks = []
        for index, chunk_text in enumerate(_chunk_text(clean_text)):
            chunk_id = f"chk_{_hash_text('|'.join([doc_id, str(index), chunk_text]))[:24]}"
            chunks.append(
                {
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                    "chunk_id": chunk_id,
                    "document_id": doc_id,
                    "novel_id": novel_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "chapter_number": chapter_number,
                    "title": document["title"],
                    "text": chunk_text,
                    "hash": _hash_text(chunk_text),
                    "vector_status": "keyword_indexed",
                    "source_refs": source_refs or [],
                    "metadata": {"chunk_index": index, **(metadata or {})},
                    "created_at": now,
                    "updated_at": now,
                }
            )
        self.repository.upsert_agent_knowledge_document(novel_id, document)
        for chunk in chunks:
            self.repository.upsert_agent_knowledge_chunk(novel_id, chunk)
        return {"document_indexed": True, "document_id": doc_id, "chunk_count": len(chunks)}

    def search(
        self,
        novel_id: str,
        query: str,
        *,
        before_chapter: int | None = None,
        source_types: Optional[list[str]] = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        terms = _terms(query)
        wanted = {str(item) for item in (source_types or []) if str(item)}
        candidates = []
        for chunk in self.repository.list_agent_knowledge_chunks(novel_id):
            if wanted and str(chunk.get("source_type") or "") not in wanted:
                continue
            chapter_number = _int_or_none(chunk.get("chapter_number"))
            if before_chapter and chapter_number and chapter_number >= before_chapter:
                continue
            score = _score_chunk(chunk, terms)
            if score <= 0 and terms:
                continue
            candidates.append((score, str(chunk.get("updated_at") or ""), chunk))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        items = []
        for score, _, chunk in candidates[: max(1, limit)]:
            items.append({**chunk, "score": round(score, 4)})
        return {
            "source": "agent_knowledge_base",
            "query": query,
            "items": items,
            "item_count": len(items),
            "source_types": sorted({str(item.get("source_type") or "") for item in items}),
            "coverage": self.coverage(novel_id),
        }

    def coverage(self, novel_id: str) -> dict[str, Any]:
        documents = self.repository.list_agent_knowledge_documents(novel_id)
        chunks = self.repository.list_agent_knowledge_chunks(novel_id)
        doc_counts = Counter(str(item.get("source_type") or "unknown") for item in documents)
        chunk_counts = Counter(str(item.get("source_type") or "unknown") for item in chunks)
        latest = ""
        for item in [*documents, *chunks]:
            latest = max(latest, str(item.get("updated_at") or ""))
        return {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "document_count": len(documents),
            "chunk_count": len(chunks),
            "document_counts_by_source": dict(sorted(doc_counts.items())),
            "chunk_counts_by_source": dict(sorted(chunk_counts.items())),
            "latest_updated_at": latest,
            "vector_status": "keyword_indexed",
        }


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("name"),
        item.get("title"),
        item.get("subject"),
        item.get("predicate"),
        item.get("description"),
        item.get("summary"),
        item.get("object"),
        item.get("note"),
        item.get("text"),
        item.get("event_summary"),
        item.get("progress_summary"),
        item.get("consistency_note"),
        item.get("fact_lock"),
    ]
    for key in ("open_threads", "beat_sections", "milestones"):
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(entry) for entry in value[:8])
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _asset_text(source_type: str, item: dict[str, Any]) -> str:
    if source_type == "gene":
        return "\n".join([str(item.get("title") or ""), *[str(part) for part in (item.get("strategy") or [])]])
    if source_type == "capsule":
        return "\n".join(str(item.get(key) or "") for key in ("title", "summary", "guidance"))
    if source_type == "reflection":
        parts = [item.get("problem_pattern"), item.get("root_cause"), item.get("content")]
        parts.extend(item.get("next_chapter_constraints") or [])
        return "\n".join(str(part).strip() for part in parts if str(part or "").strip())
    if source_type == "gene_candidate":
        return "\n".join([str(item.get("title") or ""), *[str(part) for part in (item.get("strategy_draft") or [])]])
    return _item_text(item)


def _chunk_text(text: str, *, size: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    cursor = 0
    step = max(1, size - overlap)
    while cursor < len(text):
        chunk = text[cursor : cursor + size].strip()
        if chunk:
            chunks.append(chunk)
        cursor += step
    return chunks[:80]


def _score_chunk(chunk: dict[str, Any], terms: list[str]) -> float:
    text = str(chunk.get("text") or "")
    if not terms:
        return 0.1
    score = 0.0
    for term in terms:
        if term and term in text:
            score += 1.0 + min(2.0, text.count(term) * 0.2)
    source_boost = {
        "chapter_full_text": 0.6,
        "memory_engine": 0.5,
        "triples": 0.45,
        "bible": 0.4,
        "story_knowledge": 0.35,
        "reflection": 0.3,
        "gene": 0.25,
    }.get(str(chunk.get("source_type") or ""), 0.0)
    return score + source_boost if score else 0.0


def _terms(text: str) -> list[str]:
    raw = _clean_text(text)
    terms: list[str] = []
    for token in raw.replace("\n", " ").replace("，", " ").replace("。", " ").replace("、", " ").split():
        token = token.strip("：:；;,.!?！？“”\"'()（）[]【】")
        if len(token) >= 2:
            terms.append(token[:24])
    for marker in ["上一章", "结尾", "进入", "离开", "抵达", "回到", "伏笔", "时间线", "角色", "知道", "发现", "重复"]:
        if marker in raw:
            terms.append(marker)
    return list(dict.fromkeys(terms))[:20]


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def read_host_chapters_for_knowledge(host_database: Any, novel_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
    if host_database is None or not novel_id:
        return []
    cols = _host_table_columns(host_database, "chapters")
    if not {"novel_id", "content"}.issubset(cols):
        return []
    number_column = "number" if "number" in cols else "chapter_number" if "chapter_number" in cols else ""
    if not number_column:
        return []
    id_expr = "id" if "id" in cols else number_column
    title_expr = "title" if "title" in cols else "''"
    updated_expr = "updated_at" if "updated_at" in cols else number_column
    try:
        return host_database.fetch_all(
            f"""
            SELECT {id_expr} AS id,
                   {number_column} AS chapter_number,
                   {title_expr} AS title,
                   content,
                   {updated_expr} AS updated_at
            FROM chapters
            WHERE novel_id = ? AND TRIM(COALESCE(content, '')) != ''
            ORDER BY {number_column} ASC
            LIMIT ?
            """,
            (novel_id, limit),
        )
    except Exception:
        return []


def _host_table_columns(host_database: Any, table: str) -> set[str]:
    if host_database is None or not table:
        return set()
    try:
        rows = host_database.fetch_all("SELECT name FROM pragma_table_info(?)", (table,))
    except Exception:
        return set()
    return {str(row.get("name") or "") for row in rows if row.get("name")}


def _safe_label(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or ""))
    return text[:120].strip("_") or "unknown"


def _hash_text(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
