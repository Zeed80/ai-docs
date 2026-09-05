"""FastAPI router for Tool Catalog — supplier CRUD, tool search, AI suggestions, catalog import.

Skill: tool_catalog.*, supplier_catalog.*
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlalchemy import and_, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    CatalogPage,
    Document,
    DocumentType,
    DrawingFeature,
    InventoryItem,
    ToolCatalogEntry,
    ToolSupplier,
    ToolTypeEnum,
)
from app.db.session import get_db
from app.domain.catalog_documents import ARCHIVE_SUFFIXES
from app.domain.tool_catalog import (
    AttachedSourceResult,
    AttachWebCatalogRequest,
    AttachWebCatalogResult,
    CatalogCandidate,
    CatalogImportResult,
    CatalogIngestStatusRequest,
    CatalogIngestStatusResult,
    CatalogUploadOut,
    CatalogUploadsResponse,
    CatalogUploadStep,
    CrawlSiteRequest,
    CrawlSiteResult,
    DiscoverCatalogsRequest,
    DiscoverCatalogsResult,
    IngestWebSourceRequest,
    IngestWebSourceResult,
    ResolveSupplierResult,
    SupplierCandidate,
    SupplierRefRequest,
    ToolCatalogEntryCreate,
    ToolCatalogEntryOut,
    ToolCatalogEntryUpdate,
    ToolCatalogEntryWithSupplierOut,
    ToolCatalogListResponse,
    ToolSuggestionItem,
    ToolSuggestionResponse,
    ToolSupplierCreate,
    ToolSupplierListResponse,
    ToolSupplierOut,
    ToolSupplierUpdate,
)

# Только для аннотаций: имена стоят в строковых типах, поэтому в рантайме
# не вычисляются — но без импорта их не видят ни type checker, ни IDE, и
# опечатка в такой аннотации не находится ничем.
if TYPE_CHECKING:
    from app.db.models import Party

router = APIRouter()
logger = structlog.get_logger()


# ── Supplier CRUD ─────────────────────────────────────────────────────────────


@router.post(
    "/suppliers",
    response_model=ToolSupplierOut,
    status_code=status.HTTP_201_CREATED,
    summary="Skill: supplier_catalog.create — Create a tool supplier.",
)
async def create_supplier(
    payload: ToolSupplierCreate,
    db: AsyncSession = Depends(get_db),
) -> ToolSupplierOut:
    supplier = ToolSupplier(**payload.model_dump())
    db.add(supplier)
    await db.commit()
    await db.refresh(supplier)
    return ToolSupplierOut.model_validate(supplier)


@router.get(
    "/suppliers",
    response_model=ToolSupplierListResponse,
    summary="Skill: supplier_catalog.list — List all tool suppliers.",
)
async def list_suppliers(
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
) -> ToolSupplierListResponse:
    q = select(ToolSupplier)
    if active_only:
        q = q.where(ToolSupplier.is_active.is_(True))
    q = q.order_by(ToolSupplier.name)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    result = await db.execute(q)
    items = result.scalars().all()
    return ToolSupplierListResponse(
        items=[ToolSupplierOut.model_validate(s) for s in items],
        total=total,
    )


@router.get(
    "/suppliers/{supplier_id}",
    response_model=ToolSupplierOut,
    summary="Get tool supplier by ID.",
)
async def get_supplier(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ToolSupplierOut:
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return ToolSupplierOut.model_validate(supplier)


@router.patch(
    "/suppliers/{supplier_id}",
    response_model=ToolSupplierOut,
    summary="Update tool supplier.",
)
async def update_supplier(
    supplier_id: uuid.UUID,
    payload: ToolSupplierUpdate,
    db: AsyncSession = Depends(get_db),
) -> ToolSupplierOut:
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(supplier, field, value)
    await db.commit()
    await db.refresh(supplier)
    return ToolSupplierOut.model_validate(supplier)


@router.delete(
    "/suppliers/{supplier_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete tool supplier and all its catalog entries (with Qdrant + graph cleanup).",
)
async def delete_supplier(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    # Cascade: clean up all entries first
    entries_result = await db.execute(
        select(ToolCatalogEntry.id).where(ToolCatalogEntry.supplier_id == supplier_id)
    )
    entry_ids = list(entries_result.scalars().all())

    if entry_ids:
        # Qdrant cleanup for all entries
        try:
            from app.vector.qdrant_store import delete_tool_catalog_by_supplier

            delete_tool_catalog_by_supplier(str(supplier_id))
        except Exception as exc:
            logger.warning("supplier_qdrant_cleanup_failed", error=str(exc))

        # Graph cleanup for all entries
        for eid in entry_ids:
            try:
                from app.domain.drawing_graph import delete_tool_catalog_graph

                await delete_tool_catalog_graph(eid, db)
            except Exception:
                pass

        await db.execute(
            sa_delete(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier_id)
        )

    await db.delete(supplier)
    await db.commit()


# ── Catalog Upload & Refresh ──────────────────────────────────────────────────


def _validate_catalog_upload(file_bytes: bytes, filename: str) -> None:
    """Reject what the ingestion pipeline cannot possibly handle.

    The upload path had no size check at all (unlike document ingest, which
    enforces settings.max_upload_size_mb): a 2 GB file was read fully into
    memory and only then failed somewhere downstream.
    """
    from app.config import settings

    if not file_bytes:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "empty_file", "message": f"Файл «{filename}» пуст."},
        )
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "file_too_large",
                "message": (
                    f"Файл «{filename}» — {len(file_bytes) // (1024 * 1024)} МБ, "
                    f"это больше лимита {settings.max_upload_size_mb} МБ."
                ),
            },
        )


def _enqueue_catalog_ingest(document_id: str) -> str:
    """Queue the ingestion task, failing loudly when the broker is unreachable.

    Previously the enqueue sat in a bare try/except: a dead broker produced
    HTTP 200 with task_id=null, the UI printed "каталог принят в обработку",
    and the file sat in storage forever with nothing scheduled to read it.
    """
    try:
        from app.tasks.catalog_ingest import ingest_catalog_document

        return ingest_catalog_document.delay(document_id).id
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller below
        logger.warning("catalog_ingest_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "catalog_ingest_enqueue_failed",
                "message": (
                    "Файл сохранён, но обработку поставить в очередь не удалось "
                    f"({str(exc)[:150]}). Повторите позже."
                ),
            },
        ) from exc


async def _accept_catalog_upload(
    db: AsyncSession,
    supplier: ToolSupplier,
    file_bytes: bytes,
    filename: str,
    *,
    party_id: uuid.UUID | None = None,
) -> CatalogImportResult:
    """Shared body of both upload endpoints: Document → links → job → queue."""
    from app.domain.catalog_documents import register_catalog_document

    _validate_catalog_upload(file_bytes, filename)
    try:
        registered = await register_catalog_document(
            db,
            supplier=supplier,
            file_bytes=file_bytes,
            filename=filename,
            party_id=party_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — storage/DB failure must be visible
        logger.warning("catalog_upload_store_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "catalog_store_failed",
                "message": f"Ошибка сохранения файла каталога: {str(exc)[:150]}",
            },
        ) from exc
    await db.commit()

    doc = registered.document
    task_id = _enqueue_catalog_ingest(str(doc.id))
    is_archive = Path(filename).suffix.lower() in ARCHIVE_SUFFIXES
    if registered.is_duplicate:
        message = (
            f"Файл «{filename}» уже загружался ранее — обработка запущена повторно "
            "для того же файла, дубликат не создан."
        )
    elif is_archive:
        message = f"Архив «{filename}» принят: файлы внутри будут обработаны по отдельности."
    else:
        message = f"Файл «{filename}» принят в обработку. Позиции появятся по мере разбора."

    return CatalogImportResult(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
        storage_path=doc.storage_path,
        status="duplicate"
        if registered.is_duplicate
        else ("unpacking" if is_archive else "queued"),
        document_id=doc.id,
        job_id=registered.job.id,
        message=message,
    )


@router.post(
    "/suppliers/{supplier_id}/catalog",
    response_model=CatalogImportResult,
    summary="Skill: tool_catalog.import — Upload and ingest supplier catalog (PDF/Excel/CSV/JSON).",
)
async def upload_catalog(
    supplier_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="PDF, Excel (.xlsx), CSV, or JSON catalog")],
    db: AsyncSession = Depends(get_db),
) -> CatalogImportResult:
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    return await _accept_catalog_upload(db, supplier, await file.read(), file.filename or "catalog")


@router.get(
    "/by-supplier/{party_id}/uploads",
    response_model=CatalogUploadsResponse,
    summary="Skill: tool_catalog.list_uploads — Uploaded catalog files and their processing state.",
)
async def list_catalog_uploads(
    party_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CatalogUploadsResponse:
    """Every catalog file of this party's suppliers, with pipeline stages.

    This is what makes ingestion honest in the UI: before it, an upload either
    silently produced entries or silently produced nothing.
    """
    from app.db.models import DocumentProcessingJob
    from app.domain.catalog_documents import catalog_documents_for_supplier

    supplier_ids = [
        row[0]
        for row in (
            await db.execute(
                select(ToolSupplier.id).where(ToolSupplier.main_supplier_id == party_id)
            )
        ).all()
    ]
    docs = await catalog_documents_for_supplier(db, supplier_ids)
    if not docs:
        return CatalogUploadsResponse(items=[], total=0)

    doc_ids = [d.id for d in docs]
    jobs = (
        (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id.in_(doc_ids))
                .order_by(DocumentProcessingJob.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_job: dict[uuid.UUID, object] = {}
    for job in jobs:
        latest_job.setdefault(job.document_id, job)

    counts = dict(
        (row[0], row[1])
        for row in (
            await db.execute(
                select(ToolCatalogEntry.source_document_id, func.count())
                .where(
                    ToolCatalogEntry.source_document_id.in_(doc_ids),
                    ToolCatalogEntry.is_active.is_(True),
                )
                .group_by(ToolCatalogEntry.source_document_id)
            )
        ).all()
    )

    items: list[CatalogUploadOut] = []
    for doc in docs:
        job = latest_job.get(doc.id)
        meta = doc.metadata_ or {}
        steps = [
            CatalogUploadStep(
                key=step.get("key", ""),
                label=step.get("label"),
                status=step.get("status", "pending"),
                error=step.get("error"),
                progress=step.get("progress"),
            )
            for step in (getattr(job, "pipeline_steps", None) or [])
            if isinstance(step, dict)
        ]
        parent = meta.get("parent_document_id")
        items.append(
            CatalogUploadOut(
                document_id=doc.id,
                file_name=doc.file_name,
                file_size=doc.file_size,
                uploaded_at=doc.created_at,
                status=getattr(job, "status", None) or "queued",
                current_step=getattr(job, "current_step", None),
                error=getattr(job, "error", None),
                steps=steps,
                entries_count=counts.get(doc.id, 0),
                is_archive=bool(meta.get("is_archive")),
                parent_document_id=uuid.UUID(parent) if parent else None,
                supplier_id=(
                    uuid.UUID(meta["tool_supplier_id"]) if meta.get("tool_supplier_id") else None
                ),
            )
        )
    return CatalogUploadsResponse(items=items, total=len(items))


@router.post(
    "/uploads/{document_id}/reingest",
    response_model=CatalogImportResult,
    summary="Skill: tool_catalog.reingest — Re-run ingestion for one uploaded catalog file.",
)
async def reingest_catalog_upload(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CatalogImportResult:
    from app.db.models import Document

    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Загрузка каталога не найдена")
    supplier_id = (doc.metadata_ or {}).get("tool_supplier_id")
    if not supplier_id:
        raise HTTPException(status_code=400, detail="Документ не привязан к поставщику")
    supplier = await db.get(ToolSupplier, uuid.UUID(supplier_id))
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    task_id = _enqueue_catalog_ingest(str(document_id))
    return CatalogImportResult(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
        document_id=document_id,
        storage_path=doc.storage_path,
        status="queued",
        message=f"Файл «{doc.file_name}» поставлен на повторную обработку.",
    )


@router.delete(
    "/uploads/{document_id}",
    summary="Skill: tool_catalog.delete_upload — Remove one uploaded catalog and its entries.",
)
async def delete_catalog_upload(
    document_id: uuid.UUID,
    mode: str = Query("all", pattern="^(data|file|all)$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Skill: tool_catalog.delete_upload — Remove one uploaded catalog.

    Delegates to /api/catalogs so there is one deletion path. The local copy
    knew nothing about the page registry (which holds a foreign key on the
    document) or the rendered images, so it would now fail outright on any
    page-parsed catalog and leave ~150 MB of images behind on the others.
    """
    from app.api.catalogs import delete_catalog

    return await delete_catalog(document_id, mode, db)


@router.post(
    "/suppliers/{supplier_id}/refresh",
    response_model=CatalogImportResult,
    summary="Skill: tool_catalog.refresh — Re-ingest the most recent catalog file of a supplier.",
)
async def refresh_catalog(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CatalogImportResult:
    """Re-run ingestion of the newest catalog document for this supplier.

    It no longer clears the supplier's entries: with several catalogs per
    supplier that would destroy the other files' data. Each file's own entries
    are replaced by the ingest itself, keyed by source_document_id.
    """
    from app.domain.catalog_documents import catalog_documents_for_supplier

    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    docs = await catalog_documents_for_supplier(db, [supplier_id])
    if not docs:
        raise HTTPException(
            status_code=404,
            detail="Нет ранее загруженных файлов каталога для этого поставщика",
        )
    doc = docs[0]
    task_id = _enqueue_catalog_ingest(str(doc.id))

    return CatalogImportResult(
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
        document_id=doc.id,
        storage_path=doc.storage_path,
        status="queued",
        message=f"Файл «{doc.file_name}» поставлен на повторную обработку.",
    )


@router.post(
    "/suppliers/{supplier_id}/ingest-web-source",
    response_model=IngestWebSourceResult,
    summary="Skill: tool_catalog.ingest_web_source — Structure one web_discover-fetched "
    "page into draft catalog entries for a supplier (Ф3, AGENT_AUTONOMY_ROADMAP.md).",
)
async def ingest_web_source(
    supplier_id: uuid.UUID,
    payload: IngestWebSourceRequest,
    db: AsyncSession = Depends(get_db),
) -> IngestWebSourceResult:
    """Draft-first: created entries get metadata.review_status="ingested" (or
    "needs_review" if they conflict with an existing entry — see
    _create_catalog_entries_from_rows) rather than being immediately final,
    unlike the manual upload_catalog path above. Low risk / ungated
    (creates draft data only, nothing destructive) but not recipe-replayable
    (see capabilities.yml non_recipeable_actions) — blindly replaying it with
    different fetched text on a different order would create duplicate
    catalog data, not the intended "same known action" a recipe replay is for.
    """
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    from app.tasks.drawing_analysis import ingest_web_catalog_source

    result = await ingest_web_catalog_source(
        db,
        str(supplier_id),
        url=payload.url,
        title=payload.title,
        text=payload.text,
        snippet=payload.snippet,
    )
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return IngestWebSourceResult(
        supplier_id=supplier_id,
        source_url=result["source_url"],
        entries_created=result["entries_created"],
        entries_conflicted=result["entries_conflicted"],
        entries_skipped=result["entries_skipped"],
        anomaly_ids=[uuid.UUID(a) for a in result["anomaly_ids"]],
        errors=result["errors"],
    )


# ── Catalog Entry CRUD ────────────────────────────────────────────────────────


@router.post(
    "/entries",
    response_model=ToolCatalogEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create tool catalog entry manually.",
)
async def create_entry(
    payload: ToolCatalogEntryCreate,
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogEntryOut:
    entry = ToolCatalogEntry(**payload.model_dump(exclude_none=False, by_alias=False))
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    # Async graph ingest
    try:
        from app.domain.drawing_graph import ingest_tool_catalog_graph

        await ingest_tool_catalog_graph(entry.id, db)
        await db.commit()
    except Exception:
        pass

    return ToolCatalogEntryOut.model_validate(entry)


@router.get(
    "/entries/{entry_id}",
    response_model=ToolCatalogEntryWithSupplierOut,
    summary="Get tool catalog entry with supplier info.",
)
async def get_entry(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogEntryWithSupplierOut:
    result = await db.execute(
        select(ToolCatalogEntry)
        .where(ToolCatalogEntry.id == entry_id)
        .options(selectinload(ToolCatalogEntry.supplier))
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    return ToolCatalogEntryWithSupplierOut.model_validate(entry)


@router.patch(
    "/entries/{entry_id}",
    response_model=ToolCatalogEntryOut,
    summary="Update tool catalog entry.",
)
async def update_entry(
    entry_id: uuid.UUID,
    payload: ToolCatalogEntryUpdate,
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogEntryOut:
    entry = await db.get(ToolCatalogEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    for field, value in payload.model_dump(exclude_none=True, by_alias=False).items():
        if field == "metadata_":
            entry.metadata_ = value
        else:
            setattr(entry, field, value)
    await db.commit()
    await db.refresh(entry)
    return ToolCatalogEntryOut.model_validate(entry)


@router.post(
    "/entries/{entry_id}/approve",
    response_model=ToolCatalogEntryOut,
    summary="Skill: tool_catalog.approve — Approve a draft/needs_review web-sourced "
    "catalog entry (Ф3.B, AGENT_AUTONOMY_ROADMAP.md). Approval-gated.",
)
async def approve_entry(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogEntryOut:
    """Clears metadata.review_status (present only on web-sourced entries —
    see ingest_web_source above; manually-created/uploaded entries have none
    and are unaffected by this whole review flow). Idempotent: approving an
    already-approved or never-gated entry is a no-op, not an error."""
    entry = await db.get(ToolCatalogEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    if entry.metadata_ and "review_status" in entry.metadata_:
        metadata = dict(entry.metadata_)
        metadata.pop("review_status")
        entry.metadata_ = metadata or None
        await db.commit()
        await db.refresh(entry)
    return ToolCatalogEntryOut.model_validate(entry)


class CatalogBulkDeleteRequest(BaseModel):
    entry_ids: list[uuid.UUID] = PydanticField(..., min_length=1, max_length=1000)


@router.delete(
    "/entries/bulk-delete",
    summary="Skill: tool_catalog.bulk_delete — Bulk delete catalog entries with Qdrant + graph cleanup.",
)
async def bulk_delete_entries(
    payload: CatalogBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    deleted = 0
    for entry_id in payload.entry_ids:
        entry = await db.get(ToolCatalogEntry, entry_id)
        if not entry:
            continue
        # Qdrant cleanup
        try:
            from app.vector.qdrant_store import delete_tool_catalog_entry

            delete_tool_catalog_entry(str(entry_id))
        except Exception:
            pass
        # Graph cleanup
        try:
            from app.domain.drawing_graph import delete_tool_catalog_graph

            await delete_tool_catalog_graph(entry_id, db)
        except Exception:
            pass
        await db.delete(entry)
        await db.flush()
        deleted += 1
    await db.commit()
    return {"deleted": deleted}


@router.delete(
    "/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Skill: tool_catalog.delete_entry — Delete catalog entry with Qdrant + graph cleanup.",
)
async def delete_entry(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    entry = await db.get(ToolCatalogEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Инструмент не найден")

    # Qdrant cleanup
    try:
        from app.vector.qdrant_store import delete_tool_catalog_entry

        delete_tool_catalog_entry(str(entry_id))
    except Exception as exc:
        logger.warning("entry_qdrant_cleanup_failed", entry_id=str(entry_id), error=str(exc))

    # Graph cleanup
    try:
        from app.domain.drawing_graph import delete_tool_catalog_graph

        await delete_tool_catalog_graph(entry_id, db)
    except Exception as exc:
        logger.warning("entry_graph_cleanup_failed", entry_id=str(entry_id), error=str(exc))

    await db.delete(entry)
    await db.commit()


# ── Search ────────────────────────────────────────────────────────────────────


@router.get(
    "/search",
    response_model=ToolCatalogListResponse,
    summary="Skill: tool_catalog.search — Search tool catalog by parameters and semantic query.",
)
async def search_tools(
    query: str | None = Query(None, description="Free-text search"),
    tool_type: ToolTypeEnum | None = Query(None),
    supplier_id: uuid.UUID | None = Query(None),
    diameter_min: float | None = Query(None),
    diameter_max: float | None = Query(None),
    material: str | None = Query(None),
    coating: str | None = Query(None),
    max_price: float | None = Query(None),
    semantic: bool = Query(True, description="Оставлен для совместимости"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogListResponse:
    """Skill: tool_catalog.search — Search catalog entries.

    Delegates to the hybrid search in api/catalogs.py so there is ONE ranking in
    the product. This endpoint used to run its own "vector OR ILIKE" branch and
    then sort by name, which meant two searches over the same data that
    disagreed with each other.
    """
    from app.api.catalogs import search_catalog
    from app.domain.catalogs import CatalogSearchRequest

    result = await search_catalog(
        CatalogSearchRequest(
            query=query,
            supplier_id=supplier_id,
            tool_type=tool_type.value if tool_type else None,
            diameter_min=diameter_min,
            diameter_max=diameter_max,
            price_max=max_price,
            page=page,
            page_size=page_size,
            include_facets=False,
        ),
        db,
    )

    ids = [item.id for item in result.items]
    entries: list[ToolCatalogEntry] = []
    if ids:
        rows = (
            (await db.execute(select(ToolCatalogEntry).where(ToolCatalogEntry.id.in_(ids))))
            .scalars()
            .all()
        )
        order = {entry_id: index for index, entry_id in enumerate(ids)}
        entries = sorted(rows, key=lambda entry: order.get(entry.id, 0))

    # Material/coating are not part of the hybrid contract; keep them as a
    # post-filter so the older callers' behaviour does not change.
    if material:
        entries = [e for e in entries if (e.material or "").lower().find(material.lower()) >= 0]
    if coating:
        entries = [e for e in entries if (e.coating or "").lower().find(coating.lower()) >= 0]

    return ToolCatalogListResponse(
        items=[ToolCatalogEntryOut.model_validate(entry) for entry in entries],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/suggest/{feature_id}",
    response_model=ToolSuggestionResponse,
    summary="Skill: tool_catalog.suggest — AI-powered tool suggestion for a drawing feature.",
)
async def suggest_tools_for_feature(
    feature_id: uuid.UUID,
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> ToolSuggestionResponse:
    # Load feature with dimensions and surfaces
    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id)
        .options(
            selectinload(DrawingFeature.dimensions),
            selectinload(DrawingFeature.surfaces),
            selectinload(DrawingFeature.drawing),
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент чертежа не найден")

    # Get material from drawing title block
    material = None
    if feature.drawing and feature.drawing.title_block:
        material = feature.drawing.title_block.get("material")

    # Determine likely tool types
    from app.ai.drawing_extractor import infer_tool_type_for_feature

    likely_tool_types = infer_tool_type_for_feature(
        feature.feature_type.value,
        [{"dim_type": d.dim_type.value, "nominal": d.nominal} for d in feature.dimensions],
    )

    # Get diameter hint
    diameter_hint: float | None = None
    for dim in feature.dimensions:
        if dim.dim_type.value == "diameter":
            diameter_hint = dim.nominal
            break

    # Fetch candidate tools from DB
    q = select(ToolCatalogEntry).where(ToolCatalogEntry.is_active.is_(True))
    if likely_tool_types:
        q = q.where(
            ToolCatalogEntry.tool_type.in_(
                [ToolTypeEnum(t) for t in likely_tool_types if _is_valid_tool_type(t)]
            )
        )
    if diameter_hint:
        margin = diameter_hint * 0.3
        q = q.where(
            or_(
                ToolCatalogEntry.diameter_mm.is_(None),
                and_(
                    ToolCatalogEntry.diameter_mm >= diameter_hint - margin,
                    ToolCatalogEntry.diameter_mm <= diameter_hint + margin,
                ),
            )
        )
    q = q.options(selectinload(ToolCatalogEntry.supplier)).limit(50)
    tools_result = await db.execute(q)
    candidate_tools = tools_result.scalars().all()

    # Check warehouse availability for each tool
    warehouse_qty: dict[str, float] = {}
    try:
        for tool in candidate_tools:
            if tool.part_number:
                inv_result = await db.execute(
                    select(InventoryItem).where(InventoryItem.sku == tool.part_number)
                )
                inv_item = inv_result.scalar_one_or_none()
                if inv_item and inv_item.current_qty > 0:
                    warehouse_qty[str(tool.id)] = inv_item.current_qty
    except Exception:
        pass

    # AI suggestion ranking
    model_used = None
    ai_suggestions: list[dict] = []
    if candidate_tools:
        from app.ai.drawing_extractor import suggest_tools_for_feature as ai_suggest

        feature_dict = {
            "feature_type": feature.feature_type.value,
            "name": feature.name,
            "description": feature.description,
            "dimensions": [
                {
                    "dim_type": d.dim_type.value,
                    "nominal": d.nominal,
                    "upper_tol": d.upper_tol,
                    "lower_tol": d.lower_tol,
                    "fit_system": d.fit_system,
                    "label": d.label,
                }
                for d in feature.dimensions
            ],
            "surfaces": [
                {"roughness_type": s.roughness_type.value, "value": s.value}
                for s in feature.surfaces
            ],
        }
        tools_for_ai = [
            {
                "entry_id": str(t.id),
                "tool_type": t.tool_type.value,
                "name": t.name,
                "diameter_mm": t.diameter_mm,
                "material": t.material,
                "coating": t.coating,
                "description": t.description,
            }
            for t in candidate_tools[:20]
        ]

        try:
            ai_suggestions = await ai_suggest(
                feature=feature_dict,
                available_tools=tools_for_ai,
                material=material,
            )
            model_used = "gemma3:4b"
        except Exception as exc:
            logger.warning("ai_tool_suggestion_failed", error=str(exc))

    # Build response
    entry_map = {str(t.id): t for t in candidate_tools}
    suggestions: list[ToolSuggestionItem] = []

    if ai_suggestions:
        for ai_item in ai_suggestions[:limit]:
            entry_id = ai_item.get("entry_id", "")
            entry = entry_map.get(entry_id)
            if not entry:
                continue
            suggestions.append(
                ToolSuggestionItem(
                    entry=ToolCatalogEntryOut.model_validate(entry),
                    supplier=ToolSupplierOut.model_validate(entry.supplier)
                    if entry.supplier
                    else None,
                    score=float(ai_item.get("score", 0.5)),
                    reason=ai_item.get("reason"),
                    warehouse_available=entry_id in warehouse_qty,
                    warehouse_qty=warehouse_qty.get(entry_id),
                )
            )
    else:
        # Fallback: return candidates by diameter match
        for tool in candidate_tools[:limit]:
            suggestions.append(
                ToolSuggestionItem(
                    entry=ToolCatalogEntryOut.model_validate(tool),
                    supplier=ToolSupplierOut.model_validate(tool.supplier)
                    if tool.supplier
                    else None,
                    score=0.5,
                    reason="Подобрано по типу инструмента",
                    warehouse_available=str(tool.id) in warehouse_qty,
                    warehouse_qty=warehouse_qty.get(str(tool.id)),
                )
            )

    return ToolSuggestionResponse(
        feature_id=feature_id,
        suggestions=suggestions,
        model_used=model_used,
    )


# ── By main supplier (party) ─────────────────────────────────────────────────


@router.get(
    "/by-supplier/{party_id}",
    response_model=ToolSupplierListResponse,
    summary="Get all ToolSupplier records linked to a Party (main supplier).",
)
async def list_tool_suppliers_by_party(
    party_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ToolSupplierListResponse:
    result = await db.execute(select(ToolSupplier).where(ToolSupplier.main_supplier_id == party_id))
    items = result.scalars().all()
    return ToolSupplierListResponse(
        items=[ToolSupplierOut.model_validate(s) for s in items],
        total=len(items),
    )


@router.post(
    "/by-supplier/{party_id}/catalog",
    response_model=CatalogImportResult,
    summary="Upload catalog directly against a Party supplier (auto-creates ToolSupplier if needed).",
)
async def upload_catalog_for_party(
    party_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="PDF, Excel (.xlsx), CSV, or JSON catalog")],
    db: AsyncSession = Depends(get_db),
) -> CatalogImportResult:
    from app.db.models import Party

    party = await db.get(Party, party_id)
    if not party:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    tool_supplier = await _get_or_create_tool_supplier_for_party(db, party)

    return await _accept_catalog_upload(
        db,
        tool_supplier,
        await file.read(),
        file.filename or "catalog",
        party_id=party_id,
    )


@router.get(
    "/by-supplier/{party_id}/entries",
    response_model=ToolCatalogListResponse,
    summary="List tool catalog entries for a Party supplier (across all linked ToolSuppliers).",
)
async def list_entries_by_party(
    party_id: uuid.UUID,
    tool_type: ToolTypeEnum | None = Query(None),
    query: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogListResponse:
    # Get all ToolSupplier IDs linked to this party
    ts_result = await db.execute(
        select(ToolSupplier.id).where(ToolSupplier.main_supplier_id == party_id)
    )
    tool_supplier_ids = [row[0] for row in ts_result.all()]

    if not tool_supplier_ids:
        return ToolCatalogListResponse(items=[], total=0, page=page, page_size=page_size)

    q = select(ToolCatalogEntry).where(
        ToolCatalogEntry.supplier_id.in_(tool_supplier_ids),
        ToolCatalogEntry.is_active.is_(True),
    )
    if tool_type:
        q = q.where(ToolCatalogEntry.tool_type == tool_type)
    if query:
        from sqlalchemy import or_

        q = q.where(
            or_(
                ToolCatalogEntry.name.ilike(f"%{query}%"),
                ToolCatalogEntry.part_number.ilike(f"%{query}%"),
                ToolCatalogEntry.description.ilike(f"%{query}%"),
            )
        )

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    q = q.order_by(ToolCatalogEntry.tool_type, ToolCatalogEntry.diameter_mm, ToolCatalogEntry.name)
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    items = result.scalars().all()

    return ToolCatalogListResponse(
        items=[ToolCatalogEntryOut.model_validate(e) for e in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── Supplier catalog entries list ─────────────────────────────────────────────


@router.get(
    "/suppliers/{supplier_id}/entries",
    response_model=ToolCatalogListResponse,
    summary="List all catalog entries for a supplier.",
)
async def list_supplier_entries(
    supplier_id: uuid.UUID,
    tool_type: ToolTypeEnum | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogListResponse:
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    q = select(ToolCatalogEntry).where(
        ToolCatalogEntry.supplier_id == supplier_id,
        ToolCatalogEntry.is_active.is_(True),
    )
    if tool_type:
        q = q.where(ToolCatalogEntry.tool_type == tool_type)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    q = q.order_by(ToolCatalogEntry.tool_type, ToolCatalogEntry.diameter_mm, ToolCatalogEntry.name)
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    items = result.scalars().all()

    return ToolCatalogListResponse(
        items=[ToolCatalogEntryOut.model_validate(e) for e in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_valid_tool_type(value: str) -> bool:
    try:
        ToolTypeEnum(value)
        return True
    except ValueError:
        return False


# ── Supplier resolution (name → Party → ToolSupplier) ─────────────────────────
#
# The agent knows a supplier by NAME; the catalog is keyed by ToolSupplier.id
# and procurement by Party.id. Every catalog action an agent can call goes
# through _resolve_supplier so a chat turn never has to invent a UUID — and so
# an ambiguous name comes back as CANDIDATES (the agent asks the user) instead
# of silently picking one.


async def _get_or_create_tool_supplier_for_party(db: AsyncSession, party: "Party") -> ToolSupplier:
    """Return the ToolSupplier linked to this Party, creating it on demand.

    Same find-or-create the GUI upload path uses (upload_catalog_for_party) —
    extracted so the agent-facing actions share one behaviour with the GUI.
    """
    result = await db.execute(
        select(ToolSupplier).where(ToolSupplier.main_supplier_id == party.id).limit(1)
    )
    tool_supplier = result.scalar_one_or_none()
    if tool_supplier:
        return tool_supplier
    tool_supplier = ToolSupplier(
        name=party.name,
        main_supplier_id=party.id,
        contact_info={
            "email": party.contact_email,
            "phone": party.contact_phone,
            "address": party.address,
        },
    )
    db.add(tool_supplier)
    await db.commit()
    await db.refresh(tool_supplier)
    return tool_supplier


async def _resolve_supplier(
    db: AsyncSession, ref: SupplierRefRequest
) -> tuple[ToolSupplier | None, ResolveSupplierResult]:
    """Resolve a supplier reference to exactly one ToolSupplier.

    Returns (supplier, result). ``supplier`` is None when the name matched
    nothing or several counterparties — the result then carries the candidates
    so the caller can ask a clarifying question rather than guess.
    """
    from app.db.models import Party

    if ref.supplier_id:
        supplier = await db.get(ToolSupplier, ref.supplier_id)
        if not supplier:
            raise HTTPException(status_code=404, detail="Поставщик каталога не найден")
        return supplier, ResolveSupplierResult(
            resolved=True,
            tool_supplier_id=supplier.id,
            party_id=supplier.main_supplier_id,
            name=supplier.name,
            message=f"Поставщик каталога: {supplier.name}",
        )

    if ref.party_id:
        party = await db.get(Party, ref.party_id)
        if not party:
            raise HTTPException(status_code=404, detail="Поставщик не найден")
        supplier = await _get_or_create_tool_supplier_for_party(db, party)
        return supplier, ResolveSupplierResult(
            resolved=True,
            tool_supplier_id=supplier.id,
            party_id=party.id,
            name=party.name,
            message=f"Поставщик: {party.name}",
        )

    name = (ref.supplier_name or "").strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail="Укажите поставщика: supplier_name, party_id или supplier_id",
        )

    # Match on the distinctive part of the name: "ООО Мир Станочника" and
    # "Мир Станочника" must resolve to the same counterparty, so the legal-form
    # prefix is stripped before the ILIKE.
    import re as _re

    core = _re.sub(r"^\s*(ООО|ОАО|ЗАО|ПАО|АО|ИП)\s+", "", name, flags=_re.IGNORECASE).strip(
        " \"«»'"
    )
    pattern = f"%{core or name}%"

    parties = (
        (await db.execute(select(Party).where(Party.name.ilike(pattern)).limit(10))).scalars().all()
    )
    if len(parties) == 1:
        supplier = await _get_or_create_tool_supplier_for_party(db, parties[0])
        return supplier, ResolveSupplierResult(
            resolved=True,
            tool_supplier_id=supplier.id,
            party_id=parties[0].id,
            name=parties[0].name,
            message=f"Поставщик: {parties[0].name}",
        )
    if len(parties) > 1:
        return None, ResolveSupplierResult(
            resolved=False,
            candidates=[SupplierCandidate(party_id=p.id, name=p.name, inn=p.inn) for p in parties],
            message=(
                f"По названию «{name}» найдено несколько поставщиков "
                f"({len(parties)}). Уточните, какой именно нужен."
            ),
        )

    # No Party — maybe a catalog-only supplier (tool vendor without invoices).
    tool_suppliers = (
        (await db.execute(select(ToolSupplier).where(ToolSupplier.name.ilike(pattern)).limit(10)))
        .scalars()
        .all()
    )
    if len(tool_suppliers) == 1:
        supplier = tool_suppliers[0]
        return supplier, ResolveSupplierResult(
            resolved=True,
            tool_supplier_id=supplier.id,
            party_id=supplier.main_supplier_id,
            name=supplier.name,
            message=f"Поставщик каталога: {supplier.name}",
        )
    if len(tool_suppliers) > 1:
        return None, ResolveSupplierResult(
            resolved=False,
            candidates=[
                SupplierCandidate(tool_supplier_id=s.id, name=s.name) for s in tool_suppliers
            ],
            message=(
                f"По названию «{name}» найдено несколько поставщиков каталога "
                f"({len(tool_suppliers)}). Уточните, какой именно нужен."
            ),
        )

    return None, ResolveSupplierResult(
        resolved=False,
        message=(
            f"Поставщик «{name}» не найден ни среди контрагентов, ни среди "
            "поставщиков каталога. Уточните название или создайте поставщика "
            "(action=create_supplier)."
        ),
    )


@router.post(
    "/resolve-supplier",
    response_model=ResolveSupplierResult,
    summary="Skill: tool_catalog.resolve_supplier — Resolve a supplier by name/party_id "
    "to a catalog supplier, or return candidates to ask the user about.",
)
async def resolve_supplier_endpoint(
    payload: SupplierRefRequest,
    db: AsyncSession = Depends(get_db),
) -> ResolveSupplierResult:
    _supplier, result = await _resolve_supplier(db, payload)
    return result


@router.post(
    "/attach-web-catalog",
    response_model=AttachWebCatalogResult,
    summary="Skill: tool_catalog.attach_web_catalog — Fetch catalog pages or PDFs by URL "
    "and attach their contents to a supplier's catalog (name → Party → ToolSupplier).",
)
async def attach_web_catalog(
    payload: AttachWebCatalogRequest,
    db: AsyncSession = Depends(get_db),
) -> AttachWebCatalogResult:
    """One call for "найди каталоги на сайте и прикрепи к поставщику".

    Before this existed the agent had to chain search.browse → (a supplier id it
    had no way to obtain) → tool_catalog.ingest_web_source, and in a live chat
    it never completed the chain: it published a summary table instead. The
    fetch (HTML or PDF, OCR included — app.api.web_search.fetch_page) and the
    draft-first ingestion (app.tasks.drawing_analysis.ingest_web_catalog_source,
    Ф3) are both reused unchanged; only the wiring is new.

    Several URLs are attached in ONE call and each source's outcome is reported
    separately: a supplier publishes a catalog per product line, and "прикрепи
    все каталоги" must not depend on the model remembering to loop. One dead
    link never fails the others — it comes back as an ``error`` row.
    """
    supplier, resolution = await _resolve_supplier(db, payload)
    if supplier is None:
        # Not an error the agent should retry — it's a question for the user.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "supplier_ambiguous",
                "message": resolution.message,
                "candidates": [c.model_dump(mode="json") for c in resolution.candidates],
            },
        )

    urls = payload.source_urls()
    discovery_note = ""
    if not urls:
        # "Найди каталоги и загрузи" is ONE intention, so it is one call: with no
        # urls given, discover them here instead of failing. The live turn this
        # fixes stopped after discovery and published a table of invoice items —
        # the model is not required to remember the second step any more.
        discovered = await discover_catalogs(
            DiscoverCatalogsRequest(
                **payload.model_dump(
                    include={
                        "supplier_name",
                        "supplier_id",
                        "party_id",
                        "inn",
                        "website",
                    }
                )
            ),
            db,
        )
        urls = [c.url for c in discovered.candidates if c.kind != "page"]
        discovery_note = f"Найдено файлов каталогов: {len(urls)}. "
        # A supplier usually publishes BOTH: PDFs per product line and product
        # pages in a web catalog ("их очень много по оборудованию, PDF и просто
        # на сайте"). Attaching only the files would quietly cover half the
        # request, so the site walk is started alongside them.
        site_pages = [c for c in discovered.candidates if c.kind == "page" and c.on_supplier_site]
        if urls and site_pages and discovered.website:
            try:
                from app.tasks.catalog_crawl import crawl_supplier_site

                crawl_task_id = crawl_supplier_site.delay(
                    str(supplier.id), discovered.website, 60, 3
                ).id
                discovery_note += (
                    f"Каталог опубликован ещё и страницами сайта — запущен обход "
                    f"{discovered.website} (задача {crawl_task_id[:8]}). "
                )
            except Exception as exc:  # noqa: BLE001 — files still proceed
                logger.warning("catalog_crawl_alongside_failed", error=str(exc)[:150])
        if not urls:
            page_urls = [c.url for c in discovered.candidates if c.on_supplier_site]
            if discovered.website:
                # Nothing downloadable: the catalog lives as site pages. Crawl it
                # rather than coming back empty-handed.
                try:
                    from app.tasks.catalog_crawl import crawl_supplier_site

                    task_id = crawl_supplier_site.delay(
                        str(supplier.id), discovered.website, 60, 3
                    ).id
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error_code": "catalog_crawl_enqueue_failed",
                            "message": f"Обход сайта не встал в очередь: {str(exc)[:150]}",
                        },
                    ) from exc
                return AttachWebCatalogResult(
                    tool_supplier_id=supplier.id,
                    party_id=supplier.main_supplier_id,
                    supplier_name=supplier.name,
                    source_url=discovered.website,
                    entries_created=0,
                    entries_conflicted=0,
                    task_id=task_id,
                    status="queued",
                    message=(
                        f"Файлов каталога у «{supplier.name}» нет — каталог опубликован "
                        f"страницами сайта {discovered.website}. Запущен обход сайта "
                        f"({len(page_urls)} разделов найдено); позиции появятся в каталоге "
                        "поставщика по мере разбора. Статус — tool_catalog.ingest_status."
                    ),
                    report=discovered.report,
                )
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "supplier_website_unknown",
                    "message": discovered.message,
                },
            )

    if not payload.wait:
        # Default path: hand the work to the worker and answer now. Parsing one
        # real PDF catalog is minutes of local-LLM time per fragment — a chat
        # turn holding that open (and the GPU with it) is the wrong shape.
        from app.domain.catalog_ingest_status import record_source_status

        queued = [
            AttachedSourceResult(url=url, status="queued", message="Поставлен в очередь загрузки.")
            for url in urls
        ]
        for item in queued:
            record_source_status(
                str(supplier.id),
                item.url,
                status="queued",
                message="Поставлен в очередь загрузки.",
            )
        task_id: str | None = None
        try:
            from app.tasks.catalog_ingest import (
                ingest_catalog_url,
                looks_like_catalog_file_url,
            )
            from app.tasks.drawing_analysis import ingest_web_catalog_sources

            # One task per source: a huge catalog must not eat the time budget
            # of the others (a shared task hit the worker's time limit mid-parse
            # and left the remaining sources stuck at "queued" — live finding).
            #
            # A FILE (pdf/xlsx/zip) is downloaded and run through the document
            # pipeline: the web-text path fetches only a few OCR'd pages, and on
            # a real 200 000-character PDF catalog it returned rows=0 while
            # reporting success. Pages keep the text/LLM path.
            task_ids = [
                (
                    ingest_catalog_url.delay(str(supplier.id), url)
                    if looks_like_catalog_file_url(url)
                    else ingest_web_catalog_sources.delay(
                        str(supplier.id), [url], payload.max_pages, payload.max_chunks
                    )
                ).id
                for url in urls
            ]
            task_id = task_ids[0] if task_ids else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("web_catalog_enqueue_failed", error=str(exc))
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "catalog_ingest_enqueue_failed",
                    "message": (
                        "Не удалось поставить загрузку каталога в очередь "
                        f"({str(exc)[:150]}). Попробуйте позже."
                    ),
                },
            ) from exc

        return AttachWebCatalogResult(
            tool_supplier_id=supplier.id,
            party_id=supplier.main_supplier_id,
            supplier_name=supplier.name,
            source_url=urls[0],
            status="queued",
            task_id=task_id,
            sources=queued,
            report=_attach_report_block(supplier.name, queued),
            message=(
                discovery_note + f"Взял в работу каталогов: {len(urls)} для «{supplier.name}». "
                "Разбор идёт в фоне (крупный каталог — несколько минут на файл); "
                "позиции появляются на карточке поставщика во вкладке «Каталог "
                "инструментов» по мере разбора. Текущий прогресс — action=ingest_status."
            ),
        )

    from app.api.web_search import WebFetchRequest, fetch_page
    from app.tasks.drawing_analysis import ingest_web_catalog_source

    sources: list[AttachedSourceResult] = []
    anomaly_ids: list[uuid.UUID] = []
    errors: list[str] = []
    first_final_url: str | None = None

    for url in urls:
        try:
            fetched = await fetch_page(
                WebFetchRequest(
                    url=url,
                    max_chars=200000,
                    ocr=payload.max_pages > 0,
                    ocr_max_pages=payload.max_pages,
                )
            )
        except HTTPException as exc:
            detail = exc.detail
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            sources.append(
                AttachedSourceResult(
                    url=url, status="error", message=f"Не удалось открыть источник: {message}"[:300]
                )
            )
            errors.append(f"{url}: {message}"[:300])
            continue

        first_final_url = first_final_url or fetched.final_url
        text = (fetched.text or "").strip()
        if len(text) < 200:
            # Honest failure: a JS-only page or a blocked download must not look
            # like a successful attach with zero entries.
            sources.append(
                AttachedSourceResult(
                    url=url,
                    title=fetched.title,
                    status="empty",
                    text_chars=len(text),
                    message=(
                        f"Читаемого текста нет ({len(text)} симв.) — JS-каталог или файл, "
                        "требующий скачивания. Нужна прямая ссылка на PDF/прайс."
                    ),
                )
            )
            continue

        result = await ingest_web_catalog_source(
            db,
            str(supplier.id),
            url=url,
            title=payload.title or fetched.title,
            text=text,
            snippet=None,
            max_chunks=payload.max_chunks,
        )
        if result.get("error"):
            sources.append(
                AttachedSourceResult(
                    url=url,
                    title=fetched.title,
                    status="error",
                    text_chars=len(text),
                    message=str(result["error"])[:300],
                )
            )
            errors.append(f"{url}: {result['error']}"[:300])
            continue

        created = int(result["entries_created"])
        conflicted = int(result["entries_conflicted"])
        anomaly_ids.extend(uuid.UUID(a) for a in result["anomaly_ids"])
        errors.extend(str(e)[:300] for e in result["errors"])
        sources.append(
            AttachedSourceResult(
                url=url,
                title=fetched.title,
                status="attached" if created or conflicted else "empty",
                entries_created=created,
                entries_conflicted=conflicted,
                entries_skipped=int(result["entries_skipped"]),
                text_chars=len(text),
                message=(
                    f"Добавлено позиций: {created}"
                    + (f", на проверку: {conflicted}" if conflicted else "")
                    if created or conflicted
                    else "Позиций каталога в тексте не найдено."
                ),
            )
        )

    total_created = sum(item.entries_created for item in sources)
    total_conflicted = sum(item.entries_conflicted for item in sources)
    attached_sources = [item for item in sources if item.status == "attached"]

    if total_created or total_conflicted:
        message = (
            f"В каталог поставщика «{supplier.name}» добавлено позиций: {total_created}"
            + (f", конфликтов на проверку: {total_conflicted}" if total_conflicted else "")
            + f" (источников обработано: {len(attached_sources)} из {len(urls)}). "
            "Записи черновые — видны на карточке поставщика, вкладка «Каталог инструментов»."
        )
    else:
        # Nothing attached from anything — an honest failure, not a "success"
        # with zero rows the agent can report as done.
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "catalog_source_empty",
                "message": (
                    f"Ни из одного из {len(urls)} источников не удалось выделить позиции "
                    f"каталога для «{supplier.name}». Нужна страница со списком "
                    "артикулов/прайсом или прямая ссылка на PDF-каталог."
                ),
                "sources": [item.model_dump(mode="json") for item in sources],
            },
        )

    return AttachWebCatalogResult(
        tool_supplier_id=supplier.id,
        party_id=supplier.main_supplier_id,
        supplier_name=supplier.name,
        source_url=urls[0],
        final_url=first_final_url,
        entries_created=total_created,
        entries_conflicted=total_conflicted,
        entries_skipped=sum(item.entries_skipped for item in sources),
        anomaly_ids=anomaly_ids,
        errors=errors,
        text_chars=sum(item.text_chars for item in sources),
        message=message,
        sources=sources,
        report=_attach_report_block(supplier.name, sources),
    )


def _readable_source_name(url: str, title: str | None = None) -> str:
    """Human-readable name for a catalog source.

    A direct PDF link carries its name percent-encoded ("%D0%9A%D0%B0..."),
    which is unreadable in the report table the user actually looks at.
    """
    from urllib.parse import unquote

    if title and not title.lower().endswith((".pdf", ".xls", ".xlsx", ".csv")):
        return title
    name = unquote(url.split("?")[0].rsplit("/", 1)[-1]) or url
    return unquote(title) if title else name


def _attach_report_block(supplier_name: str, sources: list[AttachedSourceResult]) -> dict:
    """Ready-to-publish workspace block for what was attached.

    Built here, from the real per-source outcome, so the agent publishes facts
    instead of re-describing them: the ``url`` column is typed ``link``, which
    the desktop grid renders as a clickable anchor (a ``text`` column, which
    the model picked on its own in the live session, is not clickable).
    """
    status_label = {
        "attached": "Добавлен в каталог",
        "empty": "Позиции не найдены",
        "error": "Ошибка",
        "queued": "В очереди на загрузку",
        "running": "Загружается…",
    }
    return {
        "title": f"{supplier_name} — загруженные каталоги",
        "columns": [
            {"key": "title", "header": "Каталог", "type": "text"},
            {"key": "url", "header": "Ссылка", "type": "link"},
            {"key": "status", "header": "Статус", "type": "text"},
            {"key": "entries", "header": "Позиций", "type": "number"},
            {"key": "note", "header": "Комментарий", "type": "text"},
        ],
        "rows": [
            {
                "title": _readable_source_name(item.url, item.title),
                "url": {"href": item.url, "label": item.url},
                "status": status_label.get(item.status, item.status),
                "entries": item.entries_created,
                "note": item.message,
            }
            for item in sources
        ],
    }


# ── Catalog discovery (find EVERY catalog a supplier publishes) ──────────────

_CATALOG_FILE_EXTENSIONS = (".pdf", ".xls", ".xlsx", ".csv")
# Words that mark a link as a catalog/price list, in the URL or the anchor text.
_CATALOG_WORDS = (
    "каталог",
    "katalog",
    "catalog",
    "catalogue",
    "прайс",
    "price",
    "прайс-лист",
    "прейскурант",
    "номенклатур",
    "ассортимент",
    "brochure",
    "брошюр",
)


async def _known_host_from_entries(db: AsyncSession, supplier_id: uuid.UUID) -> str | None:
    """The host the supplier's existing catalog entries came from.

    Found live: "ООО Мир Станочника" had 1046 entries ingested from mirstan.ru
    and an EMPTY website field, so discovery had no anchor and web search
    returned gloves and panel benders from unrelated hosts. The system already
    knew the site — it just never wrote it down.
    """
    from urllib.parse import urlparse

    rows = (
        (
            await db.execute(
                select(ToolCatalogEntry.metadata_).where(
                    ToolCatalogEntry.supplier_id == supplier_id,
                    ToolCatalogEntry.metadata_.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for meta in rows:
        url = (meta or {}).get("source_url") if isinstance(meta, dict) else None
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host:
            counts[host] = counts.get(host, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda host: counts[host])


def _name_tokens(name: str) -> list[str]:
    import re as _re

    cleaned = _re.sub(
        r"\b(ооо|оао|зао|ао|ип|пао|тд|торговый дом|llc|ltd|gmbh)\b",
        " ",
        name.lower().replace("ё", "е"),
    )
    return [token for token in _re.split(r"[^a-zа-я0-9]+", cleaned) if len(token) > 3]


async def _discover_official_site(name: str, inn: str | None) -> tuple[str | None, list[str]]:
    """Search for the supplier's own site and VERIFY it before trusting it.

    Verification matters more than the search: an unverified host turns every
    later step into noise (that is exactly how a glove price list ended up
    among a machine-tool supplier's "catalogs").
    """
    from app.api.web_search import (
        WebFetchRequest,
        WebSearchRequest,
        execute_web_search,
        fetch_page,
    )

    diagnostics: list[str] = []
    queries = [f"{name} официальный сайт"]
    if inn:
        queries.insert(0, f"{name} ИНН {inn} официальный сайт")
    tokens = _name_tokens(name)

    seen_hosts: list[str] = []
    for query in queries:
        try:
            found = await execute_web_search(WebSearchRequest(query=query, limit=8))
        except HTTPException as exc:
            diagnostics.append(f"site_search_failed:{str(exc.detail)[:100]}")
            continue
        for hit in found.results:
            from urllib.parse import urlparse

            host = (urlparse(hit.url).hostname or "").lower().removeprefix("www.")
            if not host or host in seen_hosts:
                continue
            # Aggregators and directories are never the supplier's own site.
            if any(
                bad in host
                for bad in (
                    "rusprofile",
                    "list-org",
                    "sbis",
                    "zachestnyibiznes",
                    "checko",
                    "vk.com",
                    "facebook",
                    "youtube",
                    "avito",
                    "yandex",
                    "google",
                    "wikipedia",
                    "2gis",
                    "flamp",
                    "spark-interfax",
                    "audit-it",
                )
            ):
                continue
            seen_hosts.append(host)
            if len(seen_hosts) > 6:
                break
            try:
                page = await fetch_page(
                    WebFetchRequest(url=f"https://{host}", max_chars=6000, ocr=False)
                )
            except HTTPException:
                continue
            text = (page.text or "").lower().replace("ё", "е")
            if inn and inn in text:
                return f"https://{host}", diagnostics
            if tokens and all(token in text for token in tokens[:2]):
                return f"https://{host}", diagnostics
    diagnostics.append("site_not_verified")
    return None, diagnostics


async def _ensure_supplier_website(
    db: AsyncSession, supplier: ToolSupplier, explicit: str | None = None
) -> tuple[str | None, list[str]]:
    """Resolve the supplier's site once and REMEMBER it.

    Order: what the caller passed → what the card already stores → the host the
    supplier's own catalog entries came from → a verified web search.
    """
    diagnostics: list[str] = []
    website = (explicit or supplier.website or "").strip() or None

    if not website:
        host = await _known_host_from_entries(db, supplier.id)
        if host:
            website = f"https://{host}"
            diagnostics.append(f"site_from_existing_entries:{host}")

    if not website:
        inn = None
        if supplier.main_supplier_id:
            from app.db.models import Party

            party = await db.get(Party, supplier.main_supplier_id)
            inn = getattr(party, "inn", None)
        website, search_diag = await _discover_official_site(supplier.name, inn)
        diagnostics.extend(search_diag)

    if website and not supplier.website:
        supplier.website = website
        await db.commit()
        diagnostics.append("site_saved_to_supplier")
    return website, diagnostics


def _mentions_supplier(url: str, title: str | None, name: str) -> bool:
    """Does an off-site link actually belong to this supplier?"""
    haystack = f"{url} {title or ''}".lower().replace("ё", "е")
    tokens = _name_tokens(name)
    return bool(tokens) and any(token in haystack for token in tokens)


def _discovery_message(name: str, website: str | None, ordered: list, files: list) -> str:
    """Say what to do next, not just what happened.

    The live failure this addresses: discovery returned links, the model had no
    instruction attached to them, and it fell back to publishing a table of
    invoice items — the one thing the request did not ask for.
    """
    if ordered:
        pages = len(ordered) - len(files)
        parts = [
            f"Найдено кандидатов каталогов: {len(ordered)} "
            f"(файлов PDF/Excel: {len(files)}, страниц: {pages})."
        ]
        if files:
            parts.append(
                "СЛЕДУЮЩИЙ ШАГ: вызови tool_catalog.attach_web_catalog с supplier_name "
                "и списком urls ВСЕХ найденных файлов — одним вызовом."
            )
        if pages and website:
            parts.append(
                "Если каталог опубликован страницами сайта, а не файлом — вызови "
                "tool_catalog.crawl_site (start_url — сайт поставщика), он соберёт "
                "позиции со страниц."
            )
        return " ".join(parts)

    if not website:
        return (
            f"Сайт поставщика «{name}» неизвестен, поэтому искать негде: поиск по "
            "одному названию возвращает чужие прайсы. СПРОСИ у пользователя адрес "
            "сайта или прямую ссылку на каталог — не публикуй вместо этого таблицу "
            "товаров из счетов."
        )
    return (
        f"На сайте {website} каталогов-файлов не найдено. Возможные действия: "
        "tool_catalog.crawl_site (собрать каталог со страниц сайта) или спроси у "
        "пользователя прямую ссылку на каталог."
    )


def _candidate_kind(url: str) -> str:
    path = url.split("?")[0].lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".xls", ".xlsx", ".csv")):
        return "spreadsheet"
    return "page"


_AGGREGATOR_HOSTS = (
    "rusprofile",
    "list-org",
    "sbis",
    "zachestnyibiznes",
    "checko",
    "bizorg",
    "expocentr",
    "spark-interfax",
    "audit-it",
    "2gis",
    "flamp",
    "yell.ru",
    "wikipedia",
    "avito",
    "vk.com",
    "facebook",
    "youtube",
)


def _normalize_candidate_url(url: str) -> str:
    """Drop the fragment so "…/catalog/", "…/catalog/#" and "…/catalog/#content"
    are one candidate instead of three (measured live on a real supplier site)."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _is_navigation_link(url: str) -> bool:
    """JS handlers and mail links are page furniture, not catalogs."""
    from urllib.parse import urlparse

    return urlparse(url).scheme in ("javascript", "mailto", "tel")


def _is_aggregator(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return any(bad in host for bad in _AGGREGATOR_HOSTS)


def _looks_like_catalog_link(url: str, text: str) -> bool:
    haystack = f"{url} {text}".lower()
    if _is_navigation_link(url) or _is_aggregator(url):
        return False
    if url.split("?")[0].lower().endswith(_CATALOG_FILE_EXTENSIONS):
        return True
    return any(word in haystack for word in _CATALOG_WORDS)


def _same_site(url: str, website: str | None) -> bool:
    if not website:
        return False
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    site_host = urlparse(website if "//" in website else f"https://{website}").hostname or ""
    site_host = site_host.lower().removeprefix("www.")
    return bool(site_host) and (host == site_host or host.endswith("." + site_host))


@router.post(
    "/crawl-site",
    response_model=CrawlSiteResult,
    summary="Skill: tool_catalog.crawl_site — Build a supplier catalog by walking their website.",
)
async def crawl_supplier_site_endpoint(
    payload: CrawlSiteRequest,
    db: AsyncSession = Depends(get_db),
) -> CrawlSiteResult:
    """For suppliers who publish no catalog file — the site itself is the catalog.

    Queued, never inline: a crawl is dozens of page fetches plus an LLM call per
    page. Entries land draft-first (discovery_method="web_crawl").
    """
    supplier, resolution = await _resolve_supplier(db, payload)
    if supplier is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "supplier_ambiguous",
                "message": resolution.message,
                "candidates": [c.model_dump(mode="json") for c in resolution.candidates],
            },
        )

    start_url = (payload.start_url or supplier.website or "").strip()
    if not start_url:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "supplier_website_unknown",
                "message": (
                    f"У поставщика «{supplier.name}» не указан сайт. "
                    "Передайте start_url или заполните сайт в карточке."
                ),
            },
        )
    if "//" not in start_url:
        start_url = f"https://{start_url}"

    try:
        from app.tasks.catalog_crawl import crawl_supplier_site

        task_id = crawl_supplier_site.delay(
            str(supplier.id), start_url, payload.max_pages, payload.max_depth
        ).id
    except Exception as exc:  # noqa: BLE001 — a dead broker must not read as success
        logger.warning("catalog_crawl_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "catalog_crawl_enqueue_failed",
                "message": f"Не удалось поставить обход сайта в очередь ({str(exc)[:150]}).",
            },
        ) from exc

    return CrawlSiteResult(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        start_url=start_url,
        task_id=task_id,
        status="queued",
        message=(
            f"Обход сайта {start_url} запущен (до {payload.max_pages} страниц). "
            "Позиции появятся по мере разбора; статус — в tool_catalog.ingest_status."
        ),
    )


@router.post(
    "/discover-catalogs",
    response_model=DiscoverCatalogsResult,
    summary="Skill: tool_catalog.discover_catalogs — Find every catalog / price list "
    "a supplier publishes (web search + scan of the supplier's own site).",
)
async def discover_catalogs(
    payload: DiscoverCatalogsRequest,
    db: AsyncSession = Depends(get_db),
) -> DiscoverCatalogsResult:
    """Answer "найди ВСЕ каталоги поставщика" in one call.

    Two passes, because neither alone is enough: a web search finds catalogs
    that are indexed (and PDFs on other hosts), while the supplier's own
    catalog page usually advertises its files through <a href> that no search
    engine surfaced. The page scan needs the links the browser sidecar now
    returns (``include_links``) — the readable text says nothing about what a
    "Скачать каталог" button points at.

    Returns candidates only; attaching is a separate, explicit call
    (attach_web_catalog) so "найди" and "прикрепи" stay distinguishable.
    """
    from app.api.web_search import (
        WebFetchRequest,
        WebSearchRequest,
        execute_web_search,
        fetch_page,
    )

    supplier, resolution = await _resolve_supplier(db, payload)
    if supplier is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "supplier_ambiguous",
                "message": resolution.message,
                "candidates": [c.model_dump(mode="json") for c in resolution.candidates],
            },
        )

    # Without a verified site every later step is noise — resolve (and store)
    # it first. Live finding: a supplier with 1046 entries already ingested
    # from their own site had an empty website field, so search-only discovery
    # returned work gloves and panel benders from unrelated hosts.
    website, diagnostics = await _ensure_supplier_website(db, supplier, payload.website)
    name = supplier.name

    queries = [f"{name} каталог pdf", f"{name} прайс-лист"]
    if website:
        from urllib.parse import urlparse

        host = urlparse(website if "//" in website else f"https://{website}").hostname or ""
        host = host.removeprefix("www.")
        if host:
            queries = [f"site:{host} каталог", f"site:{host} pdf прайс", *queries]

    candidates: dict[str, CatalogCandidate] = {}
    search_hits: list[tuple[str, str]] = []  # (url, title) — page candidates to scan
    for query in queries:
        try:
            found = await execute_web_search(WebSearchRequest(query=query, limit=10))
        except HTTPException as exc:
            diagnostics.append(f"search_failed:{query}:{str(exc.detail)[:120]}")
            continue
        for hit in found.results:
            hit_url = _normalize_candidate_url(hit.url)
            if not _looks_like_catalog_link(hit_url, hit.title or ""):
                continue
            search_hits.append((hit_url, hit.title or ""))
            if hit_url in candidates:
                continue
            candidates[hit_url] = CatalogCandidate(
                url=hit_url,
                title=hit.title or "",
                kind=_candidate_kind(hit_url),
                found_via="search",
                on_supplier_site=_same_site(hit_url, website),
            )
            # A supplier's site was unknown → learn it from the first on-site hit.
            if website is None and hit_url.startswith("http"):
                from urllib.parse import urlparse

                host = urlparse(hit_url).hostname or ""
                if host and name.split()[-1].lower()[:5] in host.lower():
                    website = f"https://{host}"

    # Pass 2 — open the supplier's own catalog pages and mine their file links.
    pages_to_scan: list[str] = []
    if website:
        base = (website if "//" in website else f"https://{website}").rstrip("/")
        # Russian machine-tool sites publish catalogs under many names; trying a
        # handful of them beats depending on one guess.
        pages_to_scan.extend(
            base + path
            for path in (
                "/catalog/",
                "/katalog/",
                "/price/",
                "/prays/",
                "/prajs/",
                "/produktsiya/",
                "/products/",
                "/oborudovanie/",
                "/",
            )
        )
    pages_to_scan.extend(url for url, _ in search_hits if _candidate_kind(url) == "page")
    seen_pages: list[str] = []
    # Second level: catalog sections found on the first pass are opened too —
    # a machine-tool supplier keeps one PDF per product line behind a section
    # page, so a single-level scan sees the sections and none of the files.
    queued_second_level: list[str] = []

    async def _scan(page_url: str, *, second_level: bool) -> None:
        try:
            fetched = await fetch_page(
                WebFetchRequest(url=page_url, max_chars=2000, ocr=False, include_links=True)
            )
        except HTTPException as exc:
            diagnostics.append(f"scan_failed:{page_url}:{str(exc.detail)[:120]}")
            return
        seen_pages.append(page_url)
        for link in fetched.links:
            link_url = _normalize_candidate_url(link.url)
            on_site = _same_site(link_url, website)
            if (
                not second_level
                and on_site
                and _candidate_kind(link_url) == "page"
                and _looks_like_catalog_link(link_url, link.text)
                and link_url not in queued_second_level
                and link_url not in seen_pages
            ):
                queued_second_level.append(link_url)
            if link_url in candidates or not _looks_like_catalog_link(link_url, link.text):
                continue
            candidates[link_url] = CatalogCandidate(
                url=link_url,
                title=link.text[:200],
                kind=_candidate_kind(link_url),
                found_via="site_scan",
                on_supplier_site=on_site,
            )

    for page_url in pages_to_scan:
        if len(seen_pages) >= payload.max_pages_to_scan:
            break
        if page_url in seen_pages:
            continue
        await _scan(page_url, second_level=False)

    for page_url in queued_second_level:
        if len(seen_pages) >= payload.max_pages_to_scan:
            break
        if page_url in seen_pages:
            continue
        await _scan(page_url, second_level=True)

    # Drop foreign hosts once the site is known. A search for a supplier's name
    # reliably returns other companies' price lists (measured: work gloves and
    # panel benders came back as "catalogs" of a machine-tool supplier), and a
    # list where 19 of 20 rows are wrong is worse than a short honest one.
    if website:
        foreign = [
            url
            for url, candidate in candidates.items()
            if not candidate.on_supplier_site
            and (
                # An off-site PAGE is a directory listing or a marketplace, never
                # this supplier's catalog. Only off-site FILES that carry the
                # supplier's name are worth keeping (a CDN-hosted PDF).
                candidate.kind == "page" or not _mentions_supplier(url, candidate.title, name)
            )
        ]
        for url in foreign:
            candidates.pop(url, None)
        if foreign:
            diagnostics.append(f"filtered_foreign_hosts:{len(foreign)}")

    # Files (PDF/Excel) on the supplier's own site first — those attach cleanly;
    # pages last, since many are JS-rendered listings with no readable text.
    ordered = sorted(
        candidates.values(),
        key=lambda c: (
            c.kind == "page",
            not c.on_supplier_site,
            c.url,
        ),
    )[: payload.max_candidates]

    files = [c for c in ordered if c.kind != "page"]
    kind_label = {"pdf": "PDF-каталог", "spreadsheet": "Прайс (таблица)", "page": "Страница"}
    report = {
        "title": f"{name} — найденные каталоги",
        "columns": [
            {"key": "title", "header": "Каталог", "type": "text"},
            {"key": "url", "header": "Ссылка", "type": "link"},
            {"key": "kind", "header": "Тип", "type": "text"},
            {"key": "where", "header": "Где найден", "type": "text"},
        ],
        "rows": [
            {
                "title": _readable_source_name(c.url, c.title),
                "url": {"href": c.url, "label": c.url},
                "kind": kind_label.get(c.kind, c.kind),
                "where": ("сайт поставщика" if c.on_supplier_site else "внешний источник")
                + (" (обход сайта)" if c.found_via == "site_scan" else " (поиск)"),
            }
            for c in ordered
        ],
    }
    return DiscoverCatalogsResult(
        tool_supplier_id=supplier.id,
        party_id=supplier.main_supplier_id,
        supplier_name=name,
        website=website,
        candidates=ordered,
        scanned_pages=seen_pages,
        diagnostics=diagnostics,
        report=report,
        message=_discovery_message(name, website, ordered, files),
    )


@router.post(
    "/ingest-status",
    response_model=CatalogIngestStatusResult,
    summary="Skill: tool_catalog.ingest_status — Progress of the background catalog "
    "ingestion for a supplier (what is queued, loading, attached or failed).",
)
async def catalog_ingest_status(
    payload: CatalogIngestStatusRequest,
    db: AsyncSession = Depends(get_db),
) -> CatalogIngestStatusResult:
    """What happened to each queued source — the honest answer to "как там каталоги?".

    Reads the per-source progress the worker writes, so the agent reports the
    real state instead of repeating what it queued minutes ago.
    """
    from app.domain.catalog_ingest_status import list_source_statuses

    supplier, resolution = await _resolve_supplier(db, payload)
    if supplier is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "supplier_ambiguous",
                "message": resolution.message,
                "candidates": [c.model_dump(mode="json") for c in resolution.candidates],
            },
        )

    records = list_source_statuses(str(supplier.id))

    # Сверяем с реальностью: записи живут неделю, а каталоги за это время могли
    # удалить — и «загружено: 312 позиций» превращается в ложь, которую агент
    # повторит как факт (найдено на стенде после полной очистки каталогов).
    #
    # Условие намеренно узкое: у поставщика НЕТ ни одного каталога, и запись
    # старше двух часов. Свежая запись законно опережает появление документа
    # (загрузка идёт), а сверять каждую по её URL нельзя — краул пишет статус
    # на корень сайта, документов с таким адресом не бывает вовсе.
    # Только "attached": оно означает конкретный прикреплённый файл, и его
    # можно сверить с документом. "done" пишет краул на корень сайта —
    # документа с таким адресом не бывает вовсе, сверять нечего.
    catalog_docs = (
        (
            await db.execute(
                select(Document).where(Document.doc_type == DocumentType.supplier_catalog)
            )
        )
        .scalars()
        .all()
    )
    by_url = {
        str((doc.metadata_ or {}).get("source_url")): doc
        for doc in catalog_docs
        if isinstance(doc.metadata_, dict) and (doc.metadata_ or {}).get("source_url")
    }
    known_urls = set(by_url)

    # Реальный прогресс вместо «поставлен в очередь». Постраничный разбор ведёт
    # свой учёт и статус источника не трогает, поэтому агент на вопрос «как там
    # каталоги» видел «queued, 0 позиций», пока в базе уже лежали сотни.
    for record in records:
        doc = by_url.get(str(record.get("url") or ""))
        if doc is None:
            continue
        entries_now = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ToolCatalogEntry)
                    .where(
                        ToolCatalogEntry.source_document_id == doc.id,
                        ToolCatalogEntry.is_active.is_(True),
                    )
                )
            ).scalar_one()
        )
        pages_total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(CatalogPage)
                    .where(CatalogPage.document_id == doc.id)
                )
            ).scalar_one()
        )
        pages_done = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(CatalogPage)
                    .where(
                        CatalogPage.document_id == doc.id,
                        CatalogPage.status.in_(("parsed", "skipped")),
                    )
                )
            ).scalar_one()
        )
        record["title"] = record.get("title") or doc.file_name
        record["entries_created"] = entries_now
        if pages_total and pages_done < pages_total:
            record["status"] = "running"
            record["message"] = (
                f"Разбор идёт: страница {pages_done} из {pages_total}, позиций уже {entries_now}."
            )
        else:
            record["status"] = "attached"
            record["message"] = (
                f"Разобран полностью: страниц {pages_total}, позиций {entries_now}."
                if pages_total
                else f"Позиций: {entries_now}."
            )

    async def _entries_around(moment: datetime) -> int:
        """Сколько позиций поставщика появилось в окне вокруг записи.

        Краул ("done") пишет статус на корень сайта — сверить его с документом
        нельзя. Зато его позиции создавались тогда же: если их не осталось,
        запись описывает данные, которых больше нет. Проверять «есть ли вообще
        свежие позиции» нельзя — их мог создать совсем другой источник, и
        позавчерашняя запись выглядела бы подтверждённой.
        """
        naive = moment.replace(tzinfo=None)
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ToolCatalogEntry)
                    .where(
                        ToolCatalogEntry.supplier_id == supplier.id,
                        ToolCatalogEntry.is_active.is_(True),
                        ToolCatalogEntry.created_at >= naive - timedelta(hours=1),
                        ToolCatalogEntry.created_at <= naive + timedelta(hours=6),
                    )
                )
            ).scalar_one()
        )

    now = datetime.now(UTC)
    for record in records:
        status_value = str(record.get("status") or "")
        if status_value == "done":
            try:
                written = datetime.fromisoformat(str(record.get("updated_at") or ""))
            except ValueError:
                continue
            if now - written < timedelta(hours=2):
                continue
            if await _entries_around(written):
                continue
            record["status"] = "stale"
            record["entries_created"] = 0
            record["message"] = "Позиций того обхода уже нет — запись устарела."
            continue
        if status_value != "attached":
            continue
        if str(record.get("url") or "") in known_urls:
            continue
        try:
            # Свежая запись законно опережает появление документа: загрузка
            # ещё идёт. Устаревает лишь то, что давно должно было стать
            # каталогом и не стало (или каталог потом удалили).
            if now - datetime.fromisoformat(str(record.get("updated_at") or "")) < timedelta(
                hours=2
            ):
                continue
        except ValueError:
            continue
        record["status"] = "stale"
        record["entries_created"] = 0
        record["message"] = "Каталога больше нет — запись о загрузке устарела."

    sources = [
        AttachedSourceResult(
            url=str(record.get("url") or ""),
            title=record.get("title"),
            status=str(record.get("status") or "queued"),
            entries_created=int(record.get("entries_created") or 0),
            entries_conflicted=int(record.get("entries_conflicted") or 0),
            message=str(record.get("message") or ""),
        )
        for record in records
        if record.get("url")
    ]
    created = sum(item.entries_created for item in sources)
    conflicted = sum(item.entries_conflicted for item in sources)
    pending = [item for item in sources if item.status in ("queued", "running")]

    stale = [item for item in sources if item.status == "stale"]

    if not sources:
        message = (
            f"Для «{supplier.name}» фоновых загрузок каталога не запускалось "
            "(или сведения о них уже истекли)."
        )
    elif pending:
        message = (
            f"Загрузка каталогов «{supplier.name}» идёт: готово "
            f"{len(sources) - len(pending)} из {len(sources)}, добавлено позиций: {created}."
        )
    else:
        message = (
            f"Загрузка каталогов «{supplier.name}» завершена: источников "
            f"{len(sources)}, добавлено позиций: {created}"
            + (f", на проверку: {conflicted}" if conflicted else "")
            + "."
        )
    if stale:
        message += f" Записей о ранее загруженных каталогах, которых больше нет: {len(stale)}."

    return CatalogIngestStatusResult(
        tool_supplier_id=supplier.id,
        party_id=supplier.main_supplier_id,
        supplier_name=supplier.name,
        in_progress=bool(pending),
        entries_created=created,
        entries_conflicted=conflicted,
        sources=[item.model_dump(mode="json") for item in sources],
        report=_attach_report_block(supplier.name, sources),
        message=message,
    )


class ReindexCatalogRequest(BaseModel):
    """Rebuild the tool-catalog vector index (e.g. after an embedding change)."""

    # The fixed-name collections carry no dimension in their name, so switching
    # the embedding model leaves them on the old size and every upsert fails.
    # Recreating is destructive by nature — hence explicit, not automatic.
    recreate_collection: bool = True
    limit: int = PydanticField(default=10000, ge=1, le=100000)


class ReindexCatalogResult(BaseModel):
    entries_total: int
    entries_indexed: int
    entries_failed: int
    collection: str
    dimension: int
    message: str = ""


@router.post(
    "/reindex",
    response_model=ReindexCatalogResult,
    summary="Skill: tool_catalog.reindex — Re-embed every catalog entry into Qdrant "
    "(use after switching the embedding model).",
)
async def reindex_catalog(
    payload: ReindexCatalogRequest,
    db: AsyncSession = Depends(get_db),
) -> ReindexCatalogResult:
    from app.ai.embeddings import embed_text as _embed_text
    from app.ai.embeddings import get_active_embedding_profile
    from app.vector.qdrant_store import (
        COLLECTION_TOOL_CATALOG,
        ensure_drawing_collections,
        upsert_tool_catalog_entry,
    )

    profile = get_active_embedding_profile()
    ensure_drawing_collections(profile.dimension, recreate_on_mismatch=payload.recreate_collection)

    rows = (
        (
            await db.execute(
                select(ToolCatalogEntry)
                .where(ToolCatalogEntry.is_active.is_(True))
                .limit(payload.limit)
            )
        )
        .scalars()
        .all()
    )

    indexed = failed = 0
    for entry in rows:
        text = " ".join(
            part
            for part in (
                entry.tool_type.value if entry.tool_type else "",
                entry.name,
                f"Ø{entry.diameter_mm}мм" if entry.diameter_mm else "",
                entry.material or "",
                entry.coating or "",
                entry.description or "",
            )
            if part
        )
        try:
            vector = await _embed_text(text)
            if not vector:
                failed += 1
                continue
            upsert_tool_catalog_entry(
                entry_id=str(entry.id),
                vector=vector,
                tool_type=entry.tool_type.value if entry.tool_type else "other",
                name=entry.name,
                supplier_id=str(entry.supplier_id) if entry.supplier_id else "",
                diameter_mm=entry.diameter_mm,
                material=entry.material,
            )
            entry.embedding_id = f"tool_catalog:{entry.id}"
            indexed += 1
        except Exception as exc:  # noqa: BLE001 — one bad row can't stop the rebuild
            failed += 1
            logger.warning(
                "tool_catalog_reindex_entry_failed", entry_id=str(entry.id), error=str(exc)[:200]
            )
    await db.commit()

    return ReindexCatalogResult(
        entries_total=len(rows),
        entries_indexed=indexed,
        entries_failed=failed,
        collection=COLLECTION_TOOL_CATALOG,
        dimension=profile.dimension,
        message=(
            f"Каталог переиндексирован моделью «{profile.model_key}» "
            f"({profile.dimension} измерений): {indexed} из {len(rows)}"
            + (f", ошибок: {failed}" if failed else "")
            + "."
        ),
    )
