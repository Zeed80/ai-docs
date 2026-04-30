"""Celery tasks for document embedding pipeline."""

import asyncio
import uuid

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
    from app.db.models import Document, DocumentExtraction, DocumentProcessingJob
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

        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == uuid.UUID(document_id))
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job:
            steps = [
                {**step, "status": "running"} if step.get("key") == "embedding" else step
                for step in (job.pipeline_steps or [])
            ]
            job.pipeline_steps = steps
            if job.status != "done":
                job.current_step = "embedding"
            await db.commit()

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

        try:
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
        except Exception as exc:
            if job:
                job.pipeline_steps = [
                    (
                        {**step, "status": "failed", "error": str(exc)}
                        if step.get("key") == "embedding"
                        else step
                    )
                    for step in (job.pipeline_steps or [])
                ]
                job.status = "failed"
                job.error = str(exc)
                await db.commit()
            raise

        if job:
            await db.refresh(job)
            job.pipeline_steps = [
                {**step, "status": "done"} if step.get("key") == "embedding" else step
                for step in (job.pipeline_steps or [])
            ]
            if job.status != "running":
                job.current_step = "completed"
            if all(
                step.get("status") in {"done", "skipped"}
                for step in (job.pipeline_steps or [])
            ):
                job.status = "done"
                job.current_step = "completed"
            await db.commit()

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
