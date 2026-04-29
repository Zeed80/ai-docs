"""Backfill embeddings for all existing documents into Qdrant."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


async def main() -> None:
    import structlog
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.session import _get_session_factory
    from app.db.models import Document, DocumentExtraction
    from app.ai.embeddings import build_document_text, embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import (
        collection_count_for,
        ensure_collection,
        upsert_document,
    )

    logger = structlog.get_logger()

    profile = get_active_embedding_profile()
    ensure_collection(
        collection_name=profile.collection_name,
        vector_size=profile.dimension,
        distance_metric=profile.distance_metric,
    )
    logger.info("backfill_start", collection=profile.collection_name, model=profile.model_key)

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Document).options(
                selectinload(Document.extractions).selectinload(DocumentExtraction.fields)
            )
        )
        docs = result.scalars().all()

    logger.info("backfill_total", count=len(docs))

    ok = 0
    errors = 0
    for i, doc in enumerate(docs, 1):
        try:
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

            ok += 1
            if i % 10 == 0 or i == len(docs):
                print(f"[{i}/{len(docs)}] embedded {ok} ok, {errors} errors")

        except Exception as e:
            errors += 1
            logger.error("backfill_doc_error", doc_id=str(doc.id), error=str(e))

    final_count = collection_count_for(profile.collection_name)
    print(f"\nDone: {ok} embedded, {errors} errors. Qdrant {profile.collection_name} total: {final_count}")


if __name__ == "__main__":
    # Set env vars if not set
    import dotenv
    try:
        dotenv.load_dotenv()
    except Exception:
        pass

    asyncio.run(main())
