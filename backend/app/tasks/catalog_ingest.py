"""Supplier-catalog ingestion with visible stages.

Runs on its own ``catalog`` queue: a 200-page price list is minutes of parsing
plus one embedding per row, and it used to sit in the single-slot ``gpu`` queue
behind OCR and diffusion — one catalog blocked document recognition for the
whole company, and vice versa.

Stages are reported into the same DocumentProcessingJob the document pipeline
uses, with the catalog stage list; the frontend renders `label ?? key`, so no UI
change is needed to show them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog

from app.domain import processing_jobs as pj
from app.domain.catalog_documents import ARCHIVE_SUFFIXES
from app.domain.pipeline import CATALOG_PIPELINE_STEP_DEFINITIONS as CATALOG_STEPS
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(
    bind=True,
    name="catalog.ingest_file",
    max_retries=1,
    soft_time_limit=3600,
    time_limit=3660,
)
def ingest_catalog_document(self, document_id: str) -> dict:
    from app.tasks.drawing_analysis import run_async

    return run_async(_ingest_document_async(document_id, self.request.id))


async def _ingest_document_async(document_id: str, celery_task_id: str | None) -> dict:
    from sqlalchemy import select

    from app.db.models import Document, DocumentProcessingJob, ToolSupplier
    from app.db.session import _get_session_factory
    from app.tasks.drawing_analysis import (
        _create_catalog_entries_from_rows,
        _load_catalog_file,
        _parse_catalog_file,
    )
    from app.vector.qdrant_store import ensure_drawing_collections

    doc_uuid = uuid.UUID(document_id)
    factory = _get_session_factory()

    async with factory() as db:
        doc = await db.get(Document, doc_uuid)
        if not doc:
            return {"error": f"Document {document_id} not found"}
        supplier_id = (doc.metadata_ or {}).get("tool_supplier_id")
        if not supplier_id:
            return {"error": "Document is not linked to a tool supplier"}
        supplier_uuid = uuid.UUID(supplier_id)
        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == doc_uuid)
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        storage_path = doc.storage_path
        filename = doc.file_name

        if job is None:
            job = DocumentProcessingJob(
                document_id=doc_uuid,
                status="running",
                pipeline_steps=[
                    {"key": k, "label": lbl, "status": "done" if k == "store" else "pending"}
                    for k, lbl in CATALOG_STEPS
                ],
                current_step="parse",
            )
            db.add(job)
            await db.flush()
        job.status = "running"
        job.celery_task_id = celery_task_id or job.celery_task_id
        pj.set_job_step(job, "store", "done", defs=CATALOG_STEPS)
        await db.commit()
        job_id = job.id

    suffix = Path(filename).suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        # Archives are unpacked by their own task (Э3); each member becomes a
        # child Document with its own job, so this one only fans out.
        from app.tasks.catalog_archive import unpack_catalog_archive

        await _update_step(factory, job_id, "unpack", "running")
        unpack_catalog_archive.delay(document_id)
        return {"document_id": document_id, "status": "unpacking"}

    await _update_step(factory, job_id, "unpack", "skipped")

    if suffix == ".pdf":
        # A PDF goes through the page-wise pipeline: every page is rendered,
        # parsed and kept as a row, so the run is resumable, each position knows
        # its page, and product pictures can be cropped. The old whole-file path
        # read a sample of pages and could do none of that.
        from app.tasks.catalog_pages import prepare_catalog_pages

        await _update_step(factory, job_id, "pages", "running")
        prepare_catalog_pages.delay(document_id)
        return {"document_id": document_id, "status": "pages"}

    await _update_step(factory, job_id, "parse", "running")

    catalog_bytes = await _load_catalog_file(storage_path)
    if not catalog_bytes:
        await _fail(factory, job_id, "parse", "Файл каталога не удалось загрузить из хранилища")
        return {"error": "Could not load catalog file"}

    try:
        rows = await _parse_catalog_file(catalog_bytes, suffix, filename)
    except Exception as exc:  # noqa: BLE001 — the job must record why
        await _fail(factory, job_id, "parse", f"Разбор не удался: {exc}"[:400])
        raise

    await _update_step(factory, job_id, "pages", "skipped")
    await _update_step(factory, job_id, "images", "skipped")
    await _update_step(factory, job_id, "parse", "done", rows_parsed=len(rows))
    await _update_step(factory, job_id, "normalize", "done", rows_parsed=len(rows))

    if not rows:
        # An empty result is a real outcome, not a success: say so on the job
        # instead of finishing green with zero entries (the old silent zero).
        await _update_step(factory, job_id, "entries", "done", created=0)
        await _finish(
            factory,
            job_id,
            "done",
            warning="Из файла не удалось извлечь ни одной строки каталога.",
        )
        return {"document_id": document_id, "entries_created": 0, "rows_parsed": 0}

    ensure_drawing_collections()
    await _update_step(factory, job_id, "entries", "running")

    async with factory() as db:
        # Re-ingesting the same file replaces its own entries only; entries
        # from the supplier's other catalogs are untouched.
        from sqlalchemy import text as sa_text
        from sqlalchemy import update as sa_update

        from app.db.models import ToolCatalogEntry

        # Serialise per document. Measured on the stand: the same task reached
        # two workers, each deactivated the old rows inside its own uncommitted
        # transaction and then inserted a full set — the catalog came out
        # doubled. The lock is transaction-scoped and released by the commit.
        await db.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"catalog_ingest:{doc_uuid}"},
        )
        await db.execute(
            sa_update(ToolCatalogEntry)
            .where(
                ToolCatalogEntry.source_document_id == doc_uuid,
                ToolCatalogEntry.is_active.is_(True),
            )
            .values(is_active=False)
        )
        supplier = await db.get(ToolSupplier, supplier_uuid)
        if not supplier:
            await _fail(factory, job_id, "entries", "Поставщик не найден")
            return {"error": f"Supplier {supplier_uuid} not found"}
        result = await _create_catalog_entries_from_rows(
            db,
            supplier_uuid,
            rows,
            source_document_id=doc_uuid,
            infer_types_with_llm=True,
        )
        await db.commit()

    await _update_step(
        factory,
        job_id,
        "entries",
        "done",
        created=result["created"],
        skipped=result["skipped"],
        skipped_by_reason=result.get("skipped_by_reason") or {},
    )
    canonical_stats = await _map_canonical_items(factory, doc_uuid)
    await _update_step(
        factory,
        job_id,
        "canonical",
        "done",
        **canonical_stats,
    )
    await _update_step(factory, job_id, "embedding", "done")
    await _update_step(factory, job_id, "graph", "done")
    await _finish(factory, job_id, "done")

    logger.info(
        "catalog_document_ingested",
        document_id=document_id,
        supplier_id=str(supplier_uuid),
        rows_parsed=len(rows),
        created=result["created"],
        skipped=result["skipped"],
    )
    return {
        "document_id": document_id,
        "supplier_id": str(supplier_uuid),
        "rows_parsed": len(rows),
        "entries_created": result["created"],
        "entries_skipped": result["skipped"],
        "errors": result["errors"][:10],
    }


async def _map_canonical_items(factory, doc_uuid) -> dict:
    """Link this file's entries to canonical items and record their prices.

    Only a strong match is written automatically; a middling one is stored as a
    candidate in metadata_ for review. A wrong mapping silently poisons price
    history, so "unknown" stays unknown.
    """
    from sqlalchemy import select

    from app.db.models import CanonicalItem, PriceHistoryEntry, ToolCatalogEntry, ToolSupplier
    from app.domain.canonical_matching import best_match

    mapped = 0
    for_review = 0
    prices_recorded = 0

    async with factory() as db:
        entries = (
            (
                await db.execute(
                    select(ToolCatalogEntry).where(
                        ToolCatalogEntry.source_document_id == doc_uuid,
                        ToolCatalogEntry.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not entries:
            return {"mapped": 0, "for_review": 0, "prices_recorded": 0}

        items = (await db.execute(select(CanonicalItem))).scalars().all()
        if not items:
            return {"mapped": 0, "for_review": 0, "prices_recorded": 0}
        candidates = [(str(item.id), item.name, item.aliases) for item in items]

        party_id = None
        supplier_id = entries[0].supplier_id
        if supplier_id:
            supplier = await db.get(ToolSupplier, supplier_id)
            party_id = supplier.main_supplier_id if supplier else None

        for entry in entries:
            text = " ".join(filter(None, [entry.name, entry.description]))
            match = best_match(text, candidates)
            if match.decision == "auto" and match.canonical_item_id:
                entry.canonical_item_id = uuid.UUID(match.canonical_item_id)
                mapped += 1
                if entry.price_value:
                    db.add(
                        PriceHistoryEntry(
                            canonical_item_id=entry.canonical_item_id,
                            supplier_id=party_id,
                            price=entry.price_value,
                            currency=entry.price_currency or "RUB",
                            source="catalog",
                        )
                    )
                    prices_recorded += 1
            elif match.decision == "review" and match.canonical_item_id:
                metadata = dict(entry.metadata_ or {})
                metadata["canonical_candidate"] = {
                    "canonical_item_id": match.canonical_item_id,
                    "canonical_name": match.canonical_name,
                    "score": match.score,
                }
                entry.metadata_ = metadata
                for_review += 1
        await db.commit()

    return {"mapped": mapped, "for_review": for_review, "prices_recorded": prices_recorded}


async def _with_job(factory, job_id, fn):
    from app.db.models import DocumentProcessingJob

    async with factory() as db:
        job = await db.get(DocumentProcessingJob, job_id)
        if job is None:
            return
        fn(job)
        await db.commit()


async def _update_step(factory, job_id, key: str, status: str, **extra):
    await _with_job(
        factory, job_id, lambda job: pj.set_job_step(job, key, status, defs=CATALOG_STEPS, **extra)
    )


async def _fail(factory, job_id, key: str, message: str):
    def _apply(job):
        pj.set_job_step(job, key, "failed", error=message, defs=CATALOG_STEPS)
        pj.finish_job(job, "failed", error=message)

    await _with_job(factory, job_id, _apply)


async def _finish(factory, job_id, status: str, *, warning: str | None = None):
    def _apply(job):
        pj.finish_job(job, status)
        if warning:
            job.error = warning

    await _with_job(factory, job_id, _apply)


CATALOG_FILE_SUFFIXES = {".pdf", ".xls", ".xlsx", ".xlsm", ".csv", ".json", ".txt", ".docx"}


def looks_like_catalog_file_url(url: str) -> bool:
    """Is this URL a downloadable catalog file rather than a web page?"""
    from urllib.parse import urlparse

    path = urlparse(url).path.lower()
    return Path(path).suffix in CATALOG_FILE_SUFFIXES or Path(path).suffix in ARCHIVE_SUFFIXES


@celery_app.task(
    bind=True,
    name="catalog.ingest_url",
    max_retries=1,
    soft_time_limit=3600,
    time_limit=3660,
)
def ingest_catalog_url(self, supplier_id: str, url: str, title: str | None = None) -> dict:
    """Download a catalog FILE from the web and run the document pipeline on it.

    Web pages go through the text/LLM path, but a PDF or Excel catalog must not:
    that path fetches at most a handful of OCR'd pages and, measured live on a
    real 200 000-character supplier catalog, produced `rows=0` while reporting
    success. Downloading the file and feeding the normal pipeline gives page-by
    -page table extraction, dedup by hash, visible stages and a real row count.
    """
    from app.tasks.drawing_analysis import run_async

    return run_async(_ingest_url_async(supplier_id, url, title))


async def _ingest_url_async(supplier_id: str, url: str, title: str | None) -> dict:
    import uuid as _uuid
    from urllib.parse import unquote, urlparse

    import httpx

    from app.db.models import ToolSupplier
    from app.db.session import _get_session_factory
    from app.domain.catalog_documents import register_catalog_document

    filename = Path(unquote(urlparse(url).path)).name or "catalog.pdf"
    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.content
    except Exception as exc:  # noqa: BLE001 — one dead link must not fail the batch
        logger.warning("catalog_url_download_failed", url=url, error=str(exc)[:200])
        return {"url": url, "error": f"скачать не удалось: {str(exc)[:150]}"}

    if not payload:
        return {"url": url, "error": "пустой файл"}

    factory = _get_session_factory()
    async with factory() as db:
        supplier = await db.get(ToolSupplier, _uuid.UUID(supplier_id))
        if supplier is None:
            return {"url": url, "error": f"поставщик {supplier_id} не найден"}
        registered = await register_catalog_document(
            db,
            supplier=supplier,
            file_bytes=payload,
            filename=filename,
            source_channel="web",
            metadata={"source_url": url, "title": title} if title else {"source_url": url},
        )
        await db.commit()
        document_id = str(registered.document.id)
        duplicate = registered.is_duplicate

    ingest_catalog_document.delay(document_id)
    logger.info(
        "catalog_url_downloaded",
        url=url,
        document_id=document_id,
        bytes=len(payload),
        duplicate=duplicate,
    )
    return {
        "url": url,
        "document_id": document_id,
        "bytes": len(payload),
        "duplicate": duplicate,
        "status": "queued",
    }
