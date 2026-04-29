"""Search API — skills: doc.search, search.nl_to_query, search.hybrid, search.similar"""

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentStatus,
    DocumentType,
    ExtractionField,
)
from app.db.session import get_db
from app.db.text_search import text_search_condition, text_search_rank
from app.domain.documents import DocumentSummary

router = APIRouter()
logger = structlog.get_logger()


# ── Schemas ─────────────────────────────────────────────────────────────────


class NLQueryRequest(BaseModel):
    query: str
    limit: int = 20


class StructuredFilter(BaseModel):
    doc_type: str | None = None
    status: str | None = None
    source_channel: str | None = None
    search_text: str | None = None
    supplier_name: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None


class NLQueryResponse(BaseModel):
    original_query: str
    structured_filter: StructuredFilter
    results: list[DocumentSummary]
    total: int
    interpretation: str


class HybridSearchRequest(BaseModel):
    query: str
    doc_type: str | None = None
    status: str | None = None
    limit: int = 20


# ── doc.search ──────────────────────────────────────────────────────────────


@router.post("/documents", response_model=list[DocumentSummary])
async def search_documents(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.search — Hybrid search: Postgres FTS + ILIKE fallback."""
    columns = [
        Document.file_name,
        DocumentChunk.text,
        ExtractionField.field_name,
        ExtractionField.field_value,
    ]
    rank = text_search_rank(db, columns, q)
    query = (
        select(Document)
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .outerjoin(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .outerjoin(ExtractionField, ExtractionField.extraction_id == DocumentExtraction.id)
        .where(text_search_condition(db, columns, q))
        .distinct()
    )
    if rank is not None:
        query = query.order_by(desc(rank), Document.created_at.desc())
    else:
        query = query.order_by(Document.created_at.desc())
    result = await db.execute(query.limit(limit))
    return result.scalars().all()


# ── search.nl_to_query ──────────────────────────────────────────────────────


NL_TO_STRUCTURED_SYSTEM = """You are a search query parser for a manufacturing document management system.
Convert natural language queries in Russian to structured filters. Respond in JSON only."""

NL_TO_STRUCTURED_PROMPT = """Convert this search query to structured filters:

Query: "{query}"

Available filters:
- doc_type: invoice, letter, contract, drawing, commercial_offer, act, waybill, other
- status: ingested, classifying, extracting, needs_review, approved, rejected, archived
- source_channel: email, upload, chat
- search_text: free text search in filenames
- supplier_name: supplier/company name
- date_from: YYYY-MM-DD
- date_to: YYYY-MM-DD
- min_amount: minimum invoice total
- max_amount: maximum invoice total

Respond with JSON:
{{
  "doc_type": "<type or null>",
  "status": "<status or null>",
  "source_channel": "<channel or null>",
  "search_text": "<text or null>",
  "supplier_name": "<name or null>",
  "date_from": "<date or null>",
  "date_to": "<date or null>",
  "min_amount": <number or null>,
  "max_amount": <number or null>,
  "interpretation": "<human-readable interpretation in Russian>"
}}"""


@router.post("/nl", response_model=NLQueryResponse)
async def nl_to_query(
    payload: NLQueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: search.nl_to_query — Convert natural language to structured query."""
    # Try AI parsing first, fallback to simple text search
    structured = StructuredFilter()
    interpretation = f"Поиск: {payload.query}"

    try:
        from app.ai.router import ai_router

        result = await ai_router.nl_to_query(payload.query, schema={"prompt": NL_TO_STRUCTURED_PROMPT})

        values = result.get("filters", result)
        structured = StructuredFilter(
            doc_type=values.get("doc_type"),
            status=values.get("status"),
            source_channel=values.get("source_channel"),
            search_text=values.get("search_text"),
            supplier_name=values.get("supplier_name"),
            date_from=values.get("date_from"),
            date_to=values.get("date_to"),
            min_amount=values.get("min_amount"),
            max_amount=values.get("max_amount"),
        )
        interpretation = result.get("interpretation", interpretation)

    except Exception as e:
        logger.warning("nl_parse_failed", error=str(e), query=payload.query)
        # Fallback: use raw query as search_text
        structured = StructuredFilter(search_text=payload.query)

    # Execute structured query
    query = select(Document)
    if structured.doc_type:
        try:
            query = query.where(Document.doc_type == DocumentType(structured.doc_type))
        except ValueError:
            pass
    if structured.status:
        try:
            query = query.where(Document.status == DocumentStatus(structured.status))
        except ValueError:
            pass
    if structured.source_channel:
        query = query.where(Document.source_channel == structured.source_channel)
    if structured.search_text:
        query = query.where(text_search_condition(db, [Document.file_name], structured.search_text))

    # Count
    from sqlalchemy import func

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Fetch
    query = query.order_by(Document.created_at.desc()).limit(payload.limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return NLQueryResponse(
        original_query=payload.query,
        structured_filter=structured,
        results=items,
        total=total,
        interpretation=interpretation,
    )


# ── search.hybrid ───────────────────────────────────────────────────────────


@router.post("/hybrid", response_model=NLQueryResponse)
async def hybrid_search(
    payload: HybridSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: search.hybrid — Vector similarity search via Qdrant + SQL filter fallback."""
    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import collection_count_for, search_similar

    items = []
    total = 0
    used_vector = False

    # Try vector search if Qdrant has data
    profile = get_active_embedding_profile()
    qdrant_count = collection_count_for(profile.collection_name)
    if qdrant_count > 0 and payload.query:
        try:
            query_vec = await embed_text(payload.query, profile)
            hits = search_similar(
                query_vec,
                limit=payload.limit,
                doc_type=payload.doc_type,
                status=payload.status,
                score_threshold=0.3,
                collection_name=profile.collection_name,
            )
            if hits:
                doc_ids = [h["doc_id"] for h in hits]
                from sqlalchemy import case
                from uuid import UUID

                uuid_ids = [UUID(did) for did in doc_ids]
                ordering = case(
                    {uid: idx for idx, uid in enumerate(uuid_ids)},
                    value=Document.id,
                )
                result = await db.execute(
                    select(Document)
                    .where(Document.id.in_(uuid_ids))
                    .order_by(ordering)
                )
                items = result.scalars().all()
                total = len(items)
                used_vector = True
                logger.info(
                    "hybrid_vector_search",
                    hits=len(hits),
                    qdrant_count=qdrant_count,
                    collection=profile.collection_name,
                    model=profile.model_key,
                )
        except Exception as e:
            logger.warning("hybrid_vector_failed", error=str(e))

    # Fallback: SQL ILIKE
    if not used_vector:
        query = select(Document)
        if payload.query:
            query = query.where(Document.file_name.ilike(f"%{payload.query}%"))
        if payload.doc_type:
            try:
                query = query.where(Document.doc_type == DocumentType(payload.doc_type))
            except ValueError:
                pass
        if payload.status:
            try:
                query = query.where(Document.status == DocumentStatus(payload.status))
            except ValueError:
                pass

        count_q = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_q)).scalar() or 0
        query = query.order_by(Document.created_at.desc()).limit(payload.limit)
        result = await db.execute(query)
        items = result.scalars().all()

    suffix = " [вектор]" if used_vector else " [текст]"
    return NLQueryResponse(
        original_query=payload.query,
        structured_filter=StructuredFilter(
            doc_type=payload.doc_type,
            status=payload.status,
            search_text=payload.query,
        ),
        results=items,
        total=total,
        interpretation=f"Поиск: {payload.query}"
        + (f", тип: {payload.doc_type}" if payload.doc_type else "")
        + (f", статус: {payload.status}" if payload.status else "")
        + suffix,
    )
