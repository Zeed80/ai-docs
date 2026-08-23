"""Supplier catalogs: browsing, page images, and search over positions.

Kept out of api/tool_catalog.py (already ~2500 lines) — that router owns
suppliers and ingestion, this one owns what a person sees: which catalogs
exist, how far parsing got, what a page looks like, and finding a position.

Skill: catalog.*
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CatalogPage,
    Document,
    DocumentLink,
    DocumentProcessingJob,
    Party,
    ToolCatalogEntry,
    ToolSupplier,
)
from app.db.session import get_db
from app.domain.catalogs import (
    CatalogEntryOut,
    CatalogSimilarRequest,
    CatalogSimilarResponse,
    CatalogFacets,
    CatalogFacetValue,
    CatalogListResponse,
    CatalogOut,
    CatalogPageOut,
    CatalogPagesResponse,
    CatalogSearchRequest,
    CatalogSearchResponse,
    CatalogVisualSearchRequest,
    CatalogVisualSearchResponse,
)

router = APIRouter()
logger = structlog.get_logger()

_CATALOG_LINK_TYPE = "supplier_catalog"


def _page_image_url(document_id, page_number: int, *, thumb: bool) -> str:
    size = "thumb" if thumb else "full"
    return f"/api/catalogs/{document_id}/pages/{page_number}/image?size={size}"


def _entry_image_url(entry_id, *, thumb: bool) -> str:
    size = "thumb" if thumb else "full"
    return f"/api/catalogs/entries/{entry_id}/image?size={size}"


async def _supplier_ids_for_party(db: AsyncSession, party_id: uuid.UUID) -> list[uuid.UUID]:
    return [
        row[0]
        for row in (
            await db.execute(
                select(ToolSupplier.id).where(ToolSupplier.main_supplier_id == party_id)
            )
        ).all()
    ]


@router.get(
    "",
    response_model=CatalogListResponse,
    summary="Skill: catalog.list — Catalogs of a supplier with parsing progress.",
)
async def list_catalogs(
    party_id: uuid.UUID | None = Query(None),
    supplier_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> CatalogListResponse:
    """Several catalogs per supplier become distinguishable here.

    Before this endpoint the positions of two catalogs were one undivided list
    of thousands of rows — the user's "совсем ничего не разобрать".
    """
    supplier_ids: list[uuid.UUID] = []
    if supplier_id:
        supplier_ids = [supplier_id]
    elif party_id:
        supplier_ids = await _supplier_ids_for_party(db, party_id)
    else:
        # No filter → every supplier's catalogs in ONE call. The catalogs page
        # used to ask two questions per supplier and ran straight into the rate
        # limiter (24 parallel requests answered with 429 — seen in the browser).
        supplier_ids = [
            row[0] for row in (await db.execute(select(ToolSupplier.id))).all()
        ]
    if not supplier_ids:
        return CatalogListResponse(items=[], total=0)

    docs = (
        await db.execute(
            select(Document, DocumentLink.linked_entity_id)
            .join(DocumentLink, DocumentLink.document_id == Document.id)
            .where(
                DocumentLink.linked_entity_type == "tool_supplier",
                DocumentLink.linked_entity_id.in_(supplier_ids),
                DocumentLink.link_type == _CATALOG_LINK_TYPE,
            )
            .order_by(Document.created_at.desc())
        )
    ).all()
    if not docs:
        return CatalogListResponse(items=[], total=0)

    doc_ids = [doc.id for doc, _sid in docs]
    suppliers = {
        row.id: row
        for row in (
            await db.execute(select(ToolSupplier).where(ToolSupplier.id.in_(supplier_ids)))
        ).scalars()
    }

    jobs: dict[uuid.UUID, DocumentProcessingJob] = {}
    for job in (
        await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id.in_(doc_ids))
            .order_by(DocumentProcessingJob.created_at.desc())
        )
    ).scalars():
        jobs.setdefault(job.document_id, job)

    page_stats = {
        row[0]: (int(row[1]), int(row[2]))
        for row in (
            await db.execute(
                select(
                    CatalogPage.document_id,
                    func.count(),
                    func.count().filter(
                        CatalogPage.status.in_(("parsed", "skipped", "failed"))
                    ),
                )
                .where(CatalogPage.document_id.in_(doc_ids))
                .group_by(CatalogPage.document_id)
            )
        ).all()
    }
    entry_stats = {
        row[0]: (int(row[1]), int(row[2]))
        for row in (
            await db.execute(
                select(
                    ToolCatalogEntry.source_document_id,
                    func.count(),
                    func.count().filter(ToolCatalogEntry.image_kind == "crop"),
                )
                .where(
                    ToolCatalogEntry.source_document_id.in_(doc_ids),
                    ToolCatalogEntry.is_active.is_(True),
                )
                .group_by(ToolCatalogEntry.source_document_id)
            )
        ).all()
    }

    items: list[CatalogOut] = []
    for doc, sid in docs:
        job = jobs.get(doc.id)
        meta = doc.metadata_ or {}
        pages_total, pages_done = page_stats.get(doc.id, (0, 0))
        entries, with_image = entry_stats.get(doc.id, (0, 0))
        supplier = suppliers.get(sid)
        progress = _progress_from_job(job)
        items.append(
            CatalogOut(
                document_id=doc.id,
                file_name=doc.file_name,
                file_size=doc.file_size,
                uploaded_at=doc.created_at,
                supplier_id=sid,
                supplier_name=supplier.name if supplier else meta.get("supplier_name"),
                party_id=supplier.main_supplier_id if supplier else None,
                page_count=pages_total or (doc.page_count or 0),
                pages_ready=pages_done,
                entries_count=entries,
                entries_with_image=with_image,
                status=getattr(job, "status", None) or "queued",
                current_step=getattr(job, "current_step", None),
                error=getattr(job, "error", None),
                progress_done=progress[0],
                progress_total=progress[1],
                cover_url=_page_image_url(doc.id, 1, thumb=True) if pages_total else None,
                download_url=f"/api/documents/{doc.id}/download",
                is_archive=bool(meta.get("is_archive")),
                paused=bool(meta.get("catalog_paused")),
            )
        )
    # Positions that predate page-wise parsing (or came from a web page rather
    # than a file) have no catalog behind them. They stay searchable and are
    # shown as one honest pseudo-catalog instead of quietly disappearing.
    orphans = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ToolCatalogEntry)
                .where(
                    ToolCatalogEntry.supplier_id.in_(supplier_ids),
                    ToolCatalogEntry.is_active.is_(True),
                    or_(
                        ToolCatalogEntry.source_document_id.is_(None),
                        ToolCatalogEntry.source_document_id.notin_(doc_ids),
                    ),
                )
            )
        ).scalar_one()
    )
    if orphans and (party_id or supplier_id):
        items.append(
            CatalogOut(
                document_id=None,
                file_name="Без привязки к каталогу",
                file_size=0,
                supplier_id=supplier_ids[0],
                party_id=party_id,
                entries_count=orphans,
                status="legacy",
                legacy=True,
            )
        )

    return CatalogListResponse(items=items, total=len(items))


def _progress_from_job(job: DocumentProcessingJob | None) -> tuple[int, int]:
    """The furthest-along stage counter, so the card shows «страница N из M»."""
    if job is None:
        return (0, 0)
    best = (0, 0)
    for step in job.pipeline_steps or []:
        if not isinstance(step, dict):
            continue
        progress = step.get("progress") or {}
        done, total = int(progress.get("done") or 0), int(progress.get("total") or 0)
        if total and (done, total) > best:
            best = (done, total)
    return best


@router.get(
    "/{document_id}",
    response_model=CatalogOut,
    summary="Skill: catalog.get — One catalog with its progress.",
)
async def get_catalog(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CatalogOut:
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Каталог не найден")
    meta = doc.metadata_ or {}
    supplier_id = meta.get("tool_supplier_id")
    supplier = (
        await db.get(ToolSupplier, uuid.UUID(supplier_id)) if supplier_id else None
    )
    job = (
        await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == document_id)
            .order_by(DocumentProcessingJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    pages_total = int(
        (
            await db.execute(
                select(func.count()).select_from(CatalogPage).where(
                    CatalogPage.document_id == document_id
                )
            )
        ).scalar_one()
    )
    pages_done = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CatalogPage)
                .where(
                    CatalogPage.document_id == document_id,
                    CatalogPage.status.in_(("parsed", "skipped", "failed")),
                )
            )
        ).scalar_one()
    )
    entries, with_image = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(ToolCatalogEntry.image_kind == "crop"),
            ).where(
                ToolCatalogEntry.source_document_id == document_id,
                ToolCatalogEntry.is_active.is_(True),
            )
        )
    ).one()
    progress = _progress_from_job(job)
    return CatalogOut(
        document_id=doc.id,
        file_name=doc.file_name,
        file_size=doc.file_size,
        uploaded_at=doc.created_at,
        supplier_id=supplier.id if supplier else None,
        supplier_name=supplier.name if supplier else meta.get("supplier_name"),
        party_id=supplier.main_supplier_id if supplier else None,
        page_count=pages_total or (doc.page_count or 0),
        pages_ready=pages_done,
        entries_count=int(entries),
        entries_with_image=int(with_image),
        status=getattr(job, "status", None) or "queued",
        current_step=getattr(job, "current_step", None),
        error=getattr(job, "error", None),
        progress_done=progress[0],
        progress_total=progress[1],
        cover_url=_page_image_url(doc.id, 1, thumb=True) if pages_total else None,
        download_url=f"/api/documents/{doc.id}/download",
        is_archive=bool(meta.get("is_archive")),
        paused=bool(meta.get("catalog_paused")),
    )


@router.post(
    "/{document_id}/pause",
    summary="Skill: catalog.pause — Stop parsing a catalog, keeping what is done.",
)
async def pause_catalog(
    document_id: uuid.UUID,
    resume: bool = Query(False, description="true — продолжить разбор"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Parsing a big catalog occupies the GPU for hours; stopping it must be
    possible without losing the pages already parsed. Resuming continues from
    the same page — the page registry is the checkpoint."""
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Каталог не найден")
    meta = dict(doc.metadata_ or {})
    meta["catalog_paused"] = not resume
    doc.metadata_ = meta

    # The job status must follow, or the UI keeps calling the run "active":
    # the supplier card polled every 3 seconds forever and the page looked like
    # it was refreshing in a loop (user report).
    job = (
        await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == document_id)
            .order_by(DocumentProcessingJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if job is not None:
        job.status = "running" if resume else "paused"
    await db.commit()

    if resume:
        from app.tasks.catalog_pages import render_catalog_page_batch

        try:
            render_catalog_page_batch.delay(str(document_id), None)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail={"error_code": "catalog_resume_enqueue_failed", "message": str(exc)[:150]},
            ) from exc

    done = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CatalogPage)
                .where(
                    CatalogPage.document_id == document_id,
                    CatalogPage.status.in_(("parsed", "skipped")),
                )
            )
        ).scalar_one()
    )
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(CatalogPage).where(
                    CatalogPage.document_id == document_id
                )
            )
        ).scalar_one()
    )
    return {
        "document_id": str(document_id),
        "paused": not resume,
        "pages_done": done,
        "page_count": total,
        "message": (
            f"Разбор продолжен со страницы {done + 1}."
            if resume
            else f"Разбор остановлен: {done} из {total} страниц разобрано, результат сохранён."
        ),
    }


@router.delete(
    "/{document_id}",
    summary="Skill: catalog.delete — Remove a catalog: its data, its file, or both.",
)
async def delete_catalog(
    document_id: uuid.UUID,
    mode: str = Query("all", pattern="^(data|file|all)$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Three honest options instead of one all-or-nothing button.

    * ``data`` — positions, their vectors, graph nodes, page registry and
      rendered images. The file stays, so the catalog can be re-parsed.
    * ``file`` — the stored PDF and the rendered page images; the positions and
      their prices stay. This is how you reclaim storage without losing data.
    * ``all`` — everything, including the document record itself.

    Page rows carry a foreign key to the document, and the rendered images live
    in object storage: deleting the row without them left ~150 MB per catalog
    behind and made the delete fail outright once page-wise parsing landed.
    """
    from app.db.models import DocumentProcessingJob
    from app.domain.catalog_pages import catalog_pages_prefix
    from app.storage import delete_file, delete_prefix

    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Каталог не найден")

    removed = {"entries": 0, "pages": 0, "images": 0, "file": False}

    if mode in ("data", "all"):
        entries = (
            await db.execute(
                select(ToolCatalogEntry).where(
                    ToolCatalogEntry.source_document_id == document_id
                )
            )
        ).scalars().all()
        for entry in entries:
            try:
                from app.vector.qdrant_store import delete_tool_catalog_entry

                await asyncio.to_thread(delete_tool_catalog_entry, str(entry.id))
            except Exception as exc:  # noqa: BLE001 — vector cleanup is best effort
                logger.warning("entry_qdrant_cleanup_failed", error=str(exc)[:120])
            try:
                from app.domain.drawing_graph import delete_tool_catalog_graph

                await delete_tool_catalog_graph(entry.id, db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("entry_graph_cleanup_failed", error=str(exc)[:120])
            await db.delete(entry)
        removed["entries"] = len(entries)
        await db.flush()

        pages = (
            await db.execute(
                select(CatalogPage).where(CatalogPage.document_id == document_id)
            )
        ).scalars().all()
        for page in pages:
            await db.delete(page)
        removed["pages"] = len(pages)

    if mode in ("file", "all"):
        # Page images and crops live next to the file; one prefix covers both.
        try:
            removed["images"] = await asyncio.to_thread(
                delete_prefix, f"{catalog_pages_prefix(doc.storage_path)}/"
            )
            removed["file"] = True
        except Exception as exc:  # noqa: BLE001 — the DB part still proceeds
            logger.warning("catalog_storage_cleanup_failed", error=str(exc)[:150])
        if mode == "file":
            # Keep the record, but say plainly that the bytes are gone.
            meta = dict(doc.metadata_ or {})
            meta["file_removed"] = True
            doc.metadata_ = meta
            for page in (
                await db.execute(
                    select(CatalogPage).where(CatalogPage.document_id == document_id)
                )
            ).scalars():
                page.image_path = None
                page.thumb_path = None

    if mode == "all":
        await db.execute(sa_delete(DocumentLink).where(DocumentLink.document_id == document_id))
        await db.execute(
            sa_delete(DocumentProcessingJob).where(
                DocumentProcessingJob.document_id == document_id
            )
        )
        await db.delete(doc)
    elif mode == "data":
        # Nothing parsed any more — the job's stages would lie otherwise.
        await db.execute(
            sa_delete(DocumentProcessingJob).where(
                DocumentProcessingJob.document_id == document_id
            )
        )

    await db.commit()
    logger.info("catalog_deleted", document_id=str(document_id), mode=mode, **removed)
    return {
        "document_id": str(document_id),
        "mode": mode,
        **removed,
        "message": {
            "data": (
                f"Удалены данные каталога: {removed['entries']} позиций и "
                f"{removed['pages']} страниц. Файл сохранён — можно разобрать заново."
            ),
            "file": (
                f"Файл и {removed['images']} изображений удалены, "
                "позиции каталога сохранены."
            ),
            "all": (
                f"Каталог удалён полностью: {removed['entries']} позиций, "
                f"{removed['pages']} страниц, {removed['images']} файлов хранилища."
            ),
        }[mode],
    }


@router.get(
    "/{document_id}/pages",
    response_model=CatalogPagesResponse,
    summary="Skill: catalog.pages — Page registry with thumbnails and status.",
)
async def list_catalog_pages(
    document_id: uuid.UUID,
    page_from: int = Query(1, ge=1),
    page_to: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> CatalogPagesResponse:
    query = select(CatalogPage).where(
        CatalogPage.document_id == document_id, CatalogPage.page_number >= page_from
    )
    if page_to:
        query = query.where(CatalogPage.page_number <= page_to)
    rows = (await db.execute(query.order_by(CatalogPage.page_number))).scalars().all()
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(CatalogPage).where(
                    CatalogPage.document_id == document_id
                )
            )
        ).scalar_one()
    )
    return CatalogPagesResponse(
        document_id=document_id,
        page_count=total,
        items=[
            CatalogPageOut(
                page_number=row.page_number,
                status=row.status,
                skip_reason=row.skip_reason,
                entries_count=row.entries_count,
                width=row.image_width,
                height=row.image_height,
                thumb_url=_page_image_url(document_id, row.page_number, thumb=True),
                image_url=_page_image_url(document_id, row.page_number, thumb=False),
            )
            for row in rows
        ],
    )


@router.get(
    "/{document_id}/pages/{page_number}/image",
    summary="Skill: catalog.page_image — Rendered catalog page (webp).",
    response_class=Response,
)
async def get_catalog_page_image(
    document_id: uuid.UUID,
    page_number: int,
    request: Request,
    size: str = Query("full", pattern="^(full|thumb)$"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve the page image, rendering it on demand.

    Only the first pages are rendered eagerly: keeping 948 full-size pages for
    every catalog would cost far more storage than it saves time, and this
    endpoint fills the gap on the first open (same shape as the drawings
    thumbnail endpoint).
    """
    from app.storage import download_file, upload_file

    row = (
        await db.execute(
            select(CatalogPage).where(
                CatalogPage.document_id == document_id,
                CatalogPage.page_number == page_number,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Страница каталога не найдена")

    path = row.thumb_path if size == "thumb" else row.image_path
    etag = f'W/"{document_id}-{page_number}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    if path:
        try:
            data = await asyncio.to_thread(download_file, path)
            return _image_response(data, etag)
        except Exception as exc:  # noqa: BLE001 — fall through to rendering
            logger.info("catalog_page_image_missing", path=path, error=str(exc)[:120])

    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Каталог не найден")

    from app.domain.catalog_pages import page_image_path
    from app.tasks.catalog_pages import RENDER_DPI, _render_page, _thumbnail

    def _render() -> tuple[bytes, int, int]:
        import fitz

        content = download_file(doc.storage_path)
        with fitz.open(stream=content, filetype="pdf") as pdf:
            if page_number < 1 or page_number > pdf.page_count:
                raise HTTPException(status_code=404, detail="Такой страницы нет")
            return _render_page(pdf, page_number - 1, RENDER_DPI)

    try:
        png, width, height = await asyncio.to_thread(_render)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Не удалось отрисовать страницу: {str(exc)[:150]}"
        ) from exc

    full_path = page_image_path(doc.storage_path, page_number)
    thumb_path = page_image_path(doc.storage_path, page_number, thumb=True)
    payload = png
    if size == "thumb":
        payload = _thumbnail(png) or png

    try:
        await asyncio.to_thread(
            upload_file, png, full_path, "image/webp"
        )
        row.image_path = full_path
        row.image_width, row.image_height = width, height
        if size == "thumb" and payload is not png:
            await asyncio.to_thread(upload_file, payload, thumb_path, "image/webp")
            row.thumb_path = thumb_path
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — caching is best effort
        logger.warning("catalog_page_cache_failed", error=str(exc)[:150])

    return _image_response(payload, etag)


def _image_response(data: bytes, etag: str) -> Response:
    return Response(
        content=data,
        media_type="image/webp",
        headers={"ETag": etag, "Cache-Control": "private, max-age=604800"},
    )


@router.get(
    "/entries/{entry_id}/image",
    summary="Skill: catalog.entry_image — Product picture of a catalog position.",
    response_class=Response,
)
async def get_entry_image(
    entry_id: uuid.UUID,
    request: Request,
    size: str = Query("thumb", pattern="^(full|thumb)$"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from app.storage import download_file

    entry = await db.get(ToolCatalogEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    path = entry.image_thumb_path if size == "thumb" else entry.image_path
    path = path or entry.image_path or entry.image_thumb_path
    if not path:
        raise HTTPException(status_code=404, detail="У позиции нет изображения")

    etag = f'W/"{entry_id}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    try:
        data = await asyncio.to_thread(download_file, path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Изображение недоступно") from exc
    return _image_response(data, etag)


def _entry_out(
    entry: ToolCatalogEntry,
    *,
    supplier_name: str | None = None,
    catalog_name: str | None = None,
    score: float | None = None,
) -> CatalogEntryOut:
    # Some historical rows stored a LIST in this JSON column; `.get` on it threw
    # a 500 for the whole search page (found in the browser, not in tests).
    meta = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
    return CatalogEntryOut(
        id=entry.id,
        part_number=entry.part_number,
        name=entry.name,
        description=entry.description,
        tool_type=entry.tool_type.value if hasattr(entry.tool_type, "value") else str(entry.tool_type),
        diameter_mm=entry.diameter_mm,
        length_mm=entry.length_mm,
        material=entry.material,
        coating=entry.coating,
        price_value=entry.price_value,
        price_currency=entry.price_currency,
        unit=entry.unit,
        supplier_id=entry.supplier_id,
        supplier_name=supplier_name,
        catalog_document_id=entry.source_document_id,
        catalog_name=catalog_name,
        page_number=entry.catalog_page,
        image_url=_entry_image_url(entry.id, thumb=False) if entry.image_path else None,
        thumb_url=_entry_image_url(entry.id, thumb=True)
        if (entry.image_thumb_path or entry.image_path)
        else None,
        image_kind=entry.image_kind,
        image_bbox=entry.image_bbox,
        score=score,
        legacy=bool(meta.get("legacy_import")),
    )


# ── search ──────────────────────────────────────────────────────────────────

_RRF_K = 60
_BRANCH_WEIGHTS = {"exact": 2.0, "text": 1.0, "vector": 0.8}
_CANDIDATE_LIMIT = 400


def _apply_filters(query, payload: CatalogSearchRequest, supplier_ids: list[uuid.UUID]):
    query = query.where(ToolCatalogEntry.is_active.is_(True))
    if supplier_ids:
        query = query.where(ToolCatalogEntry.supplier_id.in_(supplier_ids))
    catalog_ids = list(payload.catalog_document_ids or [])
    if payload.catalog_document_id:
        catalog_ids.append(payload.catalog_document_id)
    if catalog_ids:
        query = query.where(ToolCatalogEntry.source_document_id.in_(catalog_ids))
    if payload.page_number:
        query = query.where(ToolCatalogEntry.catalog_page == payload.page_number)
    if payload.tool_type:
        query = query.where(ToolCatalogEntry.tool_type == payload.tool_type)
    if payload.has_price is True:
        query = query.where(ToolCatalogEntry.price_value.isnot(None))
    if payload.has_price is False:
        query = query.where(ToolCatalogEntry.price_value.is_(None))
    if payload.has_image is True:
        query = query.where(ToolCatalogEntry.image_kind == "crop")
    if payload.has_image is False:
        query = query.where(
            or_(ToolCatalogEntry.image_kind.is_(None), ToolCatalogEntry.image_kind == "page")
        )
    if payload.price_min is not None:
        query = query.where(ToolCatalogEntry.price_value >= payload.price_min)
    if payload.price_max is not None:
        query = query.where(ToolCatalogEntry.price_value <= payload.price_max)
    if payload.diameter_min is not None:
        query = query.where(ToolCatalogEntry.diameter_mm >= payload.diameter_min)
    if payload.diameter_max is not None:
        query = query.where(ToolCatalogEntry.diameter_mm <= payload.diameter_max)
    return query


@router.post(
    "/search",
    response_model=CatalogSearchResponse,
    summary="Skill: catalog.search — Search catalog positions (exact + text + vector).",
)
async def search_catalog(
    payload: CatalogSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> CatalogSearchResponse:
    """Three branches fused by rank, not one branch chosen by chance.

    The previous search picked EITHER the vector hits OR an ILIKE fallback and
    then sorted by name — the ranking was thrown away, a typo found nothing,
    and an exact article number could sit on page four of the results.
    """
    from app.db.text_search import (
        immutable_fts_condition,
        immutable_fts_rank,
        text_search_condition,
    )

    supplier_ids: list[uuid.UUID] = list(payload.supplier_ids or [])
    if payload.supplier_id:
        supplier_ids.append(payload.supplier_id)
    parties = list(payload.party_ids or [])
    if payload.party_id:
        parties.append(payload.party_id)
    for party in parties:
        supplier_ids.extend(await _supplier_ids_for_party(db, party))
    supplier_ids = list(dict.fromkeys(supplier_ids))
    if parties and not supplier_ids:
        return CatalogSearchResponse(page=payload.page, page_size=payload.page_size)
    catalog_ids = list(payload.catalog_document_ids or [])
    if payload.catalog_document_id:
        catalog_ids.append(payload.catalog_document_id)

    query_text = (payload.query or "").strip()
    diagnostics: dict = {"branches": {}}
    ranked_ids: list[uuid.UUID] = []

    if not query_text:
        # Pure filtering: honest SQL count and ordinary pagination.
        base = _apply_filters(select(ToolCatalogEntry), payload, supplier_ids)
        total = int(
            (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        )
        rows = (
            await db.execute(
                base.order_by(
                    ToolCatalogEntry.catalog_page.nulls_last(), ToolCatalogEntry.name
                )
                .offset((payload.page - 1) * payload.page_size)
                .limit(payload.page_size)
            )
        ).scalars().all()
        items = await _decorate(db, rows, {})
        facets = await _facets(db, payload, supplier_ids) if payload.include_facets else None
        return CatalogSearchResponse(
            items=items,
            total=total,
            page=payload.page,
            page_size=payload.page_size,
            facets=facets,
            diagnostics={"branches": {"filter": total}},
        )

    branches: dict[str, list[uuid.UUID]] = {}

    # 1. Exact article — what a person typing "MT190-016C04" means.
    normalized = query_text.replace(" ", "")
    exact_rows = (
        await db.execute(
            _apply_filters(select(ToolCatalogEntry.id), payload, supplier_ids)
            .where(
                or_(
                    ToolCatalogEntry.part_number.ilike(query_text),
                    ToolCatalogEntry.part_number.ilike(f"{normalized}%"),
                )
            )
            .limit(50)
        )
    ).scalars().all()
    branches["exact"] = list(exact_rows)

    # 2. Full text + trigram (typos) via the shared helpers.
    columns = [ToolCatalogEntry.name, ToolCatalogEntry.part_number, ToolCatalogEntry.description]
    text_query = _apply_filters(select(ToolCatalogEntry.id), payload, supplier_ids)
    from app.db.text_search import is_postgresql_session

    if is_postgresql_session(db):
        # The same expression the GIN index is built on — anything else means a
        # sequential scan over the whole catalog.
        text_query = text_query.where(immutable_fts_condition(columns, query_text)).order_by(
            immutable_fts_rank(columns, query_text).desc()
        )
        # Typos are the trigram branch's job; it rides along as an OR.
        fuzzy = _apply_filters(select(ToolCatalogEntry.id), payload, supplier_ids).where(
            text_search_condition(db, columns, query_text)
        )
    else:
        text_query = text_query.where(text_search_condition(db, columns, query_text))
        fuzzy = None
    text_ids = list((await db.execute(text_query.limit(_CANDIDATE_LIMIT))).scalars().all())
    if fuzzy is not None and len(text_ids) < payload.page_size:
        # Nothing matched the exact-ish index — this is where "фреза канцевая"
        # gets found instead of returning an empty page.
        seen = set(text_ids)
        for entry_id in (await db.execute(fuzzy.limit(_CANDIDATE_LIMIT))).scalars().all():
            if entry_id not in seen:
                text_ids.append(entry_id)
                seen.add(entry_id)
    branches["text"] = text_ids

    # 3. Vector — the "похожее по смыслу" branch.
    vector_ids: list[uuid.UUID] = []
    try:
        from app.ai.embeddings import embed_text
        from app.vector.qdrant_store import search_tool_catalog

        vector = await embed_text(query_text, task_type="query")
        if vector:
            hits = await asyncio.to_thread(
                search_tool_catalog,
                vector,
                tool_type=payload.tool_type,
                supplier_id=str(supplier_ids[0]) if len(supplier_ids) == 1 else None,
                catalog_document_id=(
                    str(catalog_ids[0]) if len(catalog_ids) == 1 else None
                ),
                has_image=payload.has_image,
                limit=200,
            )
            vector_ids = [uuid.UUID(hit["entry_id"]) for hit in hits if hit.get("entry_id")]
    except Exception as exc:  # noqa: BLE001 — vector is one branch, not the search
        diagnostics["vector_error"] = str(exc)[:150]
    if vector_ids:
        # Filters must hold for vector hits too, or a "поиск в этом каталоге"
        # would quietly return positions from another one.
        allowed = set(
            (
                await db.execute(
                    _apply_filters(select(ToolCatalogEntry.id), payload, supplier_ids).where(
                        ToolCatalogEntry.id.in_(vector_ids)
                    )
                )
            ).scalars().all()
        )
        vector_ids = [entry_id for entry_id in vector_ids if entry_id in allowed]
    branches["vector"] = vector_ids

    for name, ids in branches.items():
        diagnostics["branches"][name] = len(ids)

    scores: dict[uuid.UUID, float] = {}
    for name, ids in branches.items():
        weight = _BRANCH_WEIGHTS.get(name, 1.0)
        for rank_index, entry_id in enumerate(ids):
            scores[entry_id] = scores.get(entry_id, 0.0) + weight / (_RRF_K + rank_index + 1)
    ranked_ids = sorted(scores, key=lambda key: scores[key], reverse=True)

    total_available = len(ranked_ids)
    window = ranked_ids[
        (payload.page - 1) * payload.page_size : payload.page * payload.page_size
    ]
    rows = (
        (await db.execute(select(ToolCatalogEntry).where(ToolCatalogEntry.id.in_(window))))
        .scalars()
        .all()
        if window
        else []
    )
    order = {entry_id: index for index, entry_id in enumerate(window)}
    rows.sort(key=lambda entry: order.get(entry.id, 0))

    items = await _decorate(db, rows, scores)
    facets = await _facets(db, payload, supplier_ids) if payload.include_facets else None
    return CatalogSearchResponse(
        items=items,
        total=total_available,
        page=payload.page,
        page_size=payload.page_size,
        facets=facets,
        diagnostics=diagnostics,
        report=_entries_report(f"Найдено по запросу «{query_text}»", items),
        message=(
            f"Найдено позиций: {total_available}. У каждой указан каталог и страница; "
            "image_kind=\"page\" означает превью страницы, а не фото товара."
            if total_available
            else "По этому запросу в каталогах поставщиков ничего не найдено."
        ),
    )


def _entries_report(title: str, items: list[CatalogEntryOut]) -> dict:
    """A publishable table: article, name, price, catalog and a link to the page."""
    return {
        "title": title,
        "columns": [
            {"key": "part_number", "header": "Артикул", "type": "text"},
            {"key": "name", "header": "Наименование", "type": "text"},
            {"key": "price", "header": "Цена", "type": "text"},
            {"key": "catalog", "header": "Каталог", "type": "text"},
            {"key": "page", "header": "Страница", "type": "link"},
            {"key": "image", "header": "Картинка", "type": "text"},
        ],
        "rows": [
            {
                "part_number": item.part_number or "—",
                "name": item.name,
                "price": (
                    f"{item.price_value:,.2f} {item.price_currency}".replace(",", " ")
                    if item.price_value
                    else "не указана"
                ),
                "catalog": item.catalog_name or "без привязки",
                "page": (
                    {
                        "href": (
                            f"/catalogs/{item.catalog_document_id}"
                            f"?page={item.page_number}&entry={item.id}"
                        ),
                        "label": f"стр. {item.page_number}",
                    }
                    if item.catalog_document_id and item.page_number
                    else {"href": "", "label": "—"}
                ),
                "image": (
                    "товар"
                    if item.image_kind == "crop"
                    else ("страница" if item.image_kind == "page" else "нет")
                ),
            }
            for item in items
        ],
    }


async def _decorate(
    db: AsyncSession, rows: list[ToolCatalogEntry], scores: dict[uuid.UUID, float]
) -> list[CatalogEntryOut]:
    if not rows:
        return []
    supplier_ids = {row.supplier_id for row in rows if row.supplier_id}
    doc_ids = {row.source_document_id for row in rows if row.source_document_id}
    suppliers = {
        supplier.id: supplier
        for supplier in (
            await db.execute(select(ToolSupplier).where(ToolSupplier.id.in_(supplier_ids)))
        ).scalars()
    } if supplier_ids else {}
    documents = {
        doc.id: doc
        for doc in (
            await db.execute(select(Document).where(Document.id.in_(doc_ids)))
        ).scalars()
    } if doc_ids else {}
    return [
        _entry_out(
            row,
            supplier_name=(suppliers.get(row.supplier_id).name if suppliers.get(row.supplier_id) else None),
            catalog_name=(documents.get(row.source_document_id).file_name if documents.get(row.source_document_id) else None),
            score=round(scores.get(row.id), 4) if scores.get(row.id) else None,
        )
        for row in rows
    ]


async def _facets(
    db: AsyncSession, payload: CatalogSearchRequest, supplier_ids: list[uuid.UUID]
) -> CatalogFacets:
    """Counted in SQL over the whole filtered set.

    Counting facets over the current page is the classic way to show numbers
    that do not add up to the total.
    """
    base = _apply_filters(select(ToolCatalogEntry), payload, supplier_ids).subquery()

    by_supplier = (
        await db.execute(
            select(base.c.supplier_id, func.count()).group_by(base.c.supplier_id)
        )
    ).all()
    by_catalog = (
        await db.execute(
            select(base.c.source_document_id, func.count()).group_by(base.c.source_document_id)
        )
    ).all()
    by_type = (
        await db.execute(select(base.c.tool_type, func.count()).group_by(base.c.tool_type))
    ).all()
    with_price = int(
        (
            await db.execute(
                select(func.count()).select_from(base).where(base.c.price_value.isnot(None))
            )
        ).scalar_one()
    )
    with_image = int(
        (
            await db.execute(
                select(func.count()).select_from(base).where(base.c.image_kind == "crop")
            )
        ).scalar_one()
    )

    supplier_names = {
        supplier.id: supplier.name
        for supplier in (
            await db.execute(
                select(ToolSupplier).where(
                    ToolSupplier.id.in_([row[0] for row in by_supplier if row[0]])
                )
            )
        ).scalars()
    }
    catalog_names = {
        doc.id: doc.file_name
        for doc in (
            await db.execute(
                select(Document).where(Document.id.in_([row[0] for row in by_catalog if row[0]]))
            )
        ).scalars()
    }

    return CatalogFacets(
        suppliers=[
            CatalogFacetValue(
                key=str(key), label=supplier_names.get(key, "—"), count=int(count)
            )
            for key, count in by_supplier
            if key
        ],
        catalogs=[
            CatalogFacetValue(
                key=str(key), label=catalog_names.get(key, "Без привязки"), count=int(count)
            )
            for key, count in by_catalog
            if key
        ],
        tool_types=[
            CatalogFacetValue(
                key=(key.value if hasattr(key, "value") else str(key)),
                label=(key.value if hasattr(key, "value") else str(key)),
                count=int(count),
            )
            for key, count in by_type
            if key
        ],
        with_price=with_price,
        with_image=with_image,
    )


@router.post(
    "/similar",
    response_model=CatalogSimilarResponse,
    summary="Skill: catalog.similar — Analogues of a position across catalogs.",
)
async def find_similar_positions(
    payload: CatalogSimilarRequest,
    db: AsyncSession = Depends(get_db),
) -> CatalogSimilarResponse:
    """"Чем это заменить" — the question procurement actually asks.

    Runs on the position's own embedding (or a free-text description), so a
    replacement is found by meaning rather than by a matching article number,
    which by definition differs between manufacturers. Optionally excludes the
    same supplier — that is the whole point when looking for an alternative.
    """
    from app.ai.embeddings import embed_text
    from app.vector.qdrant_store import search_tool_catalog

    source: ToolCatalogEntry | None = None
    text = (payload.query or "").strip()
    if payload.entry_id:
        source = await db.get(ToolCatalogEntry, payload.entry_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Позиция не найдена")
        text = text or " ".join(
            filter(
                None,
                [
                    source.tool_type.value if hasattr(source.tool_type, "value") else None,
                    source.name,
                    f"Ø{source.diameter_mm}мм" if source.diameter_mm else None,
                    source.material,
                    source.coating,
                ],
            )
        )
    if not text:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "similar_query_required",
                "message": "Передайте entry_id или query — по чему искать аналоги.",
            },
        )

    # An analogue of a drill is a drill. Without this the vector branch happily
    # returned a thread micrometer for "сверло спиральное D5" — technically the
    # nearest neighbour, useless as a replacement.
    tool_type = None
    if source is not None:
        raw_type = (
            source.tool_type.value
            if hasattr(source.tool_type, "value")
            else str(source.tool_type)
        )
        tool_type = raw_type if raw_type != "other" else None

    try:
        vector = await embed_text(text, task_type="query")
        hits = (
            await asyncio.to_thread(
                search_tool_catalog,
                vector,
                tool_type=tool_type,
                limit=payload.limit * 4,
            )
            if vector
            else []
        )
        if len(hits) < 3 and tool_type:
            # Too few of that kind — widen rather than answer "ничего нет".
            hits = await asyncio.to_thread(
                search_tool_catalog, vector, limit=payload.limit * 4
            )
            tool_type = None
    except Exception as exc:  # noqa: BLE001 — say why, do not pretend there are none
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "vector_search_unavailable",
                "message": f"Векторный поиск недоступен: {str(exc)[:150]}",
            },
        ) from exc

    ids = [uuid.UUID(hit["entry_id"]) for hit in hits if hit.get("entry_id")]
    ids = [entry_id for entry_id in ids if not source or entry_id != source.id]
    if not ids:
        return CatalogSimilarResponse(
            source=_entry_out(source) if source else None,
            message="Похожих позиций в каталогах не нашлось.",
        )

    query = select(ToolCatalogEntry).where(
        ToolCatalogEntry.id.in_(ids), ToolCatalogEntry.is_active.is_(True)
    )
    if payload.exclude_same_supplier and source and source.supplier_id:
        query = query.where(ToolCatalogEntry.supplier_id != source.supplier_id)
    rows = (await db.execute(query)).scalars().all()

    order = {entry_id: index for index, entry_id in enumerate(ids)}
    rows.sort(key=lambda entry: order.get(entry.id, 999))
    rows = rows[: payload.limit]

    items = await _decorate(db, rows, {})
    return CatalogSimilarResponse(
        source=_entry_out(source) if source else None,
        items=items,
        report=_entries_report(
            f"Аналоги: {source.name if source else text}"[:120], items
        ),
        message=(
            f"Похожих позиций: {len(items)}"
            + (" (у других поставщиков)" if payload.exclude_same_supplier else "")
            + (
                f", тот же тип инструмента ({tool_type})"
                if tool_type
                else ", тип инструмента не ограничивался"
            )
            + ". Подбор по смыслу — обязательно сверьте размеры и исполнение."
        ),
    )


@router.post(
    "/search-visual",
    response_model=CatalogVisualSearchResponse,
    summary="Skill: catalog.search_visual — Find catalog positions by a picture.",
)
async def search_catalog_visually(
    payload: CatalogVisualSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> CatalogVisualSearchResponse:
    """«Вот такое нужно» — a photo of the tool instead of an article number.

    The picture and the catalog crops share one vector space, so words and
    images are the same query here: a photo alone, words alone, or a photo
    narrowed by words ("такой же, но 12 мм").

    When the sidecar is unavailable this returns available=false and NO items,
    rather than quietly answering from the text index — a photo search that
    silently becomes a word search is worse than one that says it is off.
    """
    import base64
    import binascii

    from app.ai.vl_embeddings import embed_query, rerank_candidates, vl_info
    from app.tasks.catalog_visual import INDEXED_KEY
    from app.vector.qdrant_store import search_visual_catalog

    text = (payload.query or "").strip() or None
    image: bytes | None = None
    if payload.image_base64:
        raw = payload.image_base64
        raw = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        try:
            image = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "image_not_base64",
                    "message": "Картинку нужно передать в base64.",
                },
            )
    source: ToolCatalogEntry | None = None
    if payload.entry_id and not image:
        source = await db.get(ToolCatalogEntry, payload.entry_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Позиция не найдена")
        if source.image_path:
            from app.storage import download_file

            try:
                image = await asyncio.to_thread(download_file, source.image_path)
            except Exception as exc:  # noqa: BLE001 — fall back to its words
                logger.warning(
                    "catalog_visual_source_image_missing",
                    entry=str(source.id),
                    error=str(exc)[:120],
                )
        text = text or " ".join(
            part for part in (source.part_number, source.name) if part
        )

    if not image and not text:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "visual_query_required",
                "message": "Передайте картинку, текст, entry_id или их сочетание.",
            },
        )

    indexed = await db.scalar(
        select(func.count())
        .select_from(ToolCatalogEntry)
        .where(
            ToolCatalogEntry.is_active.is_(True),
            ToolCatalogEntry.metadata_[INDEXED_KEY].as_string().isnot(None),
        )
    )

    info = await vl_info()
    if not info:
        return CatalogVisualSearchResponse(
            available=False,
            mode="image" if image else "text",
            indexed_positions=int(indexed or 0),
            report={
                "title": "Поиск по картинке недоступен",
                "message": (
                    "Сервис распознавания изображений не отвечает. "
                    "Обычный поиск по каталогу работает."
                ),
            },
        )

    vector = await embed_query(text=text, image=image)
    if not vector:
        return CatalogVisualSearchResponse(
            available=False,
            mode="image" if image else "text",
            model=info.get("model"),
            indexed_positions=int(indexed or 0),
        )

    hits = await asyncio.to_thread(
        search_visual_catalog,
        vector,
        supplier_id=str(payload.supplier_id) if payload.supplier_id else None,
        catalog_document_id=(
            str(payload.catalog_document_id) if payload.catalog_document_id else None
        ),
        image_kind="crop" if payload.crops_only else None,
        exclude_entry_id=str(payload.entry_id) if payload.entry_id else None,
        limit=payload.limit,
        score_threshold=payload.score_threshold,
    )

    scores = {hit["entry_id"]: float(hit["score"]) for hit in hits if hit.get("entry_id")}
    if not scores:
        return CatalogVisualSearchResponse(
            mode=("image+text" if image and text else "image" if image else "text"),
            model=info.get("model"),
            indexed_positions=int(indexed or 0),
            report={
                "title": "Похожего в каталогах не нашлось",
                "message": (
                    f"Просмотрено позиций с картинкой: {int(indexed or 0)}. "
                    "Попробуйте снимок крупнее или добавьте пару слов к запросу."
                ),
            },
        )

    ids = [uuid.UUID(entry_id) for entry_id in scores]
    rows = (
        await db.execute(
            select(ToolCatalogEntry).where(
                ToolCatalogEntry.id.in_(ids), ToolCatalogEntry.is_active.is_(True)
            )
        )
    ).scalars().all()
    # Qdrant's order is the answer; the SQL round-trip only fetches the rows.
    rows.sort(key=lambda entry: -scores.get(str(entry.id), 0.0))

    # Rerank the head of the list. The embedder recalls the right FAMILY; when
    # a family shares one illustration every crop is byte-identical and only
    # the caption separates the sizes — that ordering is what the cross-encoder
    # can fix and the vector cannot.
    reranked = False
    if payload.rerank and len(rows) > 1:
        head = rows[: min(len(rows), 16)]
        from app.storage import download_file

        documents = []
        for entry in head:
            caption = " ".join(
                part for part in (entry.part_number, entry.name, entry.description) if part
            )
            # The candidate's PICTURE goes in too. Measured without it on 25
            # live queries: the reranker scored a photo query against bare text
            # and made things worse (top-1 11/25 against the vector order's
            # 14/25) — half the query was simply not being compared.
            candidate_image: bytes | None = None
            if entry.image_path:
                try:
                    candidate_image = await asyncio.to_thread(download_file, entry.image_path)
                except Exception:  # noqa: BLE001 — text-only candidate is fine
                    candidate_image = None
            documents.append({"text": caption, "image": candidate_image})
        rerank_scores = await rerank_candidates(
            query_text=text, query_image=image, documents=documents
        )
        if rerank_scores:
            for entry, score in zip(head, rerank_scores):
                scores[str(entry.id)] = float(score)
            rows = sorted(head, key=lambda e: -scores[str(e.id)]) + rows[len(head) :]
            reranked = True

    items = await _decorate(db, rows, {})
    mode = "image+text" if image and text else ("image" if image else "text")
    return CatalogVisualSearchResponse(
        items=items,
        scores=scores,
        reranked=reranked,
        mode=mode,
        model=info.get("model"),
        indexed_positions=int(indexed or 0),
        report=_entries_report(
            "Найдено по картинке" if image else f"Найдено по запросу: {text}"[:120],
            items,
        ),
    )
