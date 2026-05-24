"""FastAPI router for Drawings — upload, analysis, feature CRUD, contour editing, tool binding.

Skill: drawing.*
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Drawing,
    DrawingFeature,
    DrawingFeatureCorrection,
    DrawingStatus,
    FeatureContour,
    FeatureDimension,
    FeatureGDT,
    FeatureToolBinding,
    FeatureSurface,
    InventoryItem,
    ToolCatalogEntry,
    ToolSourceEnum,
)
from app.db.session import get_db
from app.domain.drawings import (
    ContoursUpdateRequest,
    DrawingAnalysisRequest,
    DrawingBulkDeleteRequest,
    DrawingBulkDeleteResponse,
    DrawingCreate,
    DrawingDeleteResult,
    DrawingFeatureCreate,
    DrawingFeatureOut,
    DrawingFeatureReviewRequest,
    DrawingFeatureUpdate,
    DrawingListResponse,
    DrawingOut,
    DrawingUpdate,
    DrawingUploadResponse,
    DrawingWithFeaturesOut,
    FeatureContourOut,
    FeatureCorrectionCreate,
    FeatureCorrectionOut,
    FeatureToolBindingCreate,
    FeatureToolBindingOut,
    FeatureToolBindingUpdate,
)

router = APIRouter()
logger = structlog.get_logger()

_FEATURE_LOAD_OPTIONS = [
    selectinload(DrawingFeature.contours),
    selectinload(DrawingFeature.dimensions),
    selectinload(DrawingFeature.surfaces),
    selectinload(DrawingFeature.gdt_annotations),
    selectinload(DrawingFeature.tool_binding),
]


# ── Upload & List ─────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=DrawingUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Skill: drawing.upload — Upload drawing file and enqueue analysis.",
)
async def upload_drawing(
    file: Annotated[UploadFile, File(description="DXF, DWG, PDF, STEP, IGES")],
    document_id: uuid.UUID | None = Query(None),
    drawing_number: str | None = Query(None),
    is_confidential: bool = Query(True, description="Конфиденциальный документ — только локальные модели"),
    db: AsyncSession = Depends(get_db),
) -> DrawingUploadResponse:
    from app.services.drawing_service import create_and_analyze_drawing, DRAWING_EXTENSIONS

    file_bytes = await file.read()
    filename = file.filename or "drawing"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"

    drawing, task_id = await create_and_analyze_drawing(
        file_bytes=file_bytes,
        filename=filename,
        fmt=ext,
        db=db,
        document_id=document_id,
        drawing_number=drawing_number,
        is_confidential=is_confidential,
        allow_cloud=False,
        max_views=6,
    )

    return DrawingUploadResponse(
        drawing_id=drawing.id,
        task_id=task_id,
        message="Чертёж загружен, анализ поставлен в очередь",
    )


@router.get(
    "",
    response_model=DrawingListResponse,
    summary="Skill: drawing.list — List drawings with filters.",
)
async def list_drawings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: DrawingStatus | None = Query(None),
    document_id: uuid.UUID | None = Query(None),
    drawing_number: str | None = Query(None),
    drawing_type: str | None = Query(None, description="Filter by drawing type: detail|assembly|section|weld"),
    part_class: str | None = Query(None, description="Filter by part class: shaft|housing|etc."),
    format: str | None = Query(None, description="Filter by file format: pdf|dxf|dwg|step|etc."),
    analysis_error: bool | None = Query(None, description="If True, only drawings with analysis errors"),
    db: AsyncSession = Depends(get_db),
) -> DrawingListResponse:
    q = select(Drawing)
    if status:
        q = q.where(Drawing.status == status)
    if document_id:
        q = q.where(Drawing.document_id == document_id)
    if drawing_number:
        q = q.where(Drawing.drawing_number.ilike(f"%{drawing_number}%"))
    if drawing_type:
        q = q.where(Drawing.drawing_type == drawing_type)
    if part_class:
        q = q.where(Drawing.part_class == part_class)
    if format:
        q = q.where(Drawing.format == format)
    if analysis_error is True:
        q = q.where(Drawing.analysis_error.isnot(None))
    elif analysis_error is False:
        q = q.where(Drawing.analysis_error.is_(None))

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    q = q.order_by(Drawing.created_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    items = result.scalars().all()

    return DrawingListResponse(
        items=[DrawingOut.model_validate(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{drawing_id}",
    response_model=DrawingWithFeaturesOut,
    summary="Skill: drawing.get — Get drawing with all features.",
)
async def get_drawing(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DrawingWithFeaturesOut:
    result = await db.execute(
        select(Drawing)
        .where(Drawing.id == drawing_id)
        .options(
            selectinload(Drawing.features).options(*_FEATURE_LOAD_OPTIONS)
        )
    )
    drawing = result.scalar_one_or_none()
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")
    return DrawingWithFeaturesOut.model_validate(drawing)


@router.patch(
    "/{drawing_id}",
    response_model=DrawingOut,
    summary="Skill: drawing.update — Update drawing metadata.",
)
async def update_drawing(
    drawing_id: uuid.UUID,
    payload: DrawingUpdate,
    db: AsyncSession = Depends(get_db),
) -> DrawingOut:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")
    for field, value in payload.model_dump(exclude_none=True, by_alias=False).items():
        if field == "metadata_":
            drawing.metadata_ = value
        else:
            setattr(drawing, field, value)
    await db.commit()
    await db.refresh(drawing)
    return DrawingOut.model_validate(drawing)


@router.delete(
    "/bulk-delete",
    response_model=DrawingBulkDeleteResponse,
    summary="Skill: drawing.bulk_delete — Bulk delete drawings with full cleanup.",
)
async def bulk_delete_drawings(
    payload: DrawingBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> DrawingBulkDeleteResponse:
    response = DrawingBulkDeleteResponse()
    for drawing_id in payload.drawing_ids:
        drawing = await db.get(Drawing, drawing_id)
        if not drawing:
            response.missing += 1
            continue

        storage_path = (drawing.metadata_ or {}).get("storage_path")

        await db.delete(drawing)
        await db.flush()

        # Graph cleanup
        try:
            from app.domain.drawing_graph import delete_drawing_graph
            await delete_drawing_graph(drawing_id, db)
        except Exception as exc:
            logger.warning("bulk_delete_drawing_graph_failed", drawing_id=str(drawing_id), error=str(exc))

        # Qdrant cleanup
        try:
            from app.vector.qdrant_store import delete_drawing as qdrant_delete
            qdrant_delete(str(drawing_id))
        except Exception:
            pass

        # MinIO cleanup
        if payload.delete_files and storage_path:
            try:
                await _delete_from_minio(storage_path)
            except Exception:
                pass

        response.results.append(DrawingDeleteResult(drawing_id=drawing_id, deleted=1))
        response.deleted += 1

    await db.commit()
    logger.info("drawings_bulk_deleted", count=response.deleted, missing=response.missing)
    return response


@router.delete(
    "/{drawing_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Skill: drawing.delete — Delete drawing and all features.",
)
async def delete_drawing(
    drawing_id: uuid.UUID,
    delete_files: bool = True,
    db: AsyncSession = Depends(get_db),
) -> None:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    storage_path = (drawing.metadata_ or {}).get("storage_path")

    await db.delete(drawing)
    await db.flush()

    # Graph cleanup
    try:
        from app.domain.drawing_graph import delete_drawing_graph
        await delete_drawing_graph(drawing_id, db)
    except Exception as exc:
        logger.warning("delete_drawing_graph_failed", drawing_id=str(drawing_id), error=str(exc))

    await db.commit()

    # Qdrant cleanup
    try:
        from app.vector.qdrant_store import delete_drawing as qdrant_delete
        qdrant_delete(str(drawing_id))
    except Exception:
        pass

    # MinIO cleanup
    if delete_files and storage_path:
        try:
            await _delete_from_minio(storage_path)
        except Exception:
            pass


@router.post(
    "/{drawing_id}/reanalyze",
    response_model=DrawingUploadResponse,
    summary="Skill: drawing.reanalyze — Rerun AI analysis for a drawing.",
)
async def reanalyze_drawing(
    drawing_id: uuid.UUID,
    payload: DrawingAnalysisRequest,
    db: AsyncSession = Depends(get_db),
) -> DrawingUploadResponse:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    drawing.status = DrawingStatus.uploaded
    drawing.analysis_error = None
    await db.commit()

    task_id = None
    try:
        from app.tasks.drawing_analysis import analyze_drawing
        task = analyze_drawing.delay(
            str(drawing_id),
            payload.model,
            payload.allow_cloud,
            payload.max_views,
            payload.force_drawing_type,
        )
        task_id = task.id
        drawing.celery_task_id = task_id
        await db.commit()
    except Exception as exc:
        logger.warning("drawing_reanalyze_enqueue_failed", error=str(exc))

    return DrawingUploadResponse(
        drawing_id=drawing_id,
        task_id=task_id,
        message="Повторный анализ поставлен в очередь",
    )


@router.post(
    "/bulk-reanalyze",
    summary="Skill: drawing.bulk_reanalyze — Reanalyze multiple drawings.",
)
async def bulk_reanalyze_drawings(
    drawing_ids: list[uuid.UUID] = __import__("fastapi").Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.tasks.drawing_analysis import analyze_drawing

    queued: int = 0
    not_found: list[str] = []

    for drawing_id in drawing_ids:
        drawing = await db.get(Drawing, drawing_id)
        if not drawing:
            not_found.append(str(drawing_id))
            continue
        drawing.status = DrawingStatus.uploaded
        drawing.analysis_error = None
        await db.flush()
        try:
            task = analyze_drawing.delay(str(drawing_id), None, False, 6, None)
            drawing.celery_task_id = task.id
            queued += 1
        except Exception as exc:
            logger.warning("bulk_reanalyze_enqueue_failed",
                           drawing_id=str(drawing_id), error=str(exc))

    await db.commit()
    logger.info("drawings_bulk_reanalyzed", queued=queued, not_found=len(not_found))
    return {"queued": queued, "not_found": not_found}


@router.get(
    "/{drawing_id}/download",
    summary="Skill: drawing.download — Download original drawing file.",
)
async def download_drawing_file(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> __import__("fastapi").responses.Response:
    import mimetypes
    from fastapi.responses import Response, StreamingResponse

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    storage_path = (drawing.metadata_ or {}).get("storage_path")
    if not storage_path:
        raise HTTPException(status_code=404, detail="Файл чертежа не найден в хранилище")

    try:
        file_bytes = await _load_from_minio(storage_path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла: {exc}")

    mime_type, _ = mimetypes.guess_type(drawing.filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    return Response(
        content=file_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{drawing.filename}\"",
        },
    )


@router.patch(
    "/{drawing_id}/status",
    response_model=DrawingOut,
    summary="Skill: drawing.set_status — Manually set drawing status.",
)
async def set_drawing_status(
    drawing_id: uuid.UUID,
    status: DrawingStatus = __import__("fastapi").Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
) -> DrawingOut:
    _allowed_transitions = {DrawingStatus.needs_review, DrawingStatus.approved, DrawingStatus.uploaded}
    if status not in _allowed_transitions:
        raise HTTPException(
            status_code=400,
            detail=f"Недопустимый статус. Разрешены: {[s.value for s in _allowed_transitions]}",
        )

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    drawing.status = status
    await db.commit()
    await db.refresh(drawing)
    return DrawingOut.model_validate(drawing)


@router.get(
    "/{drawing_id}/views",
    summary="Skill: drawing.views — Get segmented view sections for a multi-view drawing.",
)
async def get_drawing_views(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    from app.db.models import DrawingViewSection
    from sqlalchemy import select

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    result = await db.execute(
        select(DrawingViewSection)
        .where(DrawingViewSection.drawing_id == drawing_id)
        .order_by(DrawingViewSection.created_at)
    )
    sections = result.scalars().all()

    return [
        {
            "id": str(s.id),
            "drawing_id": str(s.drawing_id),
            "section_label": s.section_label,
            "section_type": s.section_type,
            "bbox_on_sheet": s.bbox_on_sheet,
            "image_path": s.image_path,
            "cutting_plane_label": s.cutting_plane_label,
            "cutting_plane_coords": s.cutting_plane_coords,
            "page_number": s.page_number,
            "confidence": s.confidence,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sections
    ]


@router.get(
    "/{drawing_id}/assembly-bom",
    summary="Skill: drawing.assembly_bom — Get BOM extracted from assembly drawing.",
)
async def get_assembly_bom(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    from app.db.models import DrawingAssemblyBOM
    from sqlalchemy import select

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    result = await db.execute(
        select(DrawingAssemblyBOM)
        .where(DrawingAssemblyBOM.drawing_id == drawing_id)
        .order_by(DrawingAssemblyBOM.item_no)
    )
    bom_items = result.scalars().all()

    return [
        {
            "id": str(b.id),
            "drawing_id": str(b.drawing_id),
            "item_no": b.item_no,
            "designation": b.designation,
            "quantity": b.quantity,
            "unit": b.unit,
            "material": b.material,
            "drawing_number": b.drawing_number,
            "note": b.note,
            "balloon_coords": b.balloon_coords,
            "confidence": b.confidence,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in bom_items
    ]


@router.get(
    "/{drawing_id}/validation",
    summary="Skill: drawing.validation — Get validation report for drawing extraction quality.",
)
async def get_drawing_validation(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    meta = drawing.metadata_ or {}
    report = meta.get("validation_report")
    if not report:
        return {
            "drawing_id": str(drawing_id),
            "status": "not_validated",
            "message": "Валидация ещё не выполнена. Запустите повторный анализ.",
        }

    return {"drawing_id": str(drawing_id), **report}


@router.get(
    "/{drawing_id}/uncertain-features",
    summary="Skill: drawing.uncertain_features — Features where VLM confidence is below threshold.",
)
async def get_uncertain_features(
    drawing_id: uuid.UUID,
    threshold: float = Query(default=0.70, ge=0.0, le=1.0, description="Confidence threshold"),
    db: AsyncSession = Depends(get_db),
) -> list[DrawingFeatureOut]:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.drawing_id == drawing_id, DrawingFeature.confidence < threshold)
        .options(*_FEATURE_LOAD_OPTIONS)
        .order_by(DrawingFeature.confidence.asc())
    )
    features = result.scalars().all()
    return [DrawingFeatureOut.model_validate(f) for f in features]


@router.post(
    "/{drawing_id}/features/{feature_id}/correct",
    summary="Skill: drawing.correct_feature — Apply user correction and record for few-shot learning.",
)
async def correct_drawing_feature(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: FeatureCorrectionCreate,
    db: AsyncSession = Depends(get_db),
) -> DrawingFeatureOut:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    feat_result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    feature = feat_result.scalar_one_or_none()
    if not feature or feature.drawing_id != drawing_id:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    correction = DrawingFeatureCorrection(
        drawing_id=drawing_id,
        feature_id=feature_id,
        original_type=payload.original_type or feature.feature_type.value,
        corrected_type=payload.corrected_type,
        original_name=feature.name,
        corrected_name=payload.corrected_name,
        confidence_at_correction=feature.confidence,
        drawing_type=(drawing.metadata_ or {}).get("drawing_type", "detail"),
        source_view=feature.source_view,
        context_json={
            "surrounding_types": [],
            "drawing_filename": (drawing.metadata_ or {}).get("original_filename", ""),
        },
        corrected_by=payload.corrected_by,
        note=payload.note,
    )
    db.add(correction)

    from app.db.models import DrawingFeatureType
    try:
        feature.feature_type = DrawingFeatureType(payload.corrected_type)
    except ValueError:
        pass
    if payload.corrected_name:
        feature.name = payload.corrected_name
    feature.confidence = 1.0
    feature.reviewed_at = datetime.now(timezone.utc)
    feature.reviewed_by = payload.corrected_by

    await db.commit()

    refreshed_result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    refreshed = refreshed_result.scalar_one()
    return DrawingFeatureOut.model_validate(refreshed)


@router.get(
    "/{drawing_id}/svg",
    summary="Get drawing SVG content.",
    response_class=__import__("fastapi").responses.Response,
)
async def get_drawing_svg(
    drawing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> __import__("fastapi").responses.Response:
    from fastapi.responses import Response

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")
    if not drawing.svg_path:
        raise HTTPException(status_code=404, detail="SVG ещё не сгенерирован")

    try:
        svg_bytes = await _load_from_minio(drawing.svg_path)
        return Response(content=svg_bytes, media_type="image/svg+xml")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки SVG: {exc}")


@router.get(
    "/{drawing_id}/thumbnail",
    summary="Get drawing thumbnail (PNG, max 400px). Generated from SVG on first request.",
    response_class=__import__("fastapi").responses.Response,
)
async def get_drawing_thumbnail(
    drawing_id: uuid.UUID,
    size: int = Query(default=400, ge=64, le=800, description="Max dimension in pixels"),
    db: AsyncSession = Depends(get_db),
) -> __import__("fastapi").responses.Response:
    from fastapi.responses import Response

    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    # 1. Serve from MinIO thumbnail_path if available
    if drawing.thumbnail_path:
        try:
            data = await _load_from_minio(drawing.thumbnail_path)
            return Response(content=data, media_type="image/png")
        except Exception:
            pass  # fall through to on-the-fly generation

    # 2. Generate on-the-fly from SVG
    if not drawing.svg_path:
        raise HTTPException(status_code=404, detail="Превью недоступно — SVG не сгенерирован")

    try:
        svg_bytes = await _load_from_minio(drawing.svg_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки SVG: {exc}")

    try:
        import cairosvg  # type: ignore[import]
        png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=size, output_height=size)
    except ImportError:
        # cairosvg not installed — return SVG with image/svg+xml as fallback
        return Response(content=svg_bytes, media_type="image/svg+xml")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка рендеринга превью: {exc}")

    # 3. Cache the thumbnail in MinIO for future requests
    thumb_path = drawing.svg_path.replace("/svg/", "/thumb/").replace(".svg", f"_{size}.png")
    try:
        await _upload_to_minio(thumb_path, png_bytes, "image/png")
        drawing.thumbnail_path = thumb_path
        db.add(drawing)
        await db.commit()
    except Exception:
        pass  # caching failure is non-fatal

    return Response(content=png_bytes, media_type="image/png")


# ── Features CRUD ─────────────────────────────────────────────────────────────


@router.get(
    "/{drawing_id}/features",
    response_model=list[DrawingFeatureOut],
    summary="Skill: drawing.get_features — Get all features of a drawing.",
)
async def get_features(
    drawing_id: uuid.UUID,
    feature_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[DrawingFeatureOut]:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    q = (
        select(DrawingFeature)
        .where(DrawingFeature.drawing_id == drawing_id)
        .options(*_FEATURE_LOAD_OPTIONS)
        .order_by(DrawingFeature.sort_order, DrawingFeature.created_at)
    )
    if feature_type:
        from app.db.models import DrawingFeatureType
        try:
            q = q.where(DrawingFeature.feature_type == DrawingFeatureType(feature_type))
        except ValueError:
            pass

    result = await db.execute(q)
    features = result.scalars().all()
    return [DrawingFeatureOut.model_validate(f) for f in features]


@router.post(
    "/{drawing_id}/features",
    response_model=DrawingFeatureOut,
    status_code=status.HTTP_201_CREATED,
    summary="Skill: drawing.create_feature — Create feature manually.",
)
async def create_feature(
    drawing_id: uuid.UUID,
    payload: DrawingFeatureCreate,
    db: AsyncSession = Depends(get_db),
) -> DrawingFeatureOut:
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise HTTPException(status_code=404, detail="Чертёж не найден")

    feature = DrawingFeature(
        drawing_id=drawing_id,
        feature_type=payload.feature_type,
        name=payload.name,
        description=payload.description,
        sort_order=payload.sort_order,
        confidence=1.0,
    )
    db.add(feature)
    await db.flush()

    for c in payload.contours:
        db.add(FeatureContour(feature_id=feature.id, **c.model_dump()))
    for d in payload.dimensions:
        db.add(FeatureDimension(feature_id=feature.id, **d.model_dump()))
    for s in payload.surfaces:
        db.add(FeatureSurface(feature_id=feature.id, **s.model_dump()))
    for g in payload.gdt_annotations:
        db.add(FeatureGDT(feature_id=feature.id, **g.model_dump()))

    await db.commit()

    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature.id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    return DrawingFeatureOut.model_validate(result.scalar_one())


@router.get(
    "/{drawing_id}/features/{feature_id}",
    response_model=DrawingFeatureOut,
    summary="Get a single drawing feature.",
)
async def get_feature(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DrawingFeatureOut:
    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id, DrawingFeature.drawing_id == drawing_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    return DrawingFeatureOut.model_validate(feature)


@router.patch(
    "/{drawing_id}/features/{feature_id}",
    response_model=DrawingFeatureOut,
    summary="Skill: drawing.update_feature — Update drawing feature metadata.",
)
async def update_feature(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: DrawingFeatureUpdate,
    db: AsyncSession = Depends(get_db),
) -> DrawingFeatureOut:
    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id, DrawingFeature.drawing_id == drawing_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    for field, value in payload.model_dump(exclude_none=True, by_alias=False).items():
        if field == "metadata_":
            feature.metadata_ = value
        else:
            setattr(feature, field, value)

    await db.commit()
    result2 = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    return DrawingFeatureOut.model_validate(result2.scalar_one())


@router.post(
    "/{drawing_id}/features/{feature_id}/review",
    response_model=DrawingFeatureOut,
    summary="Mark a drawing feature as reviewed.",
)
async def review_feature(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: DrawingFeatureReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> DrawingFeatureOut:
    result = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id, DrawingFeature.drawing_id == drawing_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    feature.reviewed_at = datetime.now(tz=timezone.utc)
    feature.reviewed_by = payload.reviewed_by
    await db.commit()

    result2 = await db.execute(
        select(DrawingFeature)
        .where(DrawingFeature.id == feature_id)
        .options(*_FEATURE_LOAD_OPTIONS)
    )
    return DrawingFeatureOut.model_validate(result2.scalar_one())


@router.delete(
    "/{drawing_id}/features/{feature_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Skill: drawing.delete_feature — Delete a drawing feature.",
)
async def delete_feature(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(DrawingFeature).where(
            DrawingFeature.id == feature_id,
            DrawingFeature.drawing_id == drawing_id,
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    await db.delete(feature)
    await db.commit()


# ── Contour Editing ───────────────────────────────────────────────────────────


@router.get(
    "/{drawing_id}/features/{feature_id}/contours",
    response_model=list[FeatureContourOut],
    summary="Get contour primitives for a feature.",
)
async def get_contours(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FeatureContourOut]:
    result = await db.execute(
        select(DrawingFeature).where(
            DrawingFeature.id == feature_id,
            DrawingFeature.drawing_id == drawing_id,
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    c_result = await db.execute(
        select(FeatureContour)
        .where(FeatureContour.feature_id == feature_id)
        .order_by(FeatureContour.sort_order)
    )
    return [FeatureContourOut.model_validate(c) for c in c_result.scalars().all()]


@router.put(
    "/{drawing_id}/features/{feature_id}/contours",
    response_model=list[FeatureContourOut],
    summary="Skill: drawing.update_contours — Replace all contour primitives for a feature (edit mode).",
)
async def update_contours(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: ContoursUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> list[FeatureContourOut]:
    result = await db.execute(
        select(DrawingFeature).where(
            DrawingFeature.id == feature_id,
            DrawingFeature.drawing_id == drawing_id,
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    # Delete existing contours
    existing = await db.execute(
        select(FeatureContour).where(FeatureContour.feature_id == feature_id)
    )
    for c in existing.scalars().all():
        await db.delete(c)
    await db.flush()

    # Create new contours (all marked as user-edited)
    new_contours = []
    for idx, c_data in enumerate(payload.contours):
        contour = FeatureContour(
            feature_id=feature_id,
            primitive_type=c_data.primitive_type,
            params=c_data.params,
            layer=c_data.layer,
            line_type=c_data.line_type,
            color=c_data.color,
            sort_order=c_data.sort_order if c_data.sort_order is not None else idx,
            is_user_edited=True,
        )
        db.add(contour)
        new_contours.append(contour)

    await db.commit()

    c_result = await db.execute(
        select(FeatureContour)
        .where(FeatureContour.feature_id == feature_id)
        .order_by(FeatureContour.sort_order)
    )
    return [FeatureContourOut.model_validate(c) for c in c_result.scalars().all()]


# ── Tool Binding ──────────────────────────────────────────────────────────────


@router.get(
    "/{drawing_id}/features/{feature_id}/tool",
    response_model=FeatureToolBindingOut | None,
    summary="Get tool binding for a feature.",
)
async def get_tool_binding(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> FeatureToolBindingOut | None:
    result = await db.execute(
        select(FeatureToolBinding).where(FeatureToolBinding.feature_id == feature_id)
    )
    binding = result.scalar_one_or_none()
    if not binding:
        return None
    return FeatureToolBindingOut.model_validate(binding)


@router.post(
    "/{drawing_id}/features/{feature_id}/tool",
    response_model=FeatureToolBindingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Skill: drawing.link_tool — Bind a tool to a drawing feature.",
)
async def bind_tool(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: FeatureToolBindingCreate,
    db: AsyncSession = Depends(get_db),
) -> FeatureToolBindingOut:
    result = await db.execute(
        select(DrawingFeature).where(
            DrawingFeature.id == feature_id,
            DrawingFeature.drawing_id == drawing_id,
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Элемент чертежа не найден")

    # Validate references
    if payload.tool_source == ToolSourceEnum.warehouse and payload.warehouse_item_id:
        item = await db.get(InventoryItem, payload.warehouse_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Инструмент на складе не найден")
    if payload.tool_source == ToolSourceEnum.catalog and payload.catalog_entry_id:
        entry = await db.get(ToolCatalogEntry, payload.catalog_entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Инструмент в каталоге не найден")

    # Upsert binding
    existing_result = await db.execute(
        select(FeatureToolBinding).where(FeatureToolBinding.feature_id == feature_id)
    )
    binding = existing_result.scalar_one_or_none()

    if binding:
        for field, value in payload.model_dump(exclude_none=False).items():
            setattr(binding, field, value)
    else:
        binding = FeatureToolBinding(feature_id=feature_id, **payload.model_dump())
        db.add(binding)

    await db.commit()
    await db.refresh(binding)

    # Update graph edge if catalog entry
    if payload.catalog_entry_id:
        try:
            from app.domain.drawing_graph import link_feature_to_tool_graph
            await link_feature_to_tool_graph(feature_id, payload.catalog_entry_id, db)
            await db.commit()
        except Exception:
            pass

    return FeatureToolBindingOut.model_validate(binding)


@router.patch(
    "/{drawing_id}/features/{feature_id}/tool",
    response_model=FeatureToolBindingOut,
    summary="Update tool binding for a feature.",
)
async def update_tool_binding(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    payload: FeatureToolBindingUpdate,
    db: AsyncSession = Depends(get_db),
) -> FeatureToolBindingOut:
    result = await db.execute(
        select(FeatureToolBinding).where(FeatureToolBinding.feature_id == feature_id)
    )
    binding = result.scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Привязка инструмента не найдена")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(binding, field, value)

    await db.commit()
    await db.refresh(binding)
    return FeatureToolBindingOut.model_validate(binding)


@router.delete(
    "/{drawing_id}/features/{feature_id}/tool",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove tool binding from a feature.",
)
async def remove_tool_binding(
    drawing_id: uuid.UUID,
    feature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(FeatureToolBinding).where(FeatureToolBinding.feature_id == feature_id)
    )
    binding = result.scalar_one_or_none()
    if binding:
        await db.delete(binding)
        await db.commit()


# ── MinIO helpers ─────────────────────────────────────────────────────────────


async def _upload_to_minio(
    file_bytes: bytes,
    filename: str,
    drawing_id_hint: str | None,
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
        folder = f"drawings/{drawing_id_hint or 'pending'}"
        path = f"{folder}/{filename}"
        client.put_object(
            settings.minio_bucket,
            path,
            _io.BytesIO(file_bytes),
            len(file_bytes),
        )
        return path
    except Exception as exc:
        logger.warning("minio_upload_failed", error=str(exc))
        return None


async def _delete_from_minio(path: str) -> None:
    from app.config import settings
    from minio import Minio
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    client.remove_object(settings.minio_bucket, path)


async def _load_from_minio(path: str) -> bytes:
    from app.config import settings
    from minio import Minio

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    response = client.get_object(settings.minio_bucket, path)
    data = response.read()
    response.close()
    response.release_conn()
    return data
