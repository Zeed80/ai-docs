"""Page-wise catalog ingestion: render → parse → images, resumable by design.

Why this exists: the previous path sampled 60 pages out of 948, glued their
text into one string and lost the page number on the way. Nothing could be
traced back to a page, no picture could be cropped, and 94 % of the file was
never read.

Shape of the work: short self-rescheduling batches, not one long task and not a
chord. A batch commits its pages and enqueues the next one, so a worker restart
costs at most one batch, and `catalog.resume_stalled` picks up whatever was
left mid-flight. The status column of `catalog_pages` IS the checkpoint.
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid
from typing import Any

import structlog
from sqlalchemy import func, select, text as sa_text, update as sa_update

from app.domain import processing_jobs as pj
from app.domain.catalog_images import (
    ImageCandidate,
    WordBox,
    crop_image,
    extract_page_image_candidates,
    furniture_signatures,
    match_entries_to_images,
)
from app.domain.catalog_pages import (
    crop_image_path,
    entry_content_hash,
    page_image_path,
    page_product_verdict,
)
from app.domain.pipeline import CATALOG_PIPELINE_STEP_DEFINITIONS as CATALOG_STEPS
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

RENDER_DPI = 150
OCR_DPI = 200
THUMB_PX = 320
RENDER_BATCH = 8
PARSE_BATCH = 3
# A page image is cheap to make and expensive to keep for a 948-page catalog:
# render the first pages eagerly (covers the viewer's first screen) and let the
# endpoint render the rest on demand.
EAGER_FULL_PAGES = 24
STALE_LEASE_MINUTES = 5


def _run_async(coro):
    from app.tasks.drawing_analysis import run_async

    return run_async(coro)


@celery_app.task(bind=True, name="catalog.prepare_pages", max_retries=1,
                 soft_time_limit=1800, time_limit=1860)
def prepare_catalog_pages(self, document_id: str) -> dict:
    return _run_async(_prepare_pages_async(document_id))


@celery_app.task(bind=True, name="catalog.render_page_batch", max_retries=1,
                 soft_time_limit=900, time_limit=960)
def render_catalog_page_batch(self, document_id: str, run_id: str | None = None) -> dict:
    return _run_async(_render_batch_async(document_id, run_id))


@celery_app.task(bind=True, name="catalog.parse_page_batch", max_retries=1,
                 soft_time_limit=1800, time_limit=1860)
def parse_catalog_page_batch(self, document_id: str, run_id: str | None = None) -> dict:
    return _run_async(_parse_batch_async(document_id, run_id))


@celery_app.task(bind=True, name="catalog.finalize_catalog", max_retries=1,
                 soft_time_limit=1800, time_limit=1860)
def finalize_catalog(self, document_id: str, run_id: str | None = None) -> dict:
    return _run_async(_finalize_async(document_id, run_id))


@celery_app.task(name="catalog.resume_stalled")
def resume_stalled_catalogs() -> dict:
    """Restart page work that a worker restart left mid-flight.

    Without this the checkpoint is useless: rows stay in `rendering`/`parsing`
    forever because nobody is left to press continue.
    """
    return _run_async(_resume_stalled_async())


# ── implementation ──────────────────────────────────────────────────────────


async def _session():
    from app.db.session import _get_session_factory

    return _get_session_factory()


async def _document_and_job(db, document_id: uuid.UUID):
    from app.db.models import Document, DocumentProcessingJob

    doc = await db.get(Document, document_id)
    job = (
        await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == document_id)
            .order_by(DocumentProcessingJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return doc, job


async def _set_progress(factory, document_id: uuid.UUID, step: str, done: int, total: int) -> None:
    """Write progress under a lock — set_job_step rewrites the whole JSON."""
    from app.db.models import DocumentProcessingJob

    async with factory() as db:
        await db.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"catalog_job:{document_id}"},
        )
        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == document_id)
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return
        pj.set_step_progress(job, step, done, total, defs=CATALOG_STEPS)
        await db.commit()


async def _mark_step(factory, document_id: uuid.UUID, step: str, status: str, **extra) -> None:
    from app.db.models import DocumentProcessingJob

    async with factory() as db:
        await db.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"catalog_job:{document_id}"},
        )
        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == document_id)
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return
        pj.set_job_step(job, step, status, defs=CATALOG_STEPS, **extra)
        await db.commit()


async def _is_paused(db, document_id: uuid.UUID) -> bool:
    """A long parse must be stoppable — and stay stopped.

    Without an explicit flag `catalog.resume_stalled` would helpfully restart
    the very run a person just stopped, five minutes later.
    """
    from app.db.models import Document

    doc = await db.get(Document, document_id)
    return bool((doc.metadata_ or {}).get("catalog_paused")) if doc else False


async def _local_pdf(storage_path: str) -> str:
    """Download once into a temp file — a 44 MB PDF must not live in RAM
    across four workers, and fitz opens a path lazily."""
    from app.storage import download_file

    data = await __import__("asyncio").to_thread(download_file, storage_path)
    handle, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(handle, "wb") as fh:
        fh.write(data)
    return path


async def _prepare_pages_async(document_id: str) -> dict:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.models import CatalogPage

    doc_uuid = uuid.UUID(document_id)
    factory = await _session()
    async with factory() as db:
        doc, _job = await _document_and_job(db, doc_uuid)
        if doc is None:
            return {"error": f"Document {document_id} not found"}
        storage_path = doc.storage_path

    local_path = await _local_pdf(storage_path)
    try:
        import fitz

        with fitz.open(local_path) as pdf:
            pages = [
                {
                    "document_id": doc_uuid,
                    "page_number": index + 1,
                    "width_pt": pdf.load_page(index).rect.width,
                    "height_pt": pdf.load_page(index).rect.height,
                    "status": "pending",
                }
                for index in range(pdf.page_count)
            ]
            total = pdf.page_count
    finally:
        os.unlink(local_path)

    async with factory() as db:
        # ON CONFLICT DO NOTHING makes preparation idempotent: running it twice
        # (a retry, a re-parse) must not double the page registry.
        for chunk_start in range(0, len(pages), 500):
            await db.execute(
                pg_insert(CatalogPage)
                .values(pages[chunk_start : chunk_start + 500])
                .on_conflict_do_nothing(constraint="uq_catalog_pages_document_page")
            )
        doc, _job = await _document_and_job(db, doc_uuid)
        if doc is not None:
            doc.page_count = total
        await db.commit()

    run_id = str(uuid.uuid4())
    await _mark_step(factory, doc_uuid, "pages", "running", total=total)
    render_catalog_page_batch.delay(document_id, run_id)
    logger.info("catalog_pages_prepared", document_id=document_id, pages=total, run_id=run_id)
    return {"document_id": document_id, "pages": total, "run_id": run_id}


def _thumbnail(png: bytes, max_px: int = THUMB_PX) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            image = image.convert("RGB")
            image.thumbnail((max_px, max_px))
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=75, method=4)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalog_thumb_failed", error=str(exc)[:150])
        return None


def _render_page(pdf, index: int, dpi: int = RENDER_DPI) -> tuple[bytes, int, int]:
    import fitz  # noqa: F401 — imported for the caller's context

    page = pdf.load_page(index)
    pixmap = page.get_pixmap(dpi=dpi)
    from PIL import Image

    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=80, method=4)
    return buffer.getvalue(), pixmap.width, pixmap.height


async def _claim_pages(db, doc_uuid: uuid.UUID, status: str, next_status: str, limit: int):
    """Lease a batch: SKIP LOCKED so two workers never take the same page."""
    from app.db.models import CatalogPage

    rows = (
        await db.execute(
            select(CatalogPage)
            .where(CatalogPage.document_id == doc_uuid, CatalogPage.status == status)
            .order_by(CatalogPage.page_number)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for row in rows:
        row.status = next_status
    await db.commit()
    return rows


async def _count_pages(db, doc_uuid: uuid.UUID, statuses: tuple[str, ...]) -> int:
    from app.db.models import CatalogPage

    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(CatalogPage)
                .where(CatalogPage.document_id == doc_uuid, CatalogPage.status.in_(statuses))
            )
        ).scalar_one()
    )


async def _render_batch_async(document_id: str, run_id: str | None) -> dict:
    from app.storage import upload_file

    doc_uuid = uuid.UUID(document_id)
    factory = await _session()

    async with factory() as db:
        doc, _job = await _document_and_job(db, doc_uuid)
        if doc is None:
            return {"error": "document not found"}
        if await _is_paused(db, doc_uuid):
            return {"document_id": document_id, "status": "paused"}
        storage_path = doc.storage_path
        claimed = await _claim_pages(db, doc_uuid, "pending", "rendering", RENDER_BATCH)
        page_numbers = [(row.id, row.page_number) for row in claimed]

    if not page_numbers:
        # Nothing left to render → move on to parsing.
        async with factory() as db:
            total = await _count_pages(
                db, doc_uuid, ("pending", "rendering", "rendered", "parsing", "parsed", "skipped", "failed")
            )
            done = await _count_pages(db, doc_uuid, ("rendered", "parsing", "parsed", "skipped", "failed"))
        await _set_progress(factory, doc_uuid, "pages", done, total)
        await _mark_step(factory, doc_uuid, "pages", "done", rendered=done, total=total)
        parse_catalog_page_batch.delay(document_id, run_id)
        return {"document_id": document_id, "rendered": 0, "next": "parse"}

    local_path = await _local_pdf(storage_path)
    rendered: list[dict[str, Any]] = []
    try:
        import fitz

        with fitz.open(local_path) as pdf:
            for page_id, page_number in page_numbers:
                try:
                    png, width, height = _render_page(pdf, page_number - 1)
                    full_path = page_image_path(storage_path, page_number)
                    thumb_path = page_image_path(storage_path, page_number, thumb=True)
                    keep_full = page_number <= EAGER_FULL_PAGES
                    if keep_full:
                        upload_file(png, full_path, content_type="image/webp")
                    thumb = _thumbnail(png)
                    if thumb:
                        upload_file(thumb, thumb_path, content_type="image/webp")

                    page = pdf.load_page(page_number - 1)
                    candidates = extract_page_image_candidates(
                        page, pdf, (width, height), RENDER_DPI
                    )
                    rendered.append(
                        {
                            "id": page_id,
                            "page_number": page_number,
                            "image_path": full_path if keep_full else None,
                            "thumb_path": thumb_path if thumb else None,
                            "image_width": width,
                            "image_height": height,
                            "images": [c.as_dict() for c in candidates],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — one bad page is not the catalog
                    logger.warning(
                        "catalog_page_render_failed",
                        document_id=document_id,
                        page=page_number,
                        error=str(exc)[:200],
                    )
                    rendered.append({"id": page_id, "page_number": page_number, "error": str(exc)[:400]})
    finally:
        os.unlink(local_path)

    from app.db.models import CatalogPage

    async with factory() as db:
        for item in rendered:
            row = await db.get(CatalogPage, item["id"])
            if row is None:
                continue
            if item.get("error"):
                row.status = "failed"
                row.error = item["error"]
                continue
            row.image_path = item["image_path"]
            row.thumb_path = item["thumb_path"]
            row.image_width = item["image_width"]
            row.image_height = item["image_height"]
            row.images = item["images"]
            row.status = "rendered"
            row.run_id = run_id
        await db.commit()
        total = await _count_pages(
            db, doc_uuid, ("pending", "rendering", "rendered", "parsing", "parsed", "skipped", "failed")
        )
        done = await _count_pages(db, doc_uuid, ("rendered", "parsing", "parsed", "skipped", "failed"))

    await _set_progress(factory, doc_uuid, "pages", done, total)
    render_catalog_page_batch.delay(document_id, run_id)
    return {"document_id": document_id, "rendered": len(rendered), "done": done, "total": total}


def _words_from_text_layer(page, dpi: int) -> list[WordBox]:
    from app.domain.catalog_images import pdf_bbox_to_raster

    words: list[WordBox] = []
    try:
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            if word.strip():
                words.append(WordBox(text=word, bbox=pdf_bbox_to_raster((x0, y0, x1, y1), dpi)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog_words_failed", error=str(exc)[:120])
    return words


def _ocr_page(pdf, page_number: int, render_dpi: int) -> tuple[str, list[WordBox]]:
    """One OCR pass gives BOTH the text and the word boxes.

    The old code called image_to_string and threw the geometry away, which is
    why no picture could ever be matched to an article on a scanned page.
    """
    import pytesseract
    from PIL import Image

    page = pdf.load_page(page_number - 1)
    pixmap = page.get_pixmap(dpi=OCR_DPI)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    data = pytesseract.image_to_data(
        image, lang="rus+eng", output_type=pytesseract.Output.DICT
    )

    scale = render_dpi / OCR_DPI  # OCR renders larger; bring boxes to page raster
    words: list[WordBox] = []
    parts: list[str] = []
    for index, word in enumerate(data["text"]):
        if not word.strip():
            continue
        parts.append(word)
        left, top = data["left"][index], data["top"][index]
        width, height = data["width"][index], data["height"][index]
        words.append(
            WordBox(
                text=word,
                bbox=(
                    int(left * scale),
                    int(top * scale),
                    int((left + width) * scale),
                    int((top + height) * scale),
                ),
            )
        )
    return " ".join(parts), words


async def _parse_batch_async(document_id: str, run_id: str | None) -> dict:
    from app.db.models import CatalogPage, ToolSupplier
    from app.storage import upload_file
    from app.tasks.drawing_analysis import (
        _create_catalog_entries_from_rows,
        _pdf_text_is_unreadable,
        _parse_catalog_text_via_llm,
    )

    doc_uuid = uuid.UUID(document_id)
    factory = await _session()

    async with factory() as db:
        doc, _job = await _document_and_job(db, doc_uuid)
        if doc is None:
            return {"error": "document not found"}
        if await _is_paused(db, doc_uuid):
            return {"document_id": document_id, "status": "paused"}
        storage_path = doc.storage_path
        supplier_id = (doc.metadata_ or {}).get("tool_supplier_id")
        claimed = await _claim_pages(db, doc_uuid, "rendered", "parsing", PARSE_BATCH)
        batch = [
            {
                "id": row.id,
                "page_number": row.page_number,
                "images": row.images or [],
                "thumb_path": row.thumb_path,
                "image_path": row.image_path,
                "raster": (row.image_width or 0, row.image_height or 0),
            }
            for row in claimed
        ]
        # Furniture filter needs the whole document's candidates, which exist
        # only after rendering finished — that is why it runs here, not there.
        all_images = (
            await db.execute(
                select(CatalogPage.images).where(
                    CatalogPage.document_id == doc_uuid, CatalogPage.images.isnot(None)
                )
            )
        ).scalars().all()

    if not batch:
        finalize_catalog.delay(document_id, run_id)
        return {"document_id": document_id, "parsed": 0, "next": "finalize"}

    if not supplier_id:
        return {"error": "document is not linked to a tool supplier"}
    supplier_uuid = uuid.UUID(supplier_id)
    furniture = furniture_signatures([images or [] for images in all_images])

    local_path = await _local_pdf(storage_path)
    results: list[dict[str, Any]] = []
    try:
        import fitz

        from app.tasks.gpu_lock import gpu_single_flight

        with fitz.open(local_path) as pdf:
            for item in batch:
                page_number = item["page_number"]
                page = pdf.load_page(page_number - 1)
                text = page.get_text() or ""
                words: list[WordBox] = []
                source = "text"
                if _pdf_text_is_unreadable(text) or len(text.strip()) < 40:
                    text, words = await __import__("asyncio").to_thread(
                        _ocr_page, pdf, page_number, RENDER_DPI
                    )
                    source = "ocr"
                else:
                    words = _words_from_text_layer(page, RENDER_DPI)

                verdict = page_product_verdict(text)
                if not verdict.parse:
                    results.append(
                        {
                            "id": item["id"],
                            "status": "skipped",
                            "skip_reason": verdict.skip_reason,
                            "text_source": source,
                            "text_chars": len(text),
                            "text": text,
                            "rows": [],
                        }
                    )
                    continue

                # One LLM call per page, and the GPU is released between pages
                # so invoice processing interleaves instead of waiting hours.
                with gpu_single_flight(f"catalog:{document_id}:{page_number}"):
                    rows = await _parse_catalog_text_via_llm(
                        text, hint=f"Каталог, страница {page_number}", max_chunks=4
                    )
                # The model guesses page numbers; the real one is known here.
                for row in rows:
                    row["catalog_page"] = page_number

                candidates = [
                    ImageCandidate.from_dict(raw)
                    for raw in item["images"]
                    if raw.get("signature") not in furniture
                ]
                raster = item["raster"] if item["raster"][0] else (1, 1)
                matches = match_entries_to_images(rows, words, candidates, raster)

                page_png = None
                crops: list[dict[str, Any]] = []
                for row, match in zip(rows, matches):
                    if match.kind != "crop" or match.candidate is None:
                        crops.append({"kind": "page", "thumb": item["thumb_path"]})
                        continue
                    if page_png is None:
                        page_png, _w, _h = _render_page(pdf, page_number - 1)
                    data = crop_image(page_png, match.candidate.bbox)
                    if not data:
                        crops.append({"kind": "page", "thumb": item["thumb_path"]})
                        continue
                    crop_path = crop_image_path(storage_path, page_number, match.candidate.key)
                    thumb_path = crop_image_path(
                        storage_path, page_number, match.candidate.key, thumb=True
                    )
                    upload_file(data, crop_path, content_type="image/webp")
                    thumb = _thumbnail(data, 320)
                    if thumb:
                        upload_file(thumb, thumb_path, content_type="image/webp")
                    crops.append(
                        {
                            "kind": "crop",
                            "path": crop_path,
                            "thumb": thumb_path if thumb else crop_path,
                            "bbox": list(match.candidate.bbox),
                            "score": match.score,
                        }
                    )

                results.append(
                    {
                        "id": item["id"],
                        "status": "parsed",
                        "text_source": source,
                        "text_chars": len(text),
                        "text": text,
                        "rows": rows,
                        "crops": crops,
                    }
                )
    finally:
        os.unlink(local_path)

    created_total = 0
    async with factory() as db:
        supplier = await db.get(ToolSupplier, supplier_uuid)
        for result in results:
            row_obj = await db.get(CatalogPage, result["id"])
            if row_obj is None:
                continue
            row_obj.status = result["status"]
            row_obj.skip_reason = result.get("skip_reason")
            row_obj.text_source = result.get("text_source")
            row_obj.text_chars = result.get("text_chars")
            # Capped: a page of dense text is a few thousand characters, and
            # the tail of a pathological one adds nothing a search can use.
            page_text = result.get("text")
            row_obj.text = page_text[:40000] if page_text else None
            row_obj.run_id = run_id

            rows = result.get("rows") or []
            if not rows or supplier is None:
                row_obj.entries_count = 0
                continue

            from app.db.models import ToolCatalogEntry

            # Serialise per page: two batches (a retry, a duplicate launch) must
            # not both insert the same positions — the unique index would then
            # abort the whole batch instead of skipping one row.
            await db.execute(
                sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"catalog_page:{row_obj.id}"},
            )
            # Page-wise replacement: only THIS page's positions are retired, so
            # resuming a run never wipes what earlier batches already produced.
            await db.execute(
                sa_update(ToolCatalogEntry)
                .where(
                    ToolCatalogEntry.catalog_page_id == row_obj.id,
                    ToolCatalogEntry.is_active.is_(True),
                )
                .values(is_active=False)
            )
            await db.flush()

            crops = result.get("crops") or []
            hashes = [
                entry_content_hash(
                    doc_uuid, row_obj.page_number, row.get("part_number"), row.get("name")
                )
                for row in rows
            ]
            # Whatever another page (or an earlier run) already produced under
            # the same identity is left alone rather than inserted twice.
            taken = set(
                (
                    await db.execute(
                        select(ToolCatalogEntry.content_hash).where(
                            ToolCatalogEntry.source_document_id == doc_uuid,
                            ToolCatalogEntry.is_active.is_(True),
                            ToolCatalogEntry.content_hash.in_(hashes),
                        )
                    )
                ).scalars().all()
            )
            kept_rows: list[dict[str, Any]] = []
            kept_crops: list[dict[str, Any]] = []
            kept_hashes: list[str] = []
            seen: set[str] = set()
            for index, (row, digest) in enumerate(zip(rows, hashes)):
                if digest in taken or digest in seen:
                    continue
                seen.add(digest)
                kept_rows.append(row)
                kept_hashes.append(digest)
                kept_crops.append(crops[index] if index < len(crops) else {})
            if not kept_rows:
                row_obj.entries_count = 0
                continue

            # Ids that existed BEFORE this insert. Positions imported by the old
            # whole-file path also have no page id, and picking them up as "just
            # created" made two rows share one identity hash — the unique index
            # then aborted the whole batch (seen live).
            before_ids = set(
                (
                    await db.execute(
                        select(ToolCatalogEntry.id).where(
                            ToolCatalogEntry.source_document_id == doc_uuid
                        )
                    )
                ).scalars().all()
            )

            outcome = await _create_catalog_entries_from_rows(
                db,
                supplier_uuid,
                kept_rows,
                source_document_id=doc_uuid,
                infer_types_with_llm=False,
            )
            created = outcome["created"]
            created_total += created
            row_obj.entries_count = created

            # Attach page identity and pictures to what was just created. Match
            # by the identity hash, not by insert order — another page's batch
            # may be committing at the same time.
            fresh = [
                entry
                for entry in (
                    await db.execute(
                        select(ToolCatalogEntry)
                        .where(
                            ToolCatalogEntry.source_document_id == doc_uuid,
                            ToolCatalogEntry.catalog_page_id.is_(None),
                            ToolCatalogEntry.is_active.is_(True),
                        )
                        .order_by(ToolCatalogEntry.created_at)
                    )
                ).scalars().all()
                if entry.id not in before_ids
            ]
            by_hash = {digest: index for index, digest in enumerate(kept_hashes)}
            for entry in fresh:
                digest = entry_content_hash(
                    doc_uuid, row_obj.page_number, entry.part_number, entry.name
                )
                if digest not in by_hash:
                    continue
                entry.catalog_page_id = row_obj.id
                entry.catalog_page = row_obj.page_number
                entry.content_hash = digest
                crop = kept_crops[by_hash[digest]] or None
                if crop and crop.get("kind") == "crop":
                    entry.image_path = crop["path"]
                    entry.image_thumb_path = crop["thumb"]
                    entry.image_bbox = {
                        "x": crop["bbox"][0],
                        "y": crop["bbox"][1],
                        "w": crop["bbox"][2] - crop["bbox"][0],
                        "h": crop["bbox"][3] - crop["bbox"][1],
                    }
                    entry.image_kind = "crop"
                    entry.image_confidence = crop.get("score")
                else:
                    entry.image_path = row_obj.image_path
                    entry.image_thumb_path = row_obj.thumb_path
                    entry.image_kind = "page"
                    entry.image_confidence = 0.0
        await db.commit()

        total = await _count_pages(
            db, doc_uuid, ("pending", "rendering", "rendered", "parsing", "parsed", "skipped", "failed")
        )
        done = await _count_pages(db, doc_uuid, ("parsed", "skipped", "failed"))

    await _set_progress(factory, doc_uuid, "parse", done, total)
    parse_catalog_page_batch.delay(document_id, run_id)
    logger.info(
        "catalog_pages_parsed",
        document_id=document_id,
        pages=len(results),
        created=created_total,
        done=done,
        total=total,
    )
    return {"document_id": document_id, "pages": len(results), "created": created_total}


async def _finalize_async(document_id: str, run_id: str | None) -> dict:
    from app.db.models import CatalogPage, ToolCatalogEntry

    doc_uuid = uuid.UUID(document_id)
    factory = await _session()

    async with factory() as db:
        # Anything this run did not confirm is retired — a position that
        # disappeared from a re-parsed page must not linger as if it were real.
        page_ids = (
            await db.execute(
                select(CatalogPage.id).where(
                    CatalogPage.document_id == doc_uuid, CatalogPage.run_id == run_id
                )
            )
        ).scalars().all()
        if page_ids:
            await db.execute(
                sa_update(ToolCatalogEntry)
                .where(
                    ToolCatalogEntry.source_document_id == doc_uuid,
                    ToolCatalogEntry.is_active.is_(True),
                    ToolCatalogEntry.catalog_page_id.is_(None),
                )
                .values(is_active=False)
            )
        stats = (
            await db.execute(
                select(CatalogPage.status, func.count())
                .where(CatalogPage.document_id == doc_uuid)
                .group_by(CatalogPage.status)
            )
        ).all()
        entries = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ToolCatalogEntry)
                    .where(
                        ToolCatalogEntry.source_document_id == doc_uuid,
                        ToolCatalogEntry.is_active.is_(True),
                    )
                )
            ).scalar_one()
        )
        with_image = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ToolCatalogEntry)
                    .where(
                        ToolCatalogEntry.source_document_id == doc_uuid,
                        ToolCatalogEntry.is_active.is_(True),
                        ToolCatalogEntry.image_kind == "crop",
                    )
                )
            ).scalar_one()
        )
        await db.commit()

    by_status = {status: count for status, count in stats}
    await _mark_step(factory, doc_uuid, "parse", "done", **by_status)
    await _mark_step(
        factory, doc_uuid, "images", "done", with_crop=with_image, entries=entries
    )

    from app.tasks.catalog_ingest import _map_canonical_items

    canonical_stats = await _map_canonical_items(factory, doc_uuid)
    await _mark_step(factory, doc_uuid, "canonical", "done", **canonical_stats)
    await _mark_step(factory, doc_uuid, "normalize", "done")
    await _mark_step(factory, doc_uuid, "entries", "done", created=entries)
    await _mark_step(factory, doc_uuid, "embedding", "done")
    await _mark_step(factory, doc_uuid, "graph", "done")

    from app.db.models import DocumentProcessingJob

    async with factory() as db:
        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == doc_uuid)
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is not None:
            pj.finish_job(job, "done")
            await db.commit()

    logger.info(
        "catalog_finalized",
        document_id=document_id,
        entries=entries,
        with_crop=with_image,
        pages=by_status,
    )
    return {"document_id": document_id, "entries": entries, "pages": by_status}


async def _resume_stalled_async() -> dict:
    from datetime import UTC, datetime, timedelta

    from app.db.models import CatalogPage

    factory = await _session()
    cutoff = datetime.now(UTC) - timedelta(minutes=STALE_LEASE_MINUTES)
    resumed: dict[str, int] = {}

    async with factory() as db:
        for stuck, previous in (("rendering", "pending"), ("parsing", "rendered")):
            rows = (
                await db.execute(
                    select(CatalogPage.document_id, func.count())
                    .where(CatalogPage.status == stuck, CatalogPage.updated_at < cutoff)
                    .group_by(CatalogPage.document_id)
                )
            ).all()
            if not rows:
                continue
            await db.execute(
                sa_update(CatalogPage)
                .where(CatalogPage.status == stuck, CatalogPage.updated_at < cutoff)
                .values(status=previous)
            )
            for document_id, count in rows:
                resumed[str(document_id)] = resumed.get(str(document_id), 0) + int(count)
        await db.commit()

    # A chain can also die BETWEEN batches: nothing is left in flight, but pages
    # are waiting and no task exists to pick them up. Restart those too — this
    # is the case a worker restart actually produces (measured: 927 pages left
    # in `rendered` with an empty queue).
    async with factory() as db:
        idle = (
            await db.execute(
                select(CatalogPage.document_id, func.max(CatalogPage.updated_at))
                .where(CatalogPage.status.in_(("pending", "rendered")))
                .group_by(CatalogPage.document_id)
            )
        ).all()
        for document_id, updated_at in idle:
            if updated_at is None or updated_at < cutoff:
                resumed.setdefault(str(document_id), 0)

    if resumed:
        from app.db.models import Document

        async with factory() as db:
            paused = {
                str(row[0])
                for row in (
                    await db.execute(
                        select(Document.id).where(
                            Document.id.in_([uuid.UUID(key) for key in resumed])
                        )
                    )
                ).all()
                if await _is_paused(db, row[0])
            }
        for key in paused:
            resumed.pop(key, None)

    for document_id in resumed:
        render_catalog_page_batch.delay(document_id, None)

    if resumed:
        logger.info("catalog_pages_resumed", documents=len(resumed), pages=resumed)
    return {"resumed": resumed}


@celery_app.task(
    bind=True,
    name="catalog.reindex_payload",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1860,
)
def reindex_catalog_payload(self, document_id: str | None = None) -> dict:
    """Backfill catalog/page/image keys on already-indexed vector points."""
    return _run_async(_reindex_payload_async(document_id))


async def _reindex_payload_async(document_id: str | None) -> dict:
    from app.db.models import ToolCatalogEntry
    from app.vector.qdrant_store import set_tool_catalog_payload

    factory = await _session()
    updated = 0
    failed = 0

    async with factory() as db:
        query = select(ToolCatalogEntry).where(
            ToolCatalogEntry.is_active.is_(True),
            ToolCatalogEntry.embedding_id.isnot(None),
        )
        if document_id:
            query = query.where(ToolCatalogEntry.source_document_id == uuid.UUID(document_id))
        entries = (await db.execute(query)).scalars().all()

    for entry in entries:
        payload = {
            "part_number": entry.part_number or "",
            "catalog_document_id": (
                str(entry.source_document_id) if entry.source_document_id else ""
            ),
            "catalog_page": entry.catalog_page,
            "has_image": str(entry.image_kind == "crop").lower(),
            "price_value": entry.price_value,
        }
        try:
            await __import__("asyncio").to_thread(
                set_tool_catalog_payload, str(entry.id), payload
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 — one missing point is not fatal
            failed += 1
            logger.debug("catalog_payload_backfill_failed", entry=str(entry.id), error=str(exc)[:120])

    logger.info("catalog_payload_reindexed", updated=updated, failed=failed)
    return {"updated": updated, "failed": failed}


@celery_app.task(
    bind=True,
    name="catalog.backfill_page_text",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1860,
)
def backfill_page_text(self, document_id: str | None = None, batch: int = 200) -> dict:
    """Fill in the page text of catalogs parsed before it was stored.

    Only the PDF text layer — cheap (a whole 948-page file in seconds) and
    honest about its limits: a scanned page has no layer, so it stays without
    searchable text rather than holding up the batch for an 11-minute OCR pass.
    Those pages are reachable through their positions, and a re-parse fills
    them properly.
    """
    return _run_async(_backfill_page_text_async(document_id, batch))


async def _backfill_page_text_async(document_id: str | None, batch: int) -> dict:
    import fitz

    from app.db.models import CatalogPage, Document
    from app.storage import download_file
    from app.tasks.drawing_analysis import _pdf_text_is_unreadable

    factory = await _session()
    filled = 0
    empty = 0
    documents = 0

    async with factory() as db:
        query = (
            select(CatalogPage.document_id, func.count())
            .where(CatalogPage.text.is_(None))
            .group_by(CatalogPage.document_id)
        )
        if document_id:
            query = query.where(CatalogPage.document_id == uuid.UUID(document_id))
        pending = (await db.execute(query)).all()

    for doc_id, _count in pending:
        async with factory() as db:
            doc = await db.get(Document, doc_id)
            if doc is None or not doc.storage_path:
                continue
            rows = (
                await db.execute(
                    select(CatalogPage)
                    .where(CatalogPage.document_id == doc_id, CatalogPage.text.is_(None))
                    .order_by(CatalogPage.page_number)
                    .limit(batch)
                )
            ).scalars().all()
            if not rows:
                continue
            try:
                blob = await __import__("asyncio").to_thread(download_file, doc.storage_path)
            except Exception as exc:  # noqa: BLE001 — a missing file is not fatal
                logger.warning(
                    "catalog_text_backfill_file_missing",
                    document=str(doc_id),
                    error=str(exc)[:120],
                )
                continue
            documents += 1
            with fitz.open(stream=blob, filetype="pdf") as pdf:
                for row in rows:
                    if row.page_number > pdf.page_count:
                        continue
                    text = pdf.load_page(row.page_number - 1).get_text() or ""
                    # A "text layer" is not automatically text: a PDF without a
                    # ToUnicode map yields punctuation soup, and storing it made
                    # the document search answer with unreadable snippets
                    # (seen on the live INSIZE catalog). Better no text than
                    # text that cannot be read.
                    if text.strip() and not _pdf_text_is_unreadable(text):
                        row.text = text[:40000]
                        filled += 1
                    else:
                        # Store the empty string, not NULL: NULL means "never
                        # looked", and re-running would re-open the PDF for the
                        # same scanned pages forever.
                        row.text = ""
                        empty += 1
            await db.commit()

    logger.info(
        "catalog_page_text_backfilled", filled=filled, without_readable_text=empty,
        documents=documents,
    )
    if filled or empty:
        backfill_page_text.apply_async(args=[document_id, batch], countdown=1)
    return {"filled": filled, "without_readable_text": empty, "documents": documents}


@celery_app.task(
    bind=True,
    name="catalog.purge_inactive",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1860,
)
def purge_inactive_entries(self, older_than_days: int = 30, dry_run: bool = True) -> dict:
    """Physically delete long-deactivated positions and their vector points.

    Re-parsing a catalog retires the previous run's rows rather than deleting
    them, so a mistake stays recoverable. They should not accumulate forever;
    dry_run by default because deletion is the one step that cannot be undone.
    """
    return _run_async(_purge_inactive_async(older_than_days, dry_run))


async def _purge_inactive_async(older_than_days: int, dry_run: bool) -> dict:
    from datetime import UTC, datetime, timedelta

    from app.db.models import ToolCatalogEntry
    from app.vector.qdrant_store import delete_tool_catalog_entry

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    factory = await _session()

    async with factory() as db:
        rows = (
            await db.execute(
                select(ToolCatalogEntry).where(
                    ToolCatalogEntry.is_active.is_(False),
                    ToolCatalogEntry.updated_at < cutoff,
                )
            )
        ).scalars().all()
        candidates = [(entry.id, entry.embedding_id) for entry in rows]

        if dry_run:
            logger.info("catalog_purge_dry_run", candidates=len(candidates))
            return {"dry_run": True, "candidates": len(candidates)}

        deleted = 0
        # Visual vectors are in a separate collection and would otherwise
        # survive the purge and keep matching photo searches.
        try:
            from app.vector.qdrant_store import delete_visual_catalog_entries

            await __import__("asyncio").to_thread(
                delete_visual_catalog_entries, [str(entry_id) for entry_id, _ in candidates]
            )
        except Exception as exc:  # noqa: BLE001 — DB rows still go
            logger.debug("catalog_purge_visual_failed", error=str(exc)[:120])
        for entry_id, _embedding_id in candidates:
            # Unconditionally, NOT only when embedding_id is set: the column is
            # filled by one indexing path and left empty by others, so a point
            # could outlive its row. Found on the live stand after wiping every
            # catalog — 267 vectors of positions that no longer existed, still
            # answerable by search.
            try:
                await __import__("asyncio").to_thread(
                    delete_tool_catalog_entry, str(entry_id)
                )
            except Exception as exc:  # noqa: BLE001 — DB row still goes
                logger.debug("catalog_purge_vector_failed", error=str(exc)[:120])
            entry = await db.get(ToolCatalogEntry, entry_id)
            if entry is not None:
                await db.delete(entry)
                deleted += 1
        await db.commit()

    logger.info("catalog_purged", deleted=deleted, older_than_days=older_than_days)
    return {"dry_run": False, "deleted": deleted}


@celery_app.task(
    bind=True,
    name="catalog.cleanup_anomalies",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1860,
)
def cleanup_catalog_anomalies(self, dry_run: bool = True) -> dict:
    """Close catalog "duplicate" cards that the current rule would never raise.

    The old rule flagged any name difference or a 1 % price gap, so one article
    printed on several pages of the SAME catalog produced a card each time —
    469 open cards nobody could act on. Each card is re-judged individually
    (same source? material price gap?) rather than closed wholesale, so a real
    price conflict is not swept away with the noise.
    """
    return _run_async(_cleanup_anomalies_async(dry_run))


async def _cleanup_anomalies_async(dry_run: bool) -> dict:
    from datetime import UTC, datetime

    from app.db.models import AnomalyCard, AnomalyStatus, ToolCatalogEntry
    from app.tasks.drawing_analysis import _PRICE_CONFLICT_THRESHOLD

    factory = await _session()
    closed = 0
    kept = 0

    async with factory() as db:
        cards = (
            await db.execute(
                select(AnomalyCard).where(
                    AnomalyCard.entity_type == "tool_catalog_entry",
                    AnomalyCard.status == AnomalyStatus.open,
                )
            )
        ).scalars().all()

        for card in cards:
            details = card.details or {}
            new_entry = await db.get(ToolCatalogEntry, uuid.UUID(details["new_entry_id"])) if details.get("new_entry_id") else None
            old_entry = await db.get(ToolCatalogEntry, uuid.UUID(details["existing_entry_id"])) if details.get("existing_entry_id") else None

            still_valid = False
            if new_entry is not None and old_entry is not None:
                same_source = (
                    new_entry.source_document_id is not None
                    and new_entry.source_document_id == old_entry.source_document_id
                )
                gap = 0.0
                if new_entry.price_value and old_entry.price_value:
                    gap = abs(old_entry.price_value - new_entry.price_value) / max(
                        old_entry.price_value, 1.0
                    )
                still_valid = (
                    not same_source
                    and gap > _PRICE_CONFLICT_THRESHOLD
                    and new_entry.is_active
                    and old_entry.is_active
                )

            if still_valid:
                kept += 1
                continue

            closed += 1
            if not dry_run:
                card.status = AnomalyStatus.false_positive
                card.resolved_by = "system"
                card.resolved_at = datetime.now(UTC)
                card.resolution_comment = (
                    "Закрыто автоматически: правило изменено — карточка заводится "
                    "только при существенном расхождении цены между РАЗНЫМИ "
                    "источниками. Одна и та же позиция на нескольких страницах "
                    "каталога больше не считается аномалией."
                )
        if not dry_run:
            await db.commit()

    logger.info("catalog_anomalies_cleanup", closed=closed, kept=kept, dry_run=dry_run)
    return {"dry_run": dry_run, "closed": closed, "kept": kept}
