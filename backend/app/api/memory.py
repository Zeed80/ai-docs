"""Memory API — hybrid graph/structured memory search."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai_settings import get_ai_config
from app.db.models import (
    ChatMessage,
    ChatMessageAttachment,
    Document,
    DocumentChunk,
    DocumentExtraction,
    EvidenceSpan,
    KnowledgeEdge,
    KnowledgeNode,
    MemoryFact,
    MemoryEmbeddingRecord,
    MessageRating,
)
from app.db.session import get_db
from app.domain.graph import (
    EvidenceSpanOut,
    MemoryEmbeddingIndexRequest,
    MemoryEmbeddingIndexResponse,
    MemoryEmbeddingRebuildRequest,
    MemoryEmbeddingRebuildResponse,
    MemoryEmbeddingStatsResponse,
    MemoryExplainRequest,
    MemoryExplainResponse,
    MemoryReindexItem,
    MemoryReindexRequest,
    MemoryReindexResponse,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.domain.memory_builder import build_document_memory_async

router = APIRouter()

_TEXT_WEIGHT = float(os.getenv("MEMORY_TEXT_WEIGHT", "0.45"))
_VECTOR_WEIGHT = float(os.getenv("MEMORY_VECTOR_WEIGHT", "0.40"))
_GRAPH_WEIGHT = float(os.getenv("MEMORY_GRAPH_WEIGHT", "0.15"))
_VECTOR_SCORE_THRESHOLD = float(os.getenv("MEMORY_VECTOR_SCORE_THRESHOLD", "0.3"))
_AUTO_CANDIDATE_LIMIT = int(os.getenv("MEMORY_AUTO_CANDIDATE_LIMIT", "1000"))
_RERANK_CANDIDATE_LIMIT = int(os.getenv("MEMORY_RERANK_CANDIDATE_LIMIT", "30"))


class MemoryChatTurnRequest(BaseModel):
    user_text: str = Field("", max_length=12000)
    assistant_text: str = Field("", max_length=12000)
    session_id: str | None = None
    scope: str = "project"
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    metadata: dict | None = None


class MemoryPinRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    summary: str = Field(..., min_length=1)
    scope: str = "project"
    kind: str = "pinned_fact"
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    metadata: dict | None = None


class MemoryFactOut(BaseModel):
    id: uuid.UUID
    scope: str
    kind: str
    title: str
    summary: str
    source: str
    confidence: float
    pinned: bool
    metadata_: dict | None = Field(None, serialization_alias="metadata")

    model_config = {"from_attributes": True, "populate_by_name": True}


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(
    payload: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.search — Search graph nodes, chunks, and evidence spans.

    The public API still accepts historical retrieval modes, but execution is
    always automatic hybrid retrieval. The old mode value is treated as a
    compatibility hint only; users should not have to choose SQL vs vector vs
    graph manually.
    """
    offset = _decode_cursor(payload.cursor)
    page_limit = payload.limit
    internal_limit = _memory_candidate_limit(payload)
    search_payload = payload.model_copy(
        update={
            "limit": internal_limit,
            "retrieval_mode": "auto_hybrid",
        }
    )
    hits: list[MemorySearchHit] = []
    diagnostics: list[str] = []

    for query_text in _expanded_memory_queries(payload):
        query_payload = search_payload.model_copy(update={"query": query_text})
        pattern = f"%{query_text}%"
        hits.extend(await _search_memory_facts(db, query_payload, pattern))
        hits.extend(await _search_graph_nodes(db, query_payload, pattern))
        hits.extend(await _search_sql_memory(db, query_payload, pattern, remaining=internal_limit))

    vector_hits = await _search_vector_memory(db, search_payload, limit=internal_limit)
    if vector_hits:
        hits.extend(vector_hits)
    else:
        diagnostics.append("vector_memory_empty_or_unavailable")

    if payload.retrieval_mode != "auto_hybrid":
        diagnostics.append(f"retrieval_mode_deprecated:{payload.retrieval_mode}")

    hits = _merge_memory_hits(hits)
    hits = _rank_memory_hits(hits)

    if hits:
        cfg = get_ai_config()
        if cfg.get("reranker_model"):
            hits = _sort_memory_hits(payload.query, hits)
            reranked = await _try_rerank_hits(payload.query, hits[:_RERANK_CANDIDATE_LIMIT])
            hits = reranked + hits[_RERANK_CANDIDATE_LIMIT:]

    hits = _sort_memory_hits(payload.query, hits)
    total_available = len(hits)
    page = hits[offset: offset + page_limit]
    next_offset = offset + len(page)
    next_cursor = str(next_offset) if next_offset < total_available else None
    return MemorySearchResponse(
        query=payload.query,
        retrieval_mode="auto_hybrid",
        hits=page,
        total=len(page),
        total_available=total_available,
        next_cursor=next_cursor,
        coverage="complete" if next_cursor is None else "paged",
        diagnostics=diagnostics,
    )


@router.post("/chat-turn", response_model=MemoryFactOut)
async def store_chat_turn_memory(
    payload: MemoryChatTurnRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryFact:
    """Store an episodic chat turn as long-term memory."""
    user_text = " ".join((payload.user_text or "").split())
    assistant_text = " ".join((payload.assistant_text or "").split())
    if not user_text and not assistant_text:
        raise HTTPException(status_code=400, detail="user_text or assistant_text is required")
    title = user_text[:180] or "Assistant response"
    summary = "\n".join(
        part
        for part in [
            f"User: {user_text}" if user_text else "",
            f"Assistant: {assistant_text}" if assistant_text else "",
        ]
        if part
    )
    fact = MemoryFact(
        scope=payload.scope,
        kind="chat_turn",
        title=title,
        summary=summary[:4000],
        source="chat",
        confidence=payload.confidence,
        metadata_={"session_id": payload.session_id, **(payload.metadata or {})},
    )
    db.add(fact)
    await db.commit()
    await db.refresh(fact)
    return fact


@router.post("/pin", response_model=MemoryFactOut)
async def pin_memory_fact(
    payload: MemoryPinRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryFact:
    """Pin a verified memory fact so retrieval ranks it above ordinary turns."""
    fact = MemoryFact(
        scope=payload.scope,
        kind=payload.kind,
        title=payload.title,
        summary=payload.summary,
        source="user_pin",
        confidence=payload.confidence,
        pinned=True,
        metadata_=payload.metadata,
    )
    db.add(fact)
    await db.commit()
    await db.refresh(fact)
    return fact


class MemoryPruneRequest(BaseModel):
    scope: str | None = None
    kinds: list[str] = Field(default_factory=lambda: ["chat_turn"])
    older_than_days: int = Field(90, ge=1, le=3650)
    dry_run: bool = False


class MemoryPruneResult(BaseModel):
    deleted: int
    dry_run: bool
    scope: str | None
    kinds: list[str]
    cutoff: str


@router.post("/prune", response_model=MemoryPruneResult)
async def prune_memory_facts(
    payload: MemoryPruneRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryPruneResult:
    """Skill: memory.prune — Delete old episodic memory facts by scope and kind.

    Only non-pinned facts are removed. Protected kinds (pinned_fact, verified_fact)
    are never deleted regardless of scope/kinds filter.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=payload.older_than_days)
    protected_kinds = {"pinned_fact", "verified_fact"}
    safe_kinds = [k for k in payload.kinds if k not in protected_kinds]
    if not safe_kinds:
        raise HTTPException(
            status_code=400,
            detail=f"None of the requested kinds are pruneable. Protected: {sorted(protected_kinds)}",
        )

    stmt = select(func.count()).select_from(MemoryFact).where(
        MemoryFact.kind.in_(safe_kinds),
        MemoryFact.pinned.is_(False),
        MemoryFact.created_at < cutoff,
    )
    if payload.scope:
        stmt = stmt.where(MemoryFact.scope == payload.scope)
    count_result = await db.execute(stmt)
    count = count_result.scalar_one()

    if not payload.dry_run and count > 0:
        del_stmt = delete(MemoryFact).where(
            MemoryFact.kind.in_(safe_kinds),
            MemoryFact.pinned.is_(False),
            MemoryFact.created_at < cutoff,
        )
        if payload.scope:
            del_stmt = del_stmt.where(MemoryFact.scope == payload.scope)
        await db.execute(del_stmt)
        await db.commit()

    return MemoryPruneResult(
        deleted=count,
        dry_run=payload.dry_run,
        scope=payload.scope,
        kinds=safe_kinds,
        cutoff=cutoff.isoformat(),
    )


def _memory_candidate_limit(payload: MemorySearchRequest) -> int:
    base = _AUTO_CANDIDATE_LIMIT if payload.need_full_coverage else max(payload.limit * 4, 200)
    return max(payload.limit, min(base, _AUTO_CANDIDATE_LIMIT))


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _expanded_memory_queries(payload: MemorySearchRequest) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    for value in [payload.query, *(payload.entity_hints or [])]:
        text_value = " ".join(str(value or "").strip().split())
        if not text_value:
            continue
        key = text_value.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(text_value)
        if len(queries) >= 8:
            break
    return queries or [payload.query]


def _fts_condition(column, query: str):
    """Return a PostgreSQL tsvector FTS condition with Russian dictionary, fallback to ILIKE."""
    try:
        tsq = func.plainto_tsquery("russian", query)
        return func.to_tsvector("russian", column).op("@@")(tsq)
    except Exception:
        return column.ilike(f"%{query}%")


async def _search_graph_nodes(
    db: AsyncSession,
    payload: MemorySearchRequest,
    pattern: str,
) -> list[MemorySearchHit]:
    hits: list[MemorySearchHit] = []
    node_query = select(KnowledgeNode).where(
        or_(
            _fts_condition(KnowledgeNode.title, payload.query),
            _fts_condition(KnowledgeNode.summary, payload.query),
            KnowledgeNode.canonical_key.ilike(pattern),
        )
    )
    if payload.node_types:
        node_query = node_query.where(KnowledgeNode.node_type.in_(payload.node_types))
    if payload.document_id:
        node_query = node_query.where(KnowledgeNode.source_document_id == payload.document_id)
    node_result = await db.execute(
        node_query.order_by(KnowledgeNode.created_at.desc()).limit(payload.limit)
    )
    for node in node_result.scalars().all():
        score = _simple_score(payload.query, " ".join([node.title, node.summary or ""]))
        hits.append(
            MemorySearchHit(
                kind="node",
                id=node.id,
                title=node.title,
                summary=node.summary,
                score=score,
                source="graph",
                graph_score=score,
                source_document_id=node.source_document_id,
            )
        )
    return hits


async def _search_memory_facts(
    db: AsyncSession,
    payload: MemorySearchRequest,
    pattern: str,
) -> list[MemorySearchHit]:
    hits: list[MemorySearchHit] = []
    query = select(MemoryFact).where(
        or_(
            MemoryFact.title.ilike(pattern),
            MemoryFact.summary.ilike(pattern),
            MemoryFact.kind.ilike(pattern),
        )
    )
    if payload.scope:
        query = query.where(
            or_(MemoryFact.scope == payload.scope, MemoryFact.scope == "global")
        )
    result = await db.execute(
        query.order_by(MemoryFact.pinned.desc(), MemoryFact.created_at.desc()).limit(payload.limit)
    )
    for fact in result.scalars().all():
        score = _simple_score(payload.query, f"{fact.title} {fact.summary}")
        if fact.pinned:
            score = min(1.0, score + 0.15)
        hits.append(
            MemorySearchHit(
                kind="fact",
                id=fact.id,
                title=fact.title,
                summary=fact.summary,
                score=score,
                source=fact.source,
                text_score=score,
            )
        )
    return hits


async def _search_vector_memory(
    db: AsyncSession,
    payload: MemorySearchRequest,
    *,
    limit: int,
) -> list[MemorySearchHit]:
    try:
        from app.ai.embeddings import embed_text, get_active_embedding_profile
        from app.vector.qdrant_store import collection_count_for, search_similar

        profile = get_active_embedding_profile()
        if collection_count_for(profile.collection_name) <= 0:
            return []
        query_vector = await embed_text(payload.query, profile, task_type="query")
        vector_hits = search_similar(
            query_vector,
            limit=limit,
            collection_name=profile.collection_name,
            score_threshold=_VECTOR_SCORE_THRESHOLD,
        )
    except Exception:
        return []

    # Batch-load DB objects to avoid N+1 queries
    chunk_ids: list = []
    evidence_ids: list = []
    score_by_id: dict = {}
    doc_filter = str(payload.document_id) if payload.document_id else None
    for vector_hit in vector_hits:
        payload_data = vector_hit.get("payload") or {}
        if doc_filter and payload_data.get("document_id") != doc_filter:
            continue
        content_type = payload_data.get("content_type")
        content_id = _parse_uuid(payload_data.get("content_id"))
        if not content_id:
            continue
        score_by_id[str(content_id)] = float(vector_hit.get("score") or 0.0)
        if content_type == "document_chunk":
            chunk_ids.append(content_id)
        elif content_type == "evidence_span":
            evidence_ids.append(content_id)

    hits: list[MemorySearchHit] = []
    if chunk_ids:
        rows = (await db.execute(
            select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        )).scalars().all()
        for chunk in rows:
            hits.append(MemorySearchHit(
                kind="chunk",
                id=chunk.id,
                title=f"Document chunk #{chunk.chunk_index}",
                summary=chunk.text[:500],
                score=score_by_id.get(str(chunk.id), 0.0),
                source="vector",
                vector_score=score_by_id.get(str(chunk.id), 0.0),
                source_document_id=chunk.document_id,
            ))
    if evidence_ids:
        rows_e = (await db.execute(
            select(EvidenceSpan).where(EvidenceSpan.id.in_(evidence_ids))
        )).scalars().all()
        for evidence in rows_e:
            hits.append(MemorySearchHit(
                kind="evidence",
                id=evidence.id,
                title=evidence.field_name or "Evidence span",
                summary=evidence.text[:500],
                score=score_by_id.get(str(evidence.id), 0.0),
                source="vector",
                vector_score=score_by_id.get(str(evidence.id), 0.0),
                source_document_id=evidence.document_id,
                evidence=EvidenceSpanOut.model_validate(evidence),
            ))
    return hits


async def _try_rerank_hits(query: str, hits: list[MemorySearchHit]) -> list[MemorySearchHit]:
    config = get_ai_config()
    reranker_model = config.get("reranker_model")
    if not reranker_model:
        return hits
    try:
        from app.ai.router import AIRouter
        from app.ai.schemas import AIRequest, AITask

        documents = [hit.summary or hit.title for hit in hits]
        response = await AIRouter().run(
            AIRequest(
                task=AITask.RERANKING,
                input_text=query,
                preferred_model=reranker_model,
                metadata={"documents": documents},
                confidential=True,
            )
        )
        scores = response.scores or []
        reranked: list[MemorySearchHit] = []
        for index, hit in enumerate(hits):
            if index >= len(scores):
                reranked.append(hit)
                continue
            rerank_score = float(scores[index])
            final_score = max(float(hit.score or 0.0), rerank_score)
            reranked.append(
                hit.model_copy(
                    update={
                        "score": final_score,
                        "rerank_score": rerank_score,
                        "source": f"{hit.source}+rerank",
                    }
                )
            )
        return reranked
    except Exception:
        return hits


def _merge_memory_hits(hits: list[MemorySearchHit]) -> list[MemorySearchHit]:
    merged: dict[tuple[str, uuid.UUID], MemorySearchHit] = {}
    for hit in hits:
        key = (hit.kind, hit.id)
        existing = merged.get(key)
        if not existing:
            merged[key] = hit
            continue
        sources = sorted(set(existing.source.split("+")) | set(hit.source.split("+")))
        merged[key] = existing.model_copy(
            update={
                "score": max(existing.score, hit.score),
                "source": "+".join(sources),
                "text_score": (
                    existing.text_score if existing.text_score is not None else hit.text_score
                ),
                "vector_score": (
                    existing.vector_score if existing.vector_score is not None else hit.vector_score
                ),
                "graph_score": (
                    existing.graph_score if existing.graph_score is not None else hit.graph_score
                ),
                "rerank_score": (
                    existing.rerank_score if existing.rerank_score is not None else hit.rerank_score
                ),
                "evidence": existing.evidence or hit.evidence,
            }
        )
    return list(merged.values())


def _rank_memory_hits(hits: list[MemorySearchHit]) -> list[MemorySearchHit]:
    return [_rank_memory_hit(hit) for hit in hits]


def _rank_memory_hit(hit: MemorySearchHit) -> MemorySearchHit:
    scores = [
        (hit.text_score, _TEXT_WEIGHT),
        (hit.vector_score, _VECTOR_WEIGHT),
        (hit.graph_score, _GRAPH_WEIGHT),
    ]
    available = [(score, weight) for score, weight in scores if score is not None]
    if not available:
        return hit
    weight_sum = sum(weight for _, weight in available)
    weighted_score = sum(float(score) * weight for score, weight in available) / weight_sum
    return hit.model_copy(update={"score": round(weighted_score, 6)})


def _sort_memory_hits(query: str, hits: list[MemorySearchHit]) -> list[MemorySearchHit]:
    return sorted(hits, key=lambda hit: _memory_sort_score(query, hit), reverse=True)


def _memory_sort_score(query: str, hit: MemorySearchHit) -> float:
    """Keep exact episodic/pinned facts visible after vector reranking."""
    score = float(hit.score or 0.0)
    haystack = " ".join([hit.title or "", hit.summary or ""]).casefold()
    query_text = " ".join(query.split()).casefold()
    if hit.kind == "fact":
        score += 0.04
        if hit.source == "chat":
            score += 0.04
    if query_text and query_text in haystack:
        score += 0.08
    return score


async def _search_sql_memory(
    db: AsyncSession,
    payload: MemorySearchRequest,
    pattern: str,
    *,
    remaining: int,
) -> list[MemorySearchHit]:
    if remaining <= 0:
        return []
    hits: list[MemorySearchHit] = []

    chunk_query = select(DocumentChunk).where(
        _fts_condition(DocumentChunk.text, payload.query)
    )
    if payload.document_id:
        chunk_query = chunk_query.where(DocumentChunk.document_id == payload.document_id)
    chunk_result = await db.execute(
        chunk_query.order_by(DocumentChunk.created_at.desc()).limit(remaining)
    )
    for chunk in chunk_result.scalars().all():
        score = _simple_score(payload.query, chunk.text)
        hits.append(
            MemorySearchHit(
                kind="chunk",
                id=chunk.id,
                title=f"Document chunk #{chunk.chunk_index}",
                summary=chunk.text[:500],
                score=score,
                source="sql",
                text_score=score,
                source_document_id=chunk.document_id,
            )
        )

    remaining = remaining - len(hits)
    if remaining > 0:
        evidence_query = select(EvidenceSpan).where(
            _fts_condition(EvidenceSpan.text, payload.query)
        )
        if payload.document_id:
            evidence_query = evidence_query.where(EvidenceSpan.document_id == payload.document_id)
        evidence_result = await db.execute(
            evidence_query.order_by(EvidenceSpan.created_at.desc()).limit(remaining)
        )
        for evidence in evidence_result.scalars().all():
            score = _simple_score(payload.query, evidence.text)
            hits.append(
                MemorySearchHit(
                    kind="evidence",
                    id=evidence.id,
                    title=evidence.field_name or "Evidence span",
                    summary=evidence.text[:500],
                    score=score,
                    source="sql",
                    text_score=score,
                    source_document_id=evidence.document_id,
                    evidence=EvidenceSpanOut.model_validate(evidence),
                )
            )
    remaining = remaining - len(hits)
    if remaining > 0:
        chat_query = (
            select(ChatMessage)
            .where(
                ChatMessage.role.in_(["user", "assistant"]),
                _fts_condition(ChatMessage.content, payload.query),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(remaining)
        )
        if payload.document_id:
            chat_query = chat_query.where(
                ChatMessage.id.in_(
                    select(ChatMessageAttachment.message_id).where(
                        ChatMessageAttachment.document_id == payload.document_id,
                        ChatMessageAttachment.message_id.is_not(None),
                    )
                )
            )
        chat_result = await db.execute(chat_query)
        for message in chat_result.scalars().all():
            content = message.content or ""
            score = _simple_score(payload.query, content)
            hits.append(
                MemorySearchHit(
                    kind="chat_message",
                    id=message.id,
                    title=f"Chat {message.role}",
                    summary=content[:500],
                    score=score,
                    source="chat",
                    text_score=score,
                )
            )
    return hits


def _parse_uuid(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@router.post("/explain", response_model=MemoryExplainResponse)
async def explain_memory(
    payload: MemoryExplainRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.explain — Search memory and return evidence with graph context."""
    search_response = await search_memory(
        MemorySearchRequest(
            query=payload.query,
            node_types=payload.node_types,
            document_id=payload.document_id,
            limit=payload.limit,
            retrieval_mode=payload.retrieval_mode,
            include_explain=payload.include_explain,
        ),
        db,
    )
    nodes_by_id: dict = {}
    edges_by_id: dict = {}
    evidence_by_id: dict = {}
    node_frontier: set = set()

    for hit in search_response.hits:
        if hit.evidence:
            evidence_by_id[hit.evidence.id] = hit.evidence
        if hit.kind == "node":
            node = await db.get(KnowledgeNode, hit.id)
            if node:
                nodes_by_id[node.id] = node
                node_frontier.add(node.id)
        elif hit.kind == "chunk":
            chunk = await db.get(DocumentChunk, hit.id)
            if chunk:
                await _collect_document_context(
                    db,
                    document_id=chunk.document_id,
                    nodes_by_id=nodes_by_id,
                    evidence_by_id=evidence_by_id,
                    node_frontier=node_frontier,
                )
        elif hit.kind == "evidence":
            evidence = await db.get(EvidenceSpan, hit.id)
            if evidence:
                evidence_by_id[evidence.id] = EvidenceSpanOut.model_validate(evidence)
                await _collect_document_context(
                    db,
                    document_id=evidence.document_id,
                    nodes_by_id=nodes_by_id,
                    evidence_by_id=evidence_by_id,
                    node_frontier=node_frontier,
                )

    await _collect_graph_context(
        db,
        node_frontier=node_frontier,
        depth=payload.neighborhood_depth,
        nodes_by_id=nodes_by_id,
        edges_by_id=edges_by_id,
        evidence_by_id=evidence_by_id,
    )

    return MemoryExplainResponse(
        query=payload.query,
        hits=search_response.hits,
        nodes=list(nodes_by_id.values()),
        edges=list(edges_by_id.values()),
        evidence=list(evidence_by_id.values()),
        total_hits=search_response.total,
    )


@router.post("/embeddings/rebuild", response_model=MemoryEmbeddingRebuildResponse)
async def rebuild_memory_embeddings(
    payload: MemoryEmbeddingRebuildRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.embeddings_rebuild — Prepare chunk/evidence embeddings for Qdrant."""
    created = 0
    stale_marked = 0
    records: list[MemoryEmbeddingRecord] = []

    if payload.mark_stale_existing:
        existing_query = select(MemoryEmbeddingRecord).where(
            MemoryEmbeddingRecord.collection_name == payload.collection_name,
            MemoryEmbeddingRecord.status.in_(["queued", "indexed"]),
        )
        if payload.document_id:
            existing_query = existing_query.where(
                MemoryEmbeddingRecord.document_id == payload.document_id
            )
        existing = await db.execute(existing_query)
        for record in existing.scalars().all():
            record.status = "stale"
            stale_marked += 1

    if "document_chunk" in payload.content_types:
        chunk_query = select(DocumentChunk).limit(payload.limit)
        if payload.document_id:
            chunk_query = chunk_query.where(DocumentChunk.document_id == payload.document_id)
        chunk_result = await db.execute(chunk_query)
        for chunk in chunk_result.scalars().all():
            record = _embedding_record_for_chunk(chunk, payload)
            db.add(record)
            records.append(record)
            chunk.embedding_id = record.point_id
            created += 1

    if "evidence_span" in payload.content_types and created < payload.limit:
        evidence_query = select(EvidenceSpan).limit(payload.limit - created)
        if payload.document_id:
            evidence_query = evidence_query.where(EvidenceSpan.document_id == payload.document_id)
        evidence_result = await db.execute(evidence_query)
        for evidence in evidence_result.scalars().all():
            record = _embedding_record_for_evidence(evidence, payload)
            db.add(record)
            records.append(record)
            created += 1

    await db.commit()
    for record in records:
        await db.refresh(record)
    return MemoryEmbeddingRebuildResponse(
        records=records,
        created=created,
        stale_marked=stale_marked,
    )


@router.get("/embeddings/stats", response_model=MemoryEmbeddingStatsResponse)
async def get_memory_embedding_stats(
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.embeddings_stats — Show active embedding profile and record statuses."""
    from app.ai.embeddings import get_active_embedding_profile

    profile = get_active_embedding_profile()
    result = await db.execute(
        select(MemoryEmbeddingRecord.status, func.count())
        .group_by(MemoryEmbeddingRecord.status)
    )
    counts = {status: int(count) for status, count in result.all()}
    return MemoryEmbeddingStatsResponse(
        active_model=profile.model_key,
        active_collection=profile.collection_name,
        dimension=profile.dimension,
        counts_by_status=counts,
        total=sum(counts.values()),
    )


@router.post("/embeddings/rebuild-active", response_model=MemoryEmbeddingRebuildResponse)
async def rebuild_active_memory_embeddings(
    payload: MemoryEmbeddingRebuildRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.embeddings_rebuild_active — Rebuild records for active profile."""
    from app.ai.embeddings import get_active_embedding_profile

    profile = get_active_embedding_profile()
    active_payload = payload.model_copy(
        update={
            "collection_name": profile.collection_name,
            "embedding_model": profile.model_key,
            "vector_size": profile.dimension,
            "mark_stale_existing": payload.mark_stale_existing,
        }
    )
    return await rebuild_memory_embeddings(active_payload, db)


@router.post("/embeddings/index-active", response_model=MemoryEmbeddingIndexResponse)
async def index_active_memory_embeddings(
    payload: MemoryEmbeddingIndexRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.embeddings_index_active — Index queued/stale memory records into Qdrant."""
    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import ensure_collection, upsert_memory_embedding

    profile = get_active_embedding_profile()
    ensure_collection(
        collection_name=profile.collection_name,
        vector_size=profile.dimension,
        distance_metric=profile.distance_metric,
    )

    query = (
        select(MemoryEmbeddingRecord)
        .where(
            MemoryEmbeddingRecord.collection_name == profile.collection_name,
            MemoryEmbeddingRecord.embedding_model == profile.model_key,
            MemoryEmbeddingRecord.status.in_(payload.statuses),
        )
        .order_by(MemoryEmbeddingRecord.created_at)
        .limit(payload.limit)
    )
    if payload.document_id:
        query = query.where(MemoryEmbeddingRecord.document_id == payload.document_id)
    result = await db.execute(query)

    indexed = 0
    failed = 0
    skipped = 0
    for record in result.scalars().all():
        text = await _embedding_record_text(db, record)
        if not text:
            record.status = "failed"
            record.error = "source text not found"
            failed += 1
            continue
        try:
            vector = await embed_text(text, profile)
            upsert_memory_embedding(
                point_id=record.point_id,
                vector=vector,
                collection_name=profile.collection_name,
                payload={
                    "content_type": record.content_type,
                    "content_id": str(record.content_id),
                    "document_id": str(record.document_id) if record.document_id else "",
                    "document_version_id": (
                        str(record.document_version_id) if record.document_version_id else ""
                    ),
                    "embedding_model": profile.model_key,
                    "text_preview": text[:500],
                },
            )
            record.status = "indexed"
            record.vector_size = len(vector)
            record.error = None
            indexed += 1
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            failed += 1

    await db.commit()
    return MemoryEmbeddingIndexResponse(
        indexed=indexed,
        failed=failed,
        skipped=skipped,
        collection_name=profile.collection_name,
        embedding_model=profile.model_key,
    )


@router.post("/reindex", response_model=MemoryReindexResponse)
async def reindex_memory(
    payload: MemoryReindexRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.reindex — Rebuild graph memory for existing documents."""
    query = select(Document).order_by(Document.created_at.desc()).limit(payload.limit)
    if payload.document_ids:
        query = query.where(Document.id.in_(payload.document_ids))
    result = await db.execute(query)

    items: list[MemoryReindexItem] = []
    for document in result.scalars().all():
        text = await _document_memory_text(db, document)
        built = await build_document_memory_async(
            db,
            document,
            text=text,
            rebuild=payload.rebuild,
        )
        items.append(
            MemoryReindexItem(
                document_id=document.id,
                document_node_id=built.document_node_id,
                chunks_created=built.chunks_created,
                evidence_created=built.evidence_created,
                mentions_created=built.mentions_created,
                edges_created=built.edges_created,
                review_items_created=built.review_items_created,
            )
        )

    await db.commit()
    return MemoryReindexResponse(processed=len(items), items=items)


def _embedding_record_for_chunk(
    chunk: DocumentChunk,
    payload: MemoryEmbeddingRebuildRequest,
) -> MemoryEmbeddingRecord:
    point_id = f"chunk:{chunk.id}"
    return MemoryEmbeddingRecord(
        content_type="document_chunk",
        content_id=chunk.id,
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        collection_name=payload.collection_name,
        point_id=point_id,
        embedding_model=payload.embedding_model,
        vector_size=payload.vector_size or 768,
        status="queued",
        metadata_={"text_len": len(chunk.text), "chunk_index": chunk.chunk_index},
    )


def _embedding_record_for_evidence(
    evidence: EvidenceSpan,
    payload: MemoryEmbeddingRebuildRequest,
) -> MemoryEmbeddingRecord:
    point_id = f"evidence:{evidence.id}"
    return MemoryEmbeddingRecord(
        content_type="evidence_span",
        content_id=evidence.id,
        document_id=evidence.document_id,
        document_version_id=evidence.document_version_id,
        collection_name=payload.collection_name,
        point_id=point_id,
        embedding_model=payload.embedding_model,
        vector_size=payload.vector_size or 768,
        status="queued",
        metadata_={"text_len": len(evidence.text), "field_name": evidence.field_name},
    )


async def _embedding_record_text(
    db: AsyncSession,
    record: MemoryEmbeddingRecord,
) -> str | None:
    if record.content_type == "document_chunk":
        chunk = await db.get(DocumentChunk, record.content_id)
        return chunk.text if chunk else None
    if record.content_type == "evidence_span":
        evidence = await db.get(EvidenceSpan, record.content_id)
        return evidence.text if evidence else None
    return None


async def _collect_document_context(
    db: AsyncSession,
    *,
    document_id,
    nodes_by_id: dict,
    evidence_by_id: dict,
    node_frontier: set,
) -> None:
    node_result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "document",
            KnowledgeNode.entity_id == document_id,
        )
    )
    for node in node_result.scalars().all():
        nodes_by_id[node.id] = node
        node_frontier.add(node.id)

    evidence_result = await db.execute(
        select(EvidenceSpan).where(EvidenceSpan.document_id == document_id).limit(10)
    )
    for evidence in evidence_result.scalars().all():
        evidence_by_id[evidence.id] = EvidenceSpanOut.model_validate(evidence)


async def _collect_graph_context(
    db: AsyncSession,
    *,
    node_frontier: set,
    depth: int,
    nodes_by_id: dict,
    edges_by_id: dict,
    evidence_by_id: dict,
) -> None:
    frontier = set(node_frontier)
    seen = set(node_frontier)
    for _ in range(depth):
        if not frontier:
            return
        edge_result = await db.execute(
            select(KnowledgeEdge).where(
                (KnowledgeEdge.source_node_id.in_(frontier))
                | (KnowledgeEdge.target_node_id.in_(frontier))
            )
        )
        next_frontier = set()
        for edge in edge_result.scalars().all():
            edges_by_id[edge.id] = edge
            for node_id in (edge.source_node_id, edge.target_node_id):
                if node_id not in seen:
                    next_frontier.add(node_id)
                    seen.add(node_id)
            if edge.evidence_span_id and edge.evidence_span_id not in evidence_by_id:
                evidence = await db.get(EvidenceSpan, edge.evidence_span_id)
                if evidence:
                    evidence_by_id[evidence.id] = EvidenceSpanOut.model_validate(evidence)
        if next_frontier:
            nodes_result = await db.execute(
                select(KnowledgeNode).where(KnowledgeNode.id.in_(next_frontier))
            )
            for node in nodes_result.scalars().all():
                nodes_by_id[node.id] = node
        frontier = next_frontier


async def _document_memory_text(db: AsyncSession, document: Document) -> str:
    result = await db.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document.id)
        .order_by(DocumentExtraction.created_at.desc())
        .limit(1)
    )
    extraction = result.scalar_one_or_none()
    if not extraction:
        return document.file_name

    parts: list[str] = []
    if extraction.structured_data:
        parts.append(json.dumps(extraction.structured_data, ensure_ascii=False, sort_keys=True))
    if extraction.raw_output:
        parts.append(json.dumps(extraction.raw_output, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts) or document.file_name


def _simple_score(query: str, text: str) -> float:
    query_terms = {term.lower() for term in query.split() if term.strip()}
    if not query_terms:
        return 0.0
    text_lower = text.lower()
    matched = sum(1 for term in query_terms if term in text_lower)
    return matched / len(query_terms)


# ── Message Rating ────────────────────────────────────────────────────────────


class MessageRatingRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=120)
    message_id: str = Field(..., min_length=1, max_length=120)
    rating: int = Field(..., ge=-1, le=1)  # +1 or -1
    tools_used: list[str] = Field(default_factory=list)
    comment: str | None = Field(None, max_length=500)


class MessageRatingOut(BaseModel):
    id: uuid.UUID
    session_id: str
    message_id: str
    rating: int
    tools_used: list[str]


@router.post("/rate", response_model=MessageRatingOut)
async def rate_message(
    req: MessageRatingRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageRating:
    from app.ai.orchestrator_memory import record_user_rating

    row = MessageRating(
        session_id=req.session_id,
        message_id=req.message_id,
        rating=req.rating,
        tools_used=req.tools_used or [],
        comment=req.comment,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    # Mirror into Redis so orchestrator can use it immediately
    record_user_rating(
        tools_used=req.tools_used or [],
        rating=req.rating,
        session_id=req.session_id,
    )

    # Negative rating with comment → propose a learning rule for self-improvement
    if req.rating == -1 and req.comment and req.tools_used:
        try:
            import httpx as _httpx
            from app.ai.gateway_config import gateway_config as _gw
            await _httpx.AsyncClient(timeout=5.0).post(
                f"{_gw.backend_url}/api/technology/learning-rules",
                json={
                    "trigger_tools": req.tools_used,
                    "observation": req.comment,
                    "suggested_action": "review",
                    "status": "proposed",
                    "source": "user_rating",
                },
            )
        except Exception:
            pass

    return row


@router.get("/tool-ratings")
async def get_tool_ratings(db: AsyncSession = Depends(get_db)) -> dict:
    """Aggregated thumbs up/down per tool — used by admin UI."""
    from sqlalchemy import case
    result = await db.execute(
        select(
            MessageRating.tools_used,
            MessageRating.rating,
        ).where(MessageRating.tools_used.isnot(None))
    )
    rows = result.all()

    agg: dict[str, dict[str, int]] = {}
    for tools_used, rating in rows:
        for tool in (tools_used or []):
            if tool not in agg:
                agg[tool] = {"up": 0, "down": 0}
            if rating > 0:
                agg[tool]["up"] += 1
            elif rating < 0:
                agg[tool]["down"] += 1

    return agg
