"""Celery tasks for document embedding pipeline."""

import asyncio

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="app.tasks.embedding.embed_document", bind=True, max_retries=3)
def embed_document(self, document_id: str) -> dict:
    """Embed a document and store vector in Qdrant."""
    try:
        return asyncio.run(_embed_document(document_id))
    except Exception as exc:
        logger.error("embed_failed", doc_id=document_id, error=str(exc))
        raise self.retry(exc=exc, countdown=30)


async def _embed_document(document_id: str) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.ai.embeddings import build_document_text, embed_text, get_active_embedding_profile
    from app.db.models import Document, DocumentExtraction
    from app.db.session import _get_engine, _get_session_factory
    from app.vector.qdrant_store import ensure_collection, upsert_document

    profile = get_active_embedding_profile()
    ensure_collection(
        collection_name=profile.collection_name,
        vector_size=profile.dimension,
        distance_metric=profile.distance_metric,
    )

    _get_engine.cache_clear()
    _get_session_factory.cache_clear()
    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .options(
                selectinload(Document.extractions).selectinload(DocumentExtraction.fields)
            )
        )
        doc = result.scalar_one_or_none()
        if not doc:
            logger.warning("embed_doc_not_found", doc_id=document_id)
            return {"status": "not_found"}

        # Build text from file_name + latest extraction fields
        extraction_fields = []
        if doc.extractions:
            latest = sorted(doc.extractions, key=lambda e: e.created_at, reverse=True)[0]
            extraction_fields = [
                {
                    "field_name": f.field_name,
                    "field_value": f.field_value,
                    "corrected_value": f.corrected_value,
                }
                for f in latest.fields
            ]

        text = build_document_text(
            doc.file_name,
            doc.doc_type.value if doc.doc_type else None,
            extraction_fields,
        )

        vector = await embed_text(text, profile)

        upsert_document(
            str(doc.id),
            vector,
            file_name=doc.file_name,
            doc_type=doc.doc_type.value if doc.doc_type else None,
            status=doc.status.value if doc.status else "ingested",
            source_channel=doc.source_channel,
            collection_name=profile.collection_name,
            embedding_model=profile.model_key,
        )

    logger.info(
        "document_embedded",
        doc_id=document_id,
        text_len=len(text),
        collection=profile.collection_name,
        model=profile.model_key,
    )
    return {
        "status": "ok",
        "doc_id": document_id,
        "text_len": len(text),
        "collection": profile.collection_name,
        "embedding_model": profile.model_key,
    }
