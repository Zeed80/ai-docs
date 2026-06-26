"""Memory API — hybrid graph/structured memory search."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai_settings import get_ai_config
from app.api.web_search import WebSearchRequest, execute_web_search
from app.auth.jwt import require_human_role
from app.auth.models import UserInfo, UserRole
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
    MemoryEvidenceItem,
    MemoryQueryRequest,
    MemoryQueryResponse,
    MemoryReindexItem,
    MemoryReindexRequest,
    MemoryReindexResponse,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.domain.memory_builder import build_document_memory_async

router = APIRouter()

# Reciprocal Rank Fusion: each retrieval branch is ranked by its own raw score
# and fused by rank position, not absolute score. This avoids calibrating
# incompatible scales (cosine 0–1 vs ts_rank_cd 0–∞ vs term overlap 0–1).
# _RRF_K dampens the contribution of low-ranked items (standard value 60).
_RRF_K = int(os.getenv("MEMORY_RRF_K", "60"))
# Per-branch trust: vector and lexical are primary, graph is a weaker signal.
_RRF_BRANCH_WEIGHTS = {
    "text": float(os.getenv("MEMORY_RRF_TEXT_WEIGHT", "1.0")),
    "vector": float(os.getenv("MEMORY_RRF_VECTOR_WEIGHT", "1.0")),
    "graph": float(os.getenv("MEMORY_RRF_GRAPH_WEIGHT", "0.5")),
}
_VECTOR_SCORE_THRESHOLD = float(os.getenv("MEMORY_VECTOR_SCORE_THRESHOLD", "0.3"))
_AUTO_CANDIDATE_LIMIT = int(os.getenv("MEMORY_AUTO_CANDIDATE_LIMIT", "1000"))
_RERANK_CANDIDATE_LIMIT = int(os.getenv("MEMORY_RERANK_CANDIDATE_LIMIT", "30"))


class MemoryChatTurnRequest(BaseModel):
    user_text: str = Field("", max_length=12000)
    assistant_text: str = Field("", max_length=12000)
    session_id: str | None = None
    scope: str = "session"
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    metadata: dict | None = None


class MemoryPinRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    summary: str = Field(..., min_length=1)
    scope: str = "project"
    kind: str = "pinned_fact"
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    metadata: dict | None = None


class MemoryPromotionRequest(BaseModel):
    source_fact_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=500)
    summary: str | None = None
    confidence: float = Field(0.8, ge=0.0, le=1.0)
    metadata: dict | None = None


class MemoryPromotionDecision(BaseModel):
    approved: bool
    decided_by: str = Field("user", min_length=1, max_length=100)
    comment: str | None = None


class MemoryPromotionEvaluation(BaseModel):
    fact_id: uuid.UUID
    status: str
    passed: bool
    checks: list[dict]
    diagnostics: list[str] = []


class WebSourceProposalRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    url: str = Field(..., min_length=1, max_length=2000)
    supplier_name: str | None = Field(default=None, max_length=300)
    source_type: str = Field("supplier_catalog", min_length=1, max_length=120)
    rationale: str | None = None
    domains: list[str] | None = None
    metadata: dict | None = None


class WebSourceDecision(BaseModel):
    approved: bool
    decided_by: str = Field("user", min_length=1, max_length=100)
    comment: str | None = None


class WebSourceDiscoveryRequest(BaseModel):
    query: str | None = Field(default=None, max_length=500)
    supplier_name: str | None = Field(default=None, max_length=300)
    source_type: str = Field("supplier_catalog", min_length=1, max_length=120)
    domains: list[str] | None = None
    limit: int = Field(5, ge=1, le=20)
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    rationale: str | None = None


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


class WebSourceDiscoveryResponse(BaseModel):
    query: str
    provider: str
    proposed: list[MemoryFactOut]
    skipped_existing: int = 0
    diagnostics: list[str] = []


def _normalize_chat_turn_scope(scope: str | None, metadata: dict | None) -> tuple[str, dict]:
    """Keep raw chat turns scoped to the session unless explicitly promoted.

    Project/global memory should represent reviewed knowledge, not every model
    utterance. Callers that deliberately promote a turn must mark metadata with
    ``trusted`` or ``promoted``.
    """
    normalized_metadata = dict(metadata or {})
    requested_scope = (scope or "session").strip() or "session"
    trusted = normalized_metadata.get("trusted") is True or normalized_metadata.get("promoted") is True
    if requested_scope in {"project", "global"} and not trusted:
        normalized_metadata["requested_scope"] = requested_scope
        normalized_metadata["scope_policy"] = "chat_turn_demoted_to_session"
        return "session", normalized_metadata
    return requested_scope, normalized_metadata


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

    hits = _rrf_fuse(hits)

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
    scope, metadata = _normalize_chat_turn_scope(payload.scope, payload.metadata)
    fact = MemoryFact(
        scope=scope,
        kind="chat_turn",
        title=title,
        summary=summary[:4000],
        source="chat",
        confidence=payload.confidence,
        metadata_={"session_id": payload.session_id, **metadata},
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


@router.post("/promotions", response_model=MemoryFactOut)
async def propose_memory_promotion(
    payload: MemoryPromotionRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryFact:
    """Promote session evidence into a reviewable project-memory proposal."""
    source_fact: MemoryFact | None = None
    if payload.source_fact_id:
        source_fact = await db.get(MemoryFact, payload.source_fact_id)
        if not source_fact:
            raise HTTPException(status_code=404, detail="Source memory fact not found")
    title = payload.title or (source_fact.title if source_fact else None)
    summary = payload.summary or (source_fact.summary if source_fact else None)
    if not title or not summary:
        raise HTTPException(
            status_code=400,
            detail="title/summary are required when source_fact_id is not provided",
        )
    metadata = {
        "promotion_status": "pending",
        "source": "memory_promotion",
        **(payload.metadata or {}),
    }
    if source_fact:
        metadata.update({
            "source_fact_id": str(source_fact.id),
            "source_scope": source_fact.scope,
            "source_kind": source_fact.kind,
        })
    fact = MemoryFact(
        scope="project",
        kind="proposed_fact",
        title=title,
        summary=summary,
        source="memory_promotion",
        confidence=payload.confidence,
        pinned=False,
        metadata_=metadata,
    )
    db.add(fact)
    await db.commit()
    await db.refresh(fact)
    return fact


@router.get("/promotions", response_model=list[MemoryFactOut])
async def list_memory_promotions(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[MemoryFact]:
    status_to_kind = {
        "pending": "proposed_fact",
        "proposed": "proposed_fact",
        "approved": "verified_fact",
        "verified": "verified_fact",
        "rejected": "rejected_fact",
    }
    kinds = {"proposed_fact", "verified_fact", "rejected_fact"}
    if status:
        kind = status_to_kind.get(status)
        if not kind:
            raise HTTPException(status_code=400, detail="Unsupported promotion status")
        kinds = {kind}
    query = (
        select(MemoryFact)
        .where(
            MemoryFact.source == "memory_promotion",
            MemoryFact.kind.in_(sorted(kinds)),
        )
        .order_by(MemoryFact.created_at.desc())
        .limit(200)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/promotions/{fact_id}/evaluate", response_model=MemoryPromotionEvaluation)
async def evaluate_memory_promotion(
    fact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> MemoryPromotionEvaluation:
    """Evaluate a proposed project-memory fact before approval."""
    fact = await db.get(MemoryFact, fact_id)
    if not fact:
        raise HTTPException(status_code=404, detail="Memory promotion not found")
    if fact.kind not in {"proposed_fact", "verified_fact", "rejected_fact"}:
        raise HTTPException(status_code=400, detail="Memory fact is not a promotion")

    metadata = dict(fact.metadata_ or {})
    checks: list[dict] = []

    has_provenance = bool(metadata.get("source_fact_id") or metadata.get("source_document_id") or metadata.get("url"))
    checks.append({
        "name": "provenance",
        "passed": has_provenance,
        "message": "Has source_fact_id/source_document_id/url" if has_provenance else "Missing provenance",
    })

    summary_ok = 20 <= len((fact.summary or "").strip()) <= 4000
    checks.append({
        "name": "summary_length",
        "passed": summary_ok,
        "message": "Summary length is within bounds" if summary_ok else "Summary is too short or too long",
    })

    confidence_ok = fact.confidence >= 0.5
    checks.append({
        "name": "confidence",
        "passed": confidence_ok,
        "message": "Confidence is acceptable" if confidence_ok else "Confidence is below 0.5",
    })

    duplicate_query = select(MemoryFact).where(
        MemoryFact.id != fact.id,
        MemoryFact.scope == "project",
        MemoryFact.kind.in_(["verified_fact", "pinned_fact"]),
        MemoryFact.title == fact.title,
    ).limit(1)
    duplicate = (await db.execute(duplicate_query)).scalars().first()
    checks.append({
        "name": "duplicate_title",
        "passed": duplicate is None,
        "message": "No verified duplicate title" if duplicate is None else f"Duplicate: {duplicate.id}",
    })

    passed = all(bool(item["passed"]) for item in checks)
    diagnostics = [] if passed else [
        str(item["name"]) for item in checks if not item["passed"]
    ]
    metadata["last_evaluation"] = {
        "passed": passed,
        "checks": checks,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    fact.metadata_ = metadata
    await db.commit()
    await db.refresh(fact)

    return MemoryPromotionEvaluation(
        fact_id=fact.id,
        status=str(metadata.get("promotion_status") or fact.kind),
        passed=passed,
        checks=checks,
        diagnostics=diagnostics,
    )


@router.post("/promotions/{fact_id}/decide", response_model=MemoryFactOut)
async def decide_memory_promotion(
    fact_id: uuid.UUID,
    payload: MemoryPromotionDecision,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_human_role(UserRole.admin)),
) -> MemoryFact:
    fact = await db.get(MemoryFact, fact_id)
    if not fact:
        raise HTTPException(status_code=404, detail="Memory promotion not found")
    if fact.kind not in {"proposed_fact", "verified_fact", "rejected_fact"}:
        raise HTTPException(status_code=400, detail="Memory fact is not a promotion")
    metadata = dict(fact.metadata_ or {})
    metadata.update({
        "promotion_status": "approved" if payload.approved else "rejected",
        "decided_by": payload.decided_by,
        "decision_comment": payload.comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    })
    fact.metadata_ = metadata
    if payload.approved:
        fact.kind = "verified_fact"
        fact.pinned = True
        fact.last_verified_at = datetime.now(timezone.utc)
    else:
        fact.kind = "rejected_fact"
        fact.pinned = False
    await db.commit()
    await db.refresh(fact)
    return fact


@router.post("/sources/propose", response_model=MemoryFactOut)
async def propose_web_source(
    payload: WebSourceProposalRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryFact:
    """Register a reviewable external source for future web research."""
    metadata = {
        "source_status": "proposed",
        "url": payload.url,
        "supplier_name": payload.supplier_name,
        "source_type": payload.source_type,
        "rationale": payload.rationale,
        "domains": payload.domains or [],
        **(payload.metadata or {}),
    }
    fact = MemoryFact(
        scope="project",
        kind="web_source",
        title=payload.title,
        summary=payload.rationale or payload.url,
        source="web_source_registry",
        confidence=0.8,
        pinned=False,
        metadata_=metadata,
    )
    db.add(fact)
    await db.commit()
    await db.refresh(fact)
    return fact


@router.get("/sources", response_model=list[MemoryFactOut])
async def list_web_sources(
    status: str | None = None,
    source_type: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[MemoryFact]:
    query = select(MemoryFact).where(MemoryFact.kind == "web_source")
    if status:
        query = query.where(MemoryFact.metadata_["source_status"].as_string() == status)
    if source_type:
        query = query.where(MemoryFact.metadata_["source_type"].as_string() == source_type)
    result = await db.execute(query.order_by(MemoryFact.created_at.desc()).limit(200))
    return list(result.scalars().all())


def _source_discovery_query(payload: WebSourceDiscoveryRequest) -> str:
    if payload.query:
        return payload.query
    if not payload.supplier_name:
        raise HTTPException(
            status_code=400,
            detail="query or supplier_name is required",
        )
    source_hint = {
        "supplier_catalog": "официальный каталог",
        "price_list": "прайс лист",
        "manufacturer": "сайт производителя каталог",
        "reference": "официальный справочник",
    }.get(payload.source_type, payload.source_type)
    return f"{payload.supplier_name} {source_hint}"


def _domain_from_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


@router.post("/sources/discover", response_model=WebSourceDiscoveryResponse)
async def discover_web_sources(
    payload: WebSourceDiscoveryRequest,
    db: AsyncSession = Depends(get_db),
) -> WebSourceDiscoveryResponse:
    """Use configured web search to create reviewable source proposals."""
    query = _source_discovery_query(payload)
    search = await execute_web_search(
        WebSearchRequest(
            query=query,
            limit=payload.limit,
            recency_days=payload.recency_days,
            domains=payload.domains,
            intent="source_discovery",
        )
    )
    proposed: list[MemoryFact] = []
    skipped_existing = 0
    for result in search.results:
        existing = await db.scalar(
            select(MemoryFact.id)
            .where(
                MemoryFact.kind == "web_source",
                MemoryFact.metadata_["url"].as_string() == result.url,
            )
            .limit(1)
        )
        if existing:
            skipped_existing += 1
            continue
        domain = _domain_from_url(result.url)
        metadata = {
            "source_status": "proposed",
            "url": result.url,
            "supplier_name": payload.supplier_name,
            "source_type": payload.source_type,
            "rationale": payload.rationale
            or f"Discovered by web search for: {query}",
            "domains": [domain] if domain else [],
            "discovery_query": query,
            "discovery_provider": search.provider,
            "web_snippet": result.snippet,
            "published_at": result.published_at,
        }
        fact = MemoryFact(
            scope="project",
            kind="web_source",
            title=result.title or result.url,
            summary=result.snippet or result.url,
            source="web_source_discovery",
            confidence=0.6,
            pinned=False,
            metadata_=metadata,
        )
        db.add(fact)
        proposed.append(fact)
    await db.commit()
    for fact in proposed:
        await db.refresh(fact)
    diagnostics = list(search.diagnostics)
    if skipped_existing:
        diagnostics.append(f"skipped_existing:{skipped_existing}")
    return WebSourceDiscoveryResponse(
        query=query,
        provider=search.provider,
        proposed=proposed,
        skipped_existing=skipped_existing,
        diagnostics=diagnostics,
    )


@router.post("/sources/{source_id}/decide", response_model=MemoryFactOut)
async def decide_web_source(
    source_id: uuid.UUID,
    payload: WebSourceDecision,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_human_role(UserRole.admin)),
) -> MemoryFact:
    fact = await db.get(MemoryFact, source_id)
    if not fact or fact.kind != "web_source":
        raise HTTPException(status_code=404, detail="Web source proposal not found")
    metadata = dict(fact.metadata_ or {})
    metadata.update({
        "source_status": "approved" if payload.approved else "rejected",
        "decided_by": payload.decided_by,
        "decision_comment": payload.comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    })
    fact.metadata_ = metadata
    fact.pinned = bool(payload.approved)
    if payload.approved:
        fact.last_verified_at = datetime.now(timezone.utc)
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


def _fts_rank(column, query: str):
    """Return a ts_rank_cd ranking expression for a Russian-dictionary FTS match.

    Real lexical relevance (term frequency, proximity, document length) computed
    by Postgres — replaces the crude term-overlap of _simple_score. The absolute
    magnitude is irrelevant: RRF consumes only the rank order this produces.
    Falls back to a constant when FTS is unavailable (e.g. SQLite in tests).
    """
    try:
        tsq = func.plainto_tsquery("russian", query)
        vec = func.to_tsvector("russian", column)
        return func.ts_rank_cd(vec, tsq)
    except Exception:
        return None


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

    # ts_rank_cd over title+summary gives real lexical relevance; canonical_key
    # matches that the tsvector misses keep a term-overlap floor so they survive.
    rank_title = _fts_rank(KnowledgeNode.title, payload.query)
    rank_summary = _fts_rank(KnowledgeNode.summary, payload.query)
    if rank_title is not None and rank_summary is not None:
        rank_expr = rank_title + rank_summary
        node_rows = (await db.execute(
            node_query.add_columns(rank_expr.label("r"))
            .order_by(rank_expr.desc()).limit(payload.limit)
        )).all()
    else:
        node_rows = [
            (n, None) for n in (await db.execute(
                node_query.order_by(KnowledgeNode.created_at.desc()).limit(payload.limit)
            )).scalars().all()
        ]
    for node, r in node_rows:
        overlap = _simple_score(payload.query, " ".join([node.title, node.summary or ""]))
        score = max(float(r), overlap) if r is not None else overlap
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
    if payload.scope == "session":
        safe_scopes = [MemoryFact.scope.in_(["project", "global"])]
        if payload.session_id:
            safe_scopes.append(
                and_(
                    MemoryFact.scope == "session",
                    MemoryFact.metadata_["session_id"].as_string() == payload.session_id,
                )
            )
        query = query.where(or_(*safe_scopes))
    elif payload.scope:
        query = query.where(
            or_(MemoryFact.scope == payload.scope, MemoryFact.scope == "global")
        )
    else:
        query = query.where(MemoryFact.scope.in_(["project", "global"]))
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
            # Only fragment-level points — document-level vectors would crowd out
            # chunks/evidence in the shared collection (memory.search keeps only
            # chunk/evidence hits anyway).
            content_types=["document_chunk", "evidence_span"],
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
        # Skip degenerate rerankers: empty scores, or a constant value across all
        # documents (e.g. a reranker GGUF that returns zero vectors → 0.5 for
        # everything). Applying these via max() would only inflate/flatten the RRF
        # ranking, never improve it.
        usable = [float(s) for s in scores[: len(hits)]]
        if not usable or (max(usable) - min(usable)) < 1e-6:
            return hits
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


def _rrf_fuse(hits: list[MemorySearchHit]) -> list[MemorySearchHit]:
    """Fuse text / vector / graph branches via Reciprocal Rank Fusion.

    Each branch is ranked independently by its own raw score; a hit's fused
    score is Σ weight / (k + rank). Because only rank order matters, the
    branches' incompatible score scales (cosine, ts_rank_cd, term overlap)
    never need calibration. The result is min-max normalised to [0, 1] so the
    downstream reranker (0–1) and exact-match boosts stay on a comparable scale.
    """
    # 1. Dedupe within each branch, keeping the best per-branch raw score.
    branch_best: dict[str, dict[tuple[str, uuid.UUID], float]] = {
        "text": {}, "vector": {}, "graph": {},
    }
    for hit in hits:
        key = (hit.kind, hit.id)
        for branch, value in (
            ("text", hit.text_score),
            ("vector", hit.vector_score),
            ("graph", hit.graph_score),
        ):
            if value is None:
                continue
            prev = branch_best[branch].get(key)
            if prev is None or value > prev:
                branch_best[branch][key] = float(value)

    # 2. Rank within each branch and accumulate weighted RRF contributions.
    rrf: dict[tuple[str, uuid.UUID], float] = {}
    for branch, scores in branch_best.items():
        weight = _RRF_BRANCH_WEIGHTS.get(branch, 1.0)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        for rank, (key, _score) in enumerate(ranked):
            rrf[key] = rrf.get(key, 0.0) + weight / (_RRF_K + rank + 1)

    # 3. Normalise to [0, 1] for scale-compatibility with rerank/boosts.
    max_rrf = max(rrf.values()) if rrf else 0.0
    merged = _merge_memory_hits(hits)
    fused: list[MemorySearchHit] = []
    for hit in merged:
        raw = rrf.get((hit.kind, hit.id), 0.0)
        norm = round(raw / max_rrf, 6) if max_rrf > 0 else 0.0
        fused.append(hit.model_copy(update={"score": norm}))
    return fused


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
    """Lexical retrieval over chunks, evidence spans and chat messages.

    Ranks by Postgres ts_rank_cd (real lexical relevance) when available,
    falling back to recency + term-overlap on engines without FTS (SQLite).
    text_score carries the lexical signal consumed by RRF fusion.
    """
    if remaining <= 0:
        return []
    hits: list[MemorySearchHit] = []

    # ── Document chunks ──
    rank_expr = _fts_rank(DocumentChunk.text, payload.query)
    chunk_cond = _fts_condition(DocumentChunk.text, payload.query)
    if rank_expr is not None:
        stmt = select(DocumentChunk, rank_expr.label("r")).where(chunk_cond)
        if payload.document_id:
            stmt = stmt.where(DocumentChunk.document_id == payload.document_id)
        rows = (await db.execute(stmt.order_by(rank_expr.desc()).limit(remaining))).all()
        for chunk, r in rows:
            score = float(r or 0.0)
            hits.append(MemorySearchHit(
                kind="chunk", id=chunk.id,
                title=f"Document chunk #{chunk.chunk_index}",
                summary=chunk.text[:500], score=score, source="sql",
                text_score=score, source_document_id=chunk.document_id,
            ))
    else:
        stmt = select(DocumentChunk).where(chunk_cond)
        if payload.document_id:
            stmt = stmt.where(DocumentChunk.document_id == payload.document_id)
        for chunk in (await db.execute(
            stmt.order_by(DocumentChunk.created_at.desc()).limit(remaining)
        )).scalars().all():
            score = _simple_score(payload.query, chunk.text)
            hits.append(MemorySearchHit(
                kind="chunk", id=chunk.id,
                title=f"Document chunk #{chunk.chunk_index}",
                summary=chunk.text[:500], score=score, source="sql",
                text_score=score, source_document_id=chunk.document_id,
            ))

    # ── Evidence spans ──
    remaining = remaining - len(hits)
    if remaining > 0:
        rank_expr = _fts_rank(EvidenceSpan.text, payload.query)
        ev_cond = _fts_condition(EvidenceSpan.text, payload.query)
        if rank_expr is not None:
            stmt = select(EvidenceSpan, rank_expr.label("r")).where(ev_cond)
            if payload.document_id:
                stmt = stmt.where(EvidenceSpan.document_id == payload.document_id)
            ev_rows = (await db.execute(stmt.order_by(rank_expr.desc()).limit(remaining))).all()
        else:
            stmt = select(EvidenceSpan).where(ev_cond)
            if payload.document_id:
                stmt = stmt.where(EvidenceSpan.document_id == payload.document_id)
            ev_rows = [
                (e, None) for e in (await db.execute(
                    stmt.order_by(EvidenceSpan.created_at.desc()).limit(remaining)
                )).scalars().all()
            ]
        for evidence, r in ev_rows:
            score = float(r) if r is not None else _simple_score(payload.query, evidence.text)
            hits.append(MemorySearchHit(
                kind="evidence", id=evidence.id,
                title=evidence.field_name or "Evidence span",
                summary=evidence.text[:500], score=score, source="sql",
                text_score=score, source_document_id=evidence.document_id,
                evidence=EvidenceSpanOut.model_validate(evidence),
            ))

    # ── Chat messages ──
    remaining = remaining - len(hits)
    if remaining > 0:
        rank_expr = _fts_rank(ChatMessage.content, payload.query)
        chat_cond = _fts_condition(ChatMessage.content, payload.query)
        base_where = [ChatMessage.role.in_(["user", "assistant"]), chat_cond]
        if rank_expr is not None:
            stmt = select(ChatMessage, rank_expr.label("r")).where(*base_where)
            order_col = rank_expr.desc()
        else:
            stmt = select(ChatMessage).where(*base_where)
            order_col = ChatMessage.created_at.desc()
        if payload.document_id:
            stmt = stmt.where(
                ChatMessage.id.in_(
                    select(ChatMessageAttachment.message_id).where(
                        ChatMessageAttachment.document_id == payload.document_id,
                        ChatMessageAttachment.message_id.is_not(None),
                    )
                )
            )
        raw = (await db.execute(stmt.order_by(order_col).limit(remaining))).all()
        chat_rows = raw if rank_expr is not None else [(m,) for (m,) in raw]
        for row in chat_rows:
            message = row[0]
            r = row[1] if rank_expr is not None else None
            content = message.content or ""
            score = float(r) if r is not None else _simple_score(payload.query, content)
            hits.append(MemorySearchHit(
                kind="chat_message", id=message.id,
                title=f"Chat {message.role}",
                summary=content[:500], score=score, source="chat", text_score=score,
            ))
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
            scope=payload.scope,
            session_id=payload.session_id,
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


@router.post("/query", response_model=MemoryQueryResponse)
async def query_memory(
    payload: MemoryQueryRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryQueryResponse:
    """Agent-facing RAG contract: compact evidence pack plus optional graph context."""
    explained = await explain_memory(
        MemoryExplainRequest(
            query=payload.query,
            document_id=payload.document_id,
            node_types=payload.node_types,
            scope=payload.scope,
            session_id=payload.session_id,
            limit=payload.limit,
            neighborhood_depth=payload.neighborhood_depth,
            include_explain=True,
        ),
        db,
    )
    evidence_pack: list[MemoryEvidenceItem] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for hit in explained.hits:
        key = (hit.kind, hit.id)
        if key in seen:
            continue
        seen.add(key)
        evidence_pack.append(
            MemoryEvidenceItem(
                kind=hit.kind,
                id=hit.id,
                title=hit.title,
                summary=hit.summary,
                source=hit.source,
                score=hit.score,
                source_document_id=hit.source_document_id,
                evidence_text=hit.evidence.text if hit.evidence else None,
                evidence_page=hit.evidence.page_number if hit.evidence else None,
            )
        )
    return MemoryQueryResponse(
        query=payload.query,
        evidence_pack=evidence_pack,
        graph_nodes=explained.nodes if payload.include_graph else [],
        graph_edges=explained.edges if payload.include_graph else [],
        diagnostics=[] if evidence_pack else ["memory_query_empty"],
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
        if not chunk:
            return None
        # Contextual Retrieval: prepend the document context prefix so reindex
        # produces the same enriched vector as the live embedding task.
        prefix = (chunk.context_prefix or "").strip()
        return f"{prefix}\n\n{chunk.text}" if prefix else chunk.text
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
            tools_label = ", ".join(req.tools_used[:3])
            await _httpx.AsyncClient(timeout=5.0).post(
                f"{_gw.backend_url}/api/technology/learning-rules",
                json={
                    "rule_type": "agent_feedback",
                    "entity_type": "agent_behavior",
                    "field_name": tools_label or "unknown_tool",
                    "match_old_value": tools_label,
                    "replacement_value": req.comment,
                    "status": "proposed",
                    "suggested_by": "user_rating",
                    "metadata": {
                        "trigger_tools": req.tools_used,
                        "observation": req.comment,
                        "suggested_action": "review",
                        "session_id": req.session_id,
                    },
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
