"""FastAPI router for Tool Catalog — supplier CRUD, tool search, AI suggestions, catalog import.

Skill: tool_catalog.*, supplier_catalog.*
"""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    DrawingFeature,
    FeatureDimension,
    FeatureSurface,
    InventoryItem,
    ToolCatalogEntry,
    ToolSupplier,
    ToolTypeEnum,
)
from sqlalchemy import delete as sa_delete
from app.db.session import get_db
from pydantic import BaseModel, Field as PydanticField
from app.domain.tool_catalog import (
    CatalogImportResult,
    ToolCatalogEntryCreate,
    ToolCatalogEntryOut,
    ToolCatalogEntryUpdate,
    ToolCatalogEntryWithSupplierOut,
    ToolCatalogListResponse,
    ToolCatalogSearchRequest,
    ToolSuggestionResponse,
    ToolSuggestionItem,
    ToolSupplierCreate,
    ToolSupplierListResponse,
    ToolSupplierOut,
    ToolSupplierUpdate,
)

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

        await db.execute(sa_delete(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier_id))

    await db.delete(supplier)
    await db.commit()


# ── Catalog Upload & Refresh ──────────────────────────────────────────────────


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

    filename = file.filename or "catalog"
    file_bytes = await file.read()

    # Upload to MinIO
    storage_path = await _upload_catalog_to_minio(file_bytes, filename, str(supplier_id))

    if not storage_path:
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла каталога")

    # Enqueue ingestion task
    task_id = None
    try:
        from app.tasks.drawing_analysis import ingest_supplier_catalog
        task = ingest_supplier_catalog.delay(str(supplier_id), storage_path, filename)
        task_id = task.id
    except Exception as exc:
        logger.warning("catalog_ingest_enqueue_failed", error=str(exc))

    return CatalogImportResult(
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
    )


@router.post(
    "/suppliers/{supplier_id}/refresh",
    response_model=CatalogImportResult,
    summary="Skill: tool_catalog.refresh — Re-ingest the last uploaded catalog for a supplier.",
)
async def refresh_catalog(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CatalogImportResult:
    """Re-run catalog ingestion from MinIO for the last uploaded file.
    Also clears existing entries first so the catalog is rebuilt cleanly.
    """
    supplier = await db.get(ToolSupplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    # Find last uploaded catalog file in MinIO
    last_file_path: str | None = None
    last_filename: str | None = None
    try:
        from app.config import settings
        from minio import Minio
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        prefix = f"tool-catalogs/{supplier_id}/"
        objects = list(client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True))
        if objects:
            latest = max(objects, key=lambda o: o.last_modified or 0)
            last_file_path = latest.object_name
            last_filename = last_file_path.rsplit("/", 1)[-1]
    except Exception as exc:
        logger.warning("refresh_catalog_minio_list_failed", supplier_id=str(supplier_id), error=str(exc))

    if not last_file_path:
        raise HTTPException(
            status_code=404,
            detail="Нет ранее загруженных файлов каталога для этого поставщика",
        )

    # Enqueue re-ingestion
    task_id = None
    try:
        from app.tasks.drawing_analysis import ingest_supplier_catalog
        task = ingest_supplier_catalog.delay(str(supplier_id), last_file_path, last_filename)
        task_id = task.id
    except Exception as exc:
        logger.warning("refresh_catalog_enqueue_failed", error=str(exc))

    return CatalogImportResult(
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
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
    query: str | None = Query(None),
    tool_type: ToolTypeEnum | None = Query(None),
    supplier_id: uuid.UUID | None = Query(None),
    diameter_min: float | None = Query(None),
    diameter_max: float | None = Query(None),
    material: str | None = Query(None),
    coating: str | None = Query(None),
    max_price: float | None = Query(None),
    semantic: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ToolCatalogListResponse:
    # Semantic search via Qdrant
    semantic_ids: list[uuid.UUID] = []
    if query and semantic:
        try:
            from app.ai.embeddings import get_text_embedding
            from app.vector.qdrant_store import search_tool_catalog, ensure_drawing_collections

            ensure_drawing_collections()
            vector = await get_text_embedding(query)
            if vector:
                hits = search_tool_catalog(
                    query_vector=vector,
                    tool_type=tool_type.value if tool_type else None,
                    supplier_id=str(supplier_id) if supplier_id else None,
                    limit=100,
                )
                semantic_ids = [uuid.UUID(h["entry_id"]) for h in hits if h.get("entry_id")]
        except Exception as exc:
            logger.warning("tool_catalog_semantic_search_failed", error=str(exc))

    q = select(ToolCatalogEntry).where(ToolCatalogEntry.is_active.is_(True))

    if semantic_ids:
        q = q.where(ToolCatalogEntry.id.in_(semantic_ids))
    elif query:
        q = q.where(
            or_(
                ToolCatalogEntry.name.ilike(f"%{query}%"),
                ToolCatalogEntry.description.ilike(f"%{query}%"),
                ToolCatalogEntry.part_number.ilike(f"%{query}%"),
            )
        )

    if tool_type:
        q = q.where(ToolCatalogEntry.tool_type == tool_type)
    if supplier_id:
        q = q.where(ToolCatalogEntry.supplier_id == supplier_id)
    if diameter_min is not None:
        q = q.where(ToolCatalogEntry.diameter_mm >= diameter_min)
    if diameter_max is not None:
        q = q.where(ToolCatalogEntry.diameter_mm <= diameter_max)
    if material:
        q = q.where(ToolCatalogEntry.material.ilike(f"%{material}%"))
    if coating:
        q = q.where(ToolCatalogEntry.coating.ilike(f"%{coating}%"))
    if max_price is not None:
        q = q.where(
            or_(
                ToolCatalogEntry.price_value.is_(None),
                ToolCatalogEntry.price_value <= max_price,
            )
        )

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    q = q.order_by(ToolCatalogEntry.name)
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    items = result.scalars().all()

    return ToolCatalogListResponse(
        items=[ToolCatalogEntryOut.model_validate(e) for e in items],
        total=total,
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
        from sqlalchemy import cast, String
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
                    select(InventoryItem).where(
                        InventoryItem.sku == tool.part_number
                    )
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
                    supplier=ToolSupplierOut.model_validate(entry.supplier) if entry.supplier else None,
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
                    supplier=ToolSupplierOut.model_validate(tool.supplier) if tool.supplier else None,
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
    result = await db.execute(
        select(ToolSupplier).where(ToolSupplier.main_supplier_id == party_id)
    )
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

    # Find or create ToolSupplier linked to this party
    result = await db.execute(
        select(ToolSupplier).where(ToolSupplier.main_supplier_id == party_id).limit(1)
    )
    tool_supplier = result.scalar_one_or_none()

    if not tool_supplier:
        tool_supplier = ToolSupplier(
            name=party.name,
            main_supplier_id=party_id,
            contact_info={
                "email": party.contact_email,
                "phone": party.contact_phone,
                "address": party.address,
            },
        )
        db.add(tool_supplier)
        await db.commit()
        await db.refresh(tool_supplier)

    # Delegate to the existing upload logic
    filename = file.filename or "catalog"
    file_bytes = await file.read()
    storage_path = await _upload_catalog_to_minio(file_bytes, filename, str(tool_supplier.id))

    if not storage_path:
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла каталога")

    task_id = None
    try:
        from app.tasks.drawing_analysis import ingest_supplier_catalog
        task = ingest_supplier_catalog.delay(str(tool_supplier.id), storage_path, filename)
        task_id = task.id
    except Exception as exc:
        logger.warning("catalog_ingest_enqueue_failed", error=str(exc))

    return CatalogImportResult(
        supplier_id=tool_supplier.id,
        supplier_name=tool_supplier.name,
        entries_created=0,
        entries_updated=0,
        entries_skipped=0,
        task_id=task_id,
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


async def _upload_catalog_to_minio(
    file_bytes: bytes, filename: str, supplier_id: str
) -> str | None:
    try:
        import io as _io
        from app.config import settings
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        path = f"tool-catalogs/{supplier_id}/{filename}"
        client.put_object(
            settings.minio_bucket,
            path,
            _io.BytesIO(file_bytes),
            len(file_bytes),
        )
        return path
    except Exception as exc:
        logger.warning("catalog_minio_upload_failed", error=str(exc))
        return None
