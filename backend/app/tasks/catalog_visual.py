"""Visual index of catalog positions: the crops go into a shared image+text
vector space (infra/vl-embedding), so a person can find a tool by a PHOTO.

Shape of the work follows catalog_pages.py: short self-rescheduling batches
with the checkpoint in the data itself. Here the checkpoint is the position's
``metadata_.visual_indexed_at`` — a batch commits what it embedded, so a worker
restart costs one batch instead of the whole catalog, and re-running the task
resumes rather than re-embedding 4 000 pictures.

Degraded by design: when the sidecar is down the task stops and says so. It
does NOT fall back to a text-only vector — an image search silently answering
from text is exactly the kind of quiet substitution that makes a feature look
like it works.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

# One embed call holds the GPU for its duration; a small batch keeps catalog
# indexing from blocking an interactive search behind it.
EMBED_BATCH = 8
# Per task run, then the task re-queues itself. Bounded so one huge catalog
# cannot occupy the worker slot for an hour.
BATCH_PER_RUN = 64
INDEXED_KEY = "visual_indexed_at"
MODEL_KEY = "visual_model"


def _run_async(coro):
    from app.tasks.drawing_analysis import run_async

    return run_async(coro)


async def _session():
    from app.db.session import _get_session_factory

    return _get_session_factory()


@celery_app.task(
    bind=True,
    name="catalog.visual_index_batch",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1860,
)
def visual_index_batch(self, document_id: str | None = None, model: str | None = None) -> dict:
    """Embed the next batch of catalog pictures, then re-queue itself."""
    return _run_async(_visual_index_async(document_id, model))


async def _visual_index_async(document_id: str | None, model: str | None) -> dict:
    from app.ai.vl_embeddings import DOCUMENT_PROMPT, embed_multimodal, vl_info
    from app.db.models import ToolCatalogEntry
    from app.storage import download_file
    from app.vector.qdrant_store import (
        ensure_visual_catalog_collection,
        upsert_visual_catalog_entries,
    )

    info = await vl_info()
    if not info:
        logger.warning("catalog_visual_sidecar_unavailable")
        return {"status": "unavailable", "indexed": 0}
    model_name = model or info.get("model") or "unknown"
    dim = int(info.get("dim") or 0)
    if dim <= 0:
        return {"status": "bad_info", "indexed": 0}
    await asyncio.to_thread(ensure_visual_catalog_collection, dim)

    factory = await _session()
    async with factory() as db:
        query = (
            select(ToolCatalogEntry)
            .where(
                ToolCatalogEntry.is_active.is_(True),
                ToolCatalogEntry.image_path.isnot(None),
                # Not yet indexed, or indexed by a different model — changing
                # the model must re-index rather than leave a mixed collection.
                #
                # NOT has_key(): metadata_ is a json column, not jsonb, and
                # has_key/astext are jsonb-only operators that raise here (the
                # same trap the catalog anomaly rule hit). Reading the key and
                # comparing to NULL works on both.
                or_(
                    ToolCatalogEntry.metadata_.is_(None),
                    ToolCatalogEntry.metadata_[INDEXED_KEY].as_string().is_(None),
                    ToolCatalogEntry.metadata_[MODEL_KEY].as_string() != model_name,
                ),
            )
            .order_by(ToolCatalogEntry.created_at)
            .limit(BATCH_PER_RUN)
        )
        entries = (await db.execute(query)).scalars().all()

        if not entries:
            logger.info("catalog_visual_index_complete", document=document_id)
            return {"status": "complete", "indexed": 0}

        indexed = 0
        skipped = 0
        for start in range(0, len(entries), EMBED_BATCH):
            chunk = entries[start : start + EMBED_BATCH]
            items: list[dict[str, Any]] = []
            usable: list[Any] = []
            for entry in chunk:
                try:
                    image = await asyncio.to_thread(download_file, entry.image_path)
                except Exception as exc:  # noqa: BLE001 — a lost file is not fatal
                    logger.debug(
                        "catalog_visual_image_missing",
                        entry=str(entry.id),
                        error=str(exc)[:120],
                    )
                    skipped += 1
                    continue
                # Picture AND words together: the shared space lets one vector
                # carry both, and a bare crop of a drill looks like every other
                # drill until the article code is part of it.
                caption = " ".join(
                    part
                    for part in (entry.part_number, entry.name, entry.description)
                    if part
                )
                items.append({"text": caption, "image": image})
                usable.append(entry)

            if not items:
                continue
            vectors = await embed_multimodal(items, prompt=DOCUMENT_PROMPT)
            if vectors is None:
                # Stop rather than mark anything done: the remaining positions
                # stay unindexed and the next run picks them up.
                await db.commit()
                logger.warning("catalog_visual_embed_failed", indexed=indexed)
                return {"status": "embed_failed", "indexed": indexed, "skipped": skipped}

            points = []
            now = datetime.now(UTC).isoformat()
            for entry, vector in zip(usable, vectors):
                points.append(
                    {
                        "entry_id": str(entry.id),
                        "vector": vector,
                        "name": entry.name,
                        "part_number": entry.part_number,
                        "supplier_id": str(entry.supplier_id) if entry.supplier_id else "",
                        "catalog_document_id": (
                            str(entry.source_document_id) if entry.source_document_id else ""
                        ),
                        "catalog_page": entry.catalog_page,
                        "tool_type": entry.tool_type.value if entry.tool_type else "",
                        "image_kind": entry.image_kind or "",
                        "is_active": True,
                        "embedding_model": model_name,
                    }
                )
                meta = dict(entry.metadata_) if isinstance(entry.metadata_, dict) else {}
                meta[INDEXED_KEY] = now
                meta[MODEL_KEY] = model_name
                entry.metadata_ = meta
                flag_modified(entry, "metadata_")

            await asyncio.to_thread(upsert_visual_catalog_entries, points)
            indexed += len(points)

        await db.commit()

        remaining = await db.scalar(
            select(func.count())
            .select_from(ToolCatalogEntry)
            .where(
                ToolCatalogEntry.is_active.is_(True),
                ToolCatalogEntry.image_path.isnot(None),
                or_(
                    ToolCatalogEntry.metadata_.is_(None),
                    ToolCatalogEntry.metadata_[INDEXED_KEY].as_string().is_(None),
                    ToolCatalogEntry.metadata_[MODEL_KEY].as_string() != model_name,
                ),
            )
        )

    logger.info(
        "catalog_visual_indexed", indexed=indexed, skipped=skipped, remaining=remaining
    )
    if remaining:
        visual_index_batch.apply_async(args=[document_id, model], countdown=1)
    return {
        "status": "running" if remaining else "complete",
        "indexed": indexed,
        "skipped": skipped,
        "remaining": int(remaining or 0),
        "model": model_name,
    }


@celery_app.task(bind=True, name="catalog.visual_index_status", max_retries=0)
def visual_index_status(self) -> dict:
    """How much of the catalog is searchable by picture — for the settings UI."""
    return _run_async(_visual_status_async())


async def _visual_status_async() -> dict:
    from app.ai.vl_embeddings import vl_info
    from app.db.models import ToolCatalogEntry

    info = await vl_info()
    factory = await _session()
    async with factory() as db:
        with_image = await db.scalar(
            select(func.count())
            .select_from(ToolCatalogEntry)
            .where(
                ToolCatalogEntry.is_active.is_(True),
                ToolCatalogEntry.image_path.isnot(None),
            )
        )
        indexed = await db.scalar(
            select(func.count())
            .select_from(ToolCatalogEntry)
            .where(
                ToolCatalogEntry.is_active.is_(True),
                ToolCatalogEntry.image_path.isnot(None),
                ToolCatalogEntry.metadata_[INDEXED_KEY].as_string().isnot(None),
            )
        )
    return {
        "sidecar": info,
        "with_image": int(with_image or 0),
        "indexed": int(indexed or 0),
    }


@celery_app.task(bind=True, name="catalog.visual_deindex", max_retries=1)
def visual_deindex(self, entry_ids: list[str]) -> dict:
    """Drop visual vectors for positions that no longer exist."""
    from app.vector.qdrant_store import delete_visual_catalog_entries

    delete_visual_catalog_entries([str(uuid.UUID(str(i))) for i in entry_ids])
    return {"deleted": len(entry_ids)}
