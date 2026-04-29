"""Memory API — hybrid graph/structured memory search."""

import json
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    EvidenceSpan,
    KnowledgeEdge,
    KnowledgeNode,
    MemoryEmbeddingRecord,
)
from app.db.session import get_db
from app.domain.graph import (
    EvidenceSpanOut,
    MemoryExplainRequest,
    MemoryExplainResponse,
    MemoryEmbeddingRebuildRequest,
    MemoryEmbeddingRebuildResponse,
    MemoryEmbeddingIndexRequest,
    MemoryEmbeddingIndexResponse,
    MemoryEmbeddingStatsResponse,
    MemoryReindexItem,
    MemoryReindexRequest,
    MemoryReindexResponse,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.domain.memory_builder import build_document_memory_async
from app.api.ai_settings import get_ai_config

router = APIRouter()


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(
    payload: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: memory.search — Search graph nodes, chunks, and evidence spans."""
    pattern = f"%{payload.query}%"
    hits: list[MemorySearchHit] = []

    if payload.retrieval_mode in {"graph", "hybrid"}:
        hits.extend(await _search_graph_nodes(db, payload, pattern))

    if payload.retrieval_mode in {"sql", "sql_vector", "sql_vector_rerank", "hybrid"}:
        candidate_limit = payload.limit if payload.retrieval_mode == "sql" else payload.limit * 2
        hits.extend(await _search_sql_memory(db, payload, pattern, remaining=candidate_limit))

    if payload.retrieval_mode in {"sql_vector", "sql_vector_rerank", "hybrid"}:
        hits.extend(await _search_vector_memory(db, payload, limit=payload.limit * 2))

    hits = _merge_memory_hits(hits)

    if payload.retrieval_mode == "sql_vector_rerank" and hits:
        hits = await _try_rerank_hits(payload.query, hits)
    else:
        hits = _rank_memory_hits(hits)

    hits = sorted(hits, key=lambda hit: hit.score, reverse=True)[: payload.limit]
    return MemorySearchResponse(
        query=payload.query,
        retrieval_mode=payload.retrieval_mode,
        hits=hits,
        total=len(hits),
    )


async def _search_graph_nodes(
    db: AsyncSession,
    payload: MemorySearchRequest,
    pattern: str,
) -> list[MemorySearchHit]:
    hits: list[MemorySearchHit] = []
    node_query = select(KnowledgeNode).where(
        or_(
            KnowledgeNode.title.ilike(pattern),
            KnowledgeNode.summary.ilike(pattern),
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
        query_vector = await embed_text(payload.query, profile)
        vector_hits = search_similar(
            query_vector,
            limit=limit,
            collection_name=profile.collection_name,
            score_threshold=0.0,
        )
    except Exception:
        return []

    hits: list[MemorySearchHit] = []
    for vector_hit in vector_hits:
        payload_data = vector_hit.get("payload") or {}
        if payload.document_id and payload_data.get("document_id") != str(payload.document_id):
            continue
        content_type = payload_data.get("content_type")
        content_id = _parse_uuid(payload_data.get("content_id"))
        if not content_id:
            continue
        vector_score = float(vector_hit.get("score") or 0.0)
        if content_type == "document_chunk":
            chunk = await db.get(DocumentChunk, content_id)
            if not chunk:
                continue
            hits.append(
                MemorySearchHit(
                    kind="chunk",
                    id=chunk.id,
                    title=f"Document chunk #{chunk.chunk_index}",
                    summary=chunk.text[:500],
                    score=vector_score,
                    source="vector",
                    vector_score=vector_score,
                    source_document_id=chunk.document_id,
                )
            )
        elif content_type == "evidence_span":
            evidence = await db.get(EvidenceSpan, content_id)
            if not evidence:
                continue
            hits.append(
                MemorySearchHit(
                    kind="evidence",
                    id=evidence.id,
                    title=evidence.field_name or "Evidence span",
                    summary=evidence.text[:500],
                    score=vector_score,
                    source="vector",
                    vector_score=vector_score,
                    source_document_id=evidence.document_id,
                    evidence=EvidenceSpanOut.model_validate(evidence),
                )
            )
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
            reranked.append(
                hit.model_copy(
                    update={
                        "score": rerank_score,
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
                "text_score": existing.text_score if existing.text_score is not None else hit.text_score,
                "vector_score": (
                    existing.vector_score if existing.vector_score is not None else hit.vector_score
                ),
                "graph_score": existing.graph_score if existing.graph_score is not None else hit.graph_score,
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
        (hit.text_score, 0.45),
        (hit.vector_score, 0.40),
        (hit.graph_score, 0.15),
    ]
    available = [(score, weight) for score, weight in scores if score is not None]
    if not available:
        return hit
    weight_sum = sum(weight for _, weight in available)
    weighted_score = sum(float(score) * weight for score, weight in available) / weight_sum
    return hit.model_copy(update={"score": round(weighted_score, 6)})


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

    chunk_query = select(DocumentChunk).where(DocumentChunk.text.ilike(pattern))
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

    remaining = payload.limit - len(hits)
    if remaining > 0:
        evidence_query = select(EvidenceSpan).where(EvidenceSpan.text.ilike(pattern))
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
    """Skill: memory.embeddings_rebuild_active — Rebuild records for the active embedding profile."""
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
