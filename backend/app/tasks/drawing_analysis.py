"""Celery tasks for drawing analysis and supplier catalog ingestion.

Handles:
- analyze_drawing: DXF/DWG/PDF parsing → SVG export → AI extraction → DB + Qdrant + Graph
- ingest_supplier_catalog: Multi-format catalog ingestion → normalize → embed → graph
"""

import asyncio
import functools
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


# ── Drawing Analysis Task ─────────────────────────────────────────────────────


# Supported raster formats for VLM analysis
RASTER_FORMATS = frozenset({"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp", "gif"})
# Supported vector formats
VECTOR_FORMATS = frozenset({"dxf", "dwg", "svg"})
# All supported formats
ALL_SUPPORTED_FORMATS = RASTER_FORMATS | VECTOR_FORMATS | frozenset({"pdf", "step", "iges", "stp"})


@celery_app.task(
    bind=True,
    name="drawing_analysis.analyze_drawing",
    max_retries=2,
    soft_time_limit=720,   # 12 min — VLM (qwen3.6:35b) classification + extraction
    time_limit=780,        # 13 min — hard kill after soft limit
)
def analyze_drawing(
    self,
    drawing_id: str,
    model: str | None = None,
    allow_cloud: bool = False,
    max_views: int = 6,
    force_drawing_type: str | None = None,
) -> dict:
    """
    Full drawing analysis pipeline:
    1. Load file from MinIO
    2. Parse format (DXF → entities + SVG, PDF → raster + OCR text)
    3. Preprocess: CLAHE, deskew, view segmentation (drawing_preprocessor)
    4. AI extraction via AIRouter: multi-view VLM features, dimensions, surfaces, GDT
    5. Save features to DB (with source_view / confidence_votes provenance)
    6. Embed drawing + features → Qdrant
    7. Build graph nodes
    8. Notify via WebSocket
    """
    return run_async(
        _analyze_drawing_async(drawing_id, model, allow_cloud, max_views, force_drawing_type)
    )


async def _analyze_drawing_async(
    drawing_id: str,
    model: str | None,
    allow_cloud: bool = False,
    max_views: int = 6,
    force_drawing_type: str | None = None,
) -> dict:

    from app.ai.drawing_extractor import extract_drawing_features, extract_features_from_image
    from app.ai.embeddings import embed_text as get_text_embedding
    from app.db.models import (
        Drawing,
        DrawingFeature,
        DrawingStatus,
        FeatureContour,
        FeatureDimension,
        FeatureGDT,
        FeatureSurface,
    )
    from app.db.session import _get_session_factory
    from app.domain.drawing_graph import ingest_drawing_graph
    from app.vector.qdrant_store import (
        ensure_drawing_collections,
        upsert_drawing,
        upsert_drawing_feature,
    )

    # AIRouter for policy-aware VLM dispatch (confidential = local only)
    router = None
    try:
        from app.ai.router import AIRouter
        router = AIRouter()
    except Exception as _router_exc:
        logger.warning("drawing_router_unavailable", error=str(_router_exc))

    drawing_uuid = uuid.UUID(drawing_id)

    async with _get_session_factory()() as db:
        drawing = await db.get(Drawing, drawing_uuid)
        if not drawing:
            logger.error("analyze_drawing_not_found", drawing_id=drawing_id)
            return {"error": "Drawing not found"}

        drawing.status = DrawingStatus.analyzing
        await db.flush()
        await db.commit()

    svg_content: str | None = None
    drawing_text: str = ""
    dxf_entities: list[dict] = []
    title_block: dict = {}
    image_bytes_for_vlm: bytes | None = None
    view_crops: list = []        # list[ViewCrop] from drawing_preprocessor
    step_geometry = None         # StepGeometryResult from step_extractor (STEP/IGES only)

    try:
        # VLM model is resolved by AIRouter from task_routing at dispatch time;
        # resolve a display name here only for logging.
        from app.ai.schemas import AITask as _AITask
        from app.ai.task_routing import resolve_model
        _routed_model, _ = resolve_model(_AITask.DRAWING_ANALYSIS_VLM)
        vlm_model = model or _routed_model or "auto"

        # Load file from MinIO
        file_bytes = await _load_drawing_file(drawing)
        fmt = (drawing.format or "").lower()

        if fmt == "dwg":
            # DWG is a proprietary binary format — convert to DXF first.
            # dwg2dxf (libredwg) handles R13–R2018 with ~90% entity coverage.
            logger.info("dwg_conversion_start", drawing_id=drawing_id, size=len(file_bytes))
            dxf_bytes = await _convert_dwg_to_dxf(file_bytes)
            if dxf_bytes:
                svg_content, dxf_entities, drawing_text = await _parse_dxf(
                    dxf_bytes, drawing.filename.replace(".dwg", ".dxf")
                )
                if svg_content:
                    image_bytes_for_vlm = await _svg_to_png_bytes(svg_content)
            else:
                # Conversion failed — record error, no geometry available
                logger.error(
                    "dwg_conversion_failed_no_fallback",
                    drawing_id=drawing_id,
                    filename=drawing.filename,
                )
                drawing_text = (
                    f"DWG файл: {drawing.filename}. "
                    "Конвертация в DXF не удалась — dwg2dxf недоступен или файл повреждён."
                )

        elif fmt == "dxf":
            svg_content, dxf_entities, drawing_text = await _parse_dxf(file_bytes, drawing.filename)
            if svg_content:
                image_bytes_for_vlm = await _svg_to_png_bytes(svg_content)

        elif fmt == "pdf":
            # SVG/text from first page (for viewer + OCR hint)
            svg_content, drawing_text = await _parse_pdf_drawing(file_bytes)
            # Multi-page preprocessing: CLAHE + deskew + each page as a ViewCrop
            try:
                from app.ai.drawing_preprocessor import preprocess_pdf_pages
                _page_limit = max(1, min(max_views, 10))
                view_crops = await asyncio.get_event_loop().run_in_executor(
                    None,
                    functools.partial(preprocess_pdf_pages, file_bytes, max_pages=_page_limit),
                )
                logger.info("pdf_pages_preprocessed", drawing_id=drawing_id, pages=len(view_crops))
            except Exception as _prep_exc:
                logger.warning("pdf_preprocessor_failed", error=str(_prep_exc))
            if not view_crops:
                image_bytes_for_vlm = await _pdf_to_png_bytes(file_bytes)

        elif fmt == "svg":
            svg_content = file_bytes.decode("utf-8", errors="replace")
            drawing_text = _extract_text_from_svg(svg_content)
            image_bytes_for_vlm = await _svg_to_png_bytes(svg_content)

        elif fmt in RASTER_FORMATS:
            image_bytes_for_vlm = await _normalize_raster_to_png(file_bytes, fmt)
            drawing_text = f"Изображение чертежа: {drawing.filename}"

        elif fmt in ("step", "stp", "iges"):
            # Primary path: pythonocc-core for real 3D geometry + orthographic views
            step_geometry = None
            try:
                from app.ai.step_extractor import extract_step_geometry
                step_geometry = await asyncio.get_event_loop().run_in_executor(
                    None,
                    functools.partial(extract_step_geometry, file_bytes, drawing.filename,
                                      generate_views=True),
                )
                drawing_text = (
                    f"3D файл: {drawing.filename}\n"
                    f"Изделия: {', '.join(step_geometry.product_names[:4])}\n"
                    f"Форма: {step_geometry.shape_class}, "
                    f"Граней: {step_geometry.face_count}, "
                    f"Объём: {step_geometry.volume_mm3:.1f} мм³\n"
                    f"BBox: X={step_geometry.bounding_box_mm.get('x_max', 0) - step_geometry.bounding_box_mm.get('x_min', 0):.1f} "
                    f"Y={step_geometry.bounding_box_mm.get('y_max', 0) - step_geometry.bounding_box_mm.get('y_min', 0):.1f} "
                    f"Z={step_geometry.bounding_box_mm.get('z_max', 0) - step_geometry.bounding_box_mm.get('z_min', 0):.1f} мм"
                )
                # Use rendered orthographic views as VLM input (if generated)
                if step_geometry.view_images:
                    from app.ai.drawing_preprocessor import ViewCrop
                    for view_name, view_png in step_geometry.view_images.items():
                        view_crops.append(ViewCrop(
                            view_type=view_name,
                            image_bytes=view_png,
                            bbox=(0, 0, 0, 0),
                            label=view_name,
                            confidence=0.9,
                        ))
                logger.info(
                    "step_geometry_extracted",
                    drawing_id=drawing_id,
                    source=step_geometry.source,
                    shape=step_geometry.shape_class,
                    views=len(view_crops),
                )
            except Exception as _step_exc:
                logger.warning("step_extractor_failed", error=str(_step_exc))

            # Fallback: text info-SVG (always works)
            if not drawing_text or not step_geometry:
                step_svg, drawing_text = _parse_step_to_info_svg(file_bytes, drawing.filename)
                if step_svg:
                    svg_content = step_svg
                    image_bytes_for_vlm = await _svg_to_png_bytes(svg_content)
            else:
                # Always generate info-SVG for the viewer regardless
                step_svg, _ = _parse_step_to_info_svg(file_bytes, drawing.filename)
                if step_svg:
                    svg_content = step_svg
        else:
            drawing_text = f"Файл: {drawing.filename} Формат: {fmt}"

        # ── Preprocess raster images (non-PDF, non-STEP) ─────────────────────
        # CLAHE + deskew + view segmentation via drawing_preprocessor
        if image_bytes_for_vlm and not view_crops and fmt not in ("step", "stp", "iges"):
            try:
                from app.ai.drawing_preprocessor import preprocess_drawing_image
                _preprocessed = await asyncio.get_event_loop().run_in_executor(
                    None,
                    functools.partial(
                        preprocess_drawing_image, image_bytes_for_vlm, fmt, max_views
                    ),
                )
                if _preprocessed.views:
                    view_crops = _preprocessed.views
                    logger.info(
                        "drawing_views_segmented",
                        drawing_id=drawing_id,
                        views=len(view_crops),
                        enhanced=_preprocessed.was_enhanced,
                    )
            except Exception as _prep_exc:
                logger.warning("drawing_preprocessor_failed", error=str(_prep_exc))

        # ── Assemble VLM input ────────────────────────────────────────────────
        # Prefer list of view crops; fall back to single rasterised image.
        vlm_images: bytes | list[bytes] | None = None
        view_labels_list: list[str] | None = None
        if view_crops:
            _valid = [(vc.image_bytes, vc.label) for vc in view_crops if vc.image_bytes]
            if _valid:
                vlm_images = [img for img, _ in _valid]
                view_labels_list = [lbl for _, lbl in _valid]
        if vlm_images is None:
            vlm_images = image_bytes_for_vlm

        # Drawing type: explicit override → else default "detail"
        drawing_type = force_drawing_type or "detail"

        # ── Stage 1: Classify drawing type from image (raster + PDF paths) ───────────
        classification = None
        if vlm_images is not None:
            try:
                from app.ai.drawing_extractor import classify_drawing_image
                _classify_img = vlm_images[0] if isinstance(vlm_images, list) else vlm_images
                classification = await classify_drawing_image(
                    _classify_img,
                    router=router,
                    drawing=drawing,
                    allow_cloud=allow_cloud,
                )
                if classification:
                    drawing_type = classification.drawing_type
                    # Prepend classification context to hint text for Stage-2
                    _cls_ctx = (
                        f"Тип чертежа: {classification.drawing_type}\n"
                        f"Класс изделия: {classification.part_class}\n"
                        f"Наименование: {classification.part_name}\n"
                        f"Виды: {', '.join(classification.views_present)}"
                    )
                    drawing_text = f"{_cls_ctx}\n\n{drawing_text}" if drawing_text else _cls_ctx
                    logger.info(
                        "drawing_classified",
                        drawing_id=drawing_id,
                        drawing_type=classification.drawing_type,
                        part_class=classification.part_class,
                        part_name=classification.part_name,
                        confidence=classification.confidence,
                    )
            except Exception as _cls_exc:
                logger.warning("drawing_classification_failed", error=str(_cls_exc))

        # ── Few-shot corrections (prioritised over VLM defaults) ─────────────
        few_shot: list[dict] = []
        try:
            async with _get_session_factory()() as db_fs:
                few_shot = await _load_few_shot_corrections(db_fs, drawing_type=drawing_type, limit=10)
        except Exception as _fs_exc:
            logger.warning("few_shot_load_failed", error=str(_fs_exc))

        # ── AI extraction ────────────────────────────────────────────────────
        # Strategy: VLM first (via AIRouter for policy enforcement), then text-based fallback
        if vlm_images:
            _view_count = len(vlm_images) if isinstance(vlm_images, list) else 1
            logger.info(
                "drawing_vlm_extraction",
                drawing_id=drawing_id,
                model=vlm_model,
                fmt=fmt,
                views=_view_count,
                drawing_type=drawing_type,
                few_shot_count=len(few_shot),
            )
            extraction = await extract_features_from_image(
                vlm_images,
                router=router,
                drawing=drawing,
                model=vlm_model,
                hint_text=drawing_text if drawing_text else None,
                drawing_type=drawing_type,
                view_labels=view_labels_list,
                allow_cloud=allow_cloud,
                few_shot_examples=few_shot or None,
                classification=classification,
            )
            # If VLM returned nothing meaningful → try rule-based DXF extraction first
            if not extraction.get("features") and dxf_entities:
                from app.ai.drawing_extractor import extract_features_from_dxf_entities
                rule_features = extract_features_from_dxf_entities(dxf_entities, drawing_type)
                if rule_features:
                    logger.info(
                        "drawing_dxf_rule_extraction",
                        drawing_id=drawing_id,
                        features=len(rule_features),
                    )
                    extraction["features"] = rule_features
            # If still nothing and text is available → LLM text fallback
            if not extraction.get("features") and drawing_text:
                logger.info("drawing_vlm_fallback_to_text", drawing_id=drawing_id)
                extraction = await extract_drawing_features(
                    drawing_text=drawing_text,
                    drawing_entities=dxf_entities or None,
                    model=vlm_model,
                )
        else:
            # Text-only path (STEP/IGES/unknown or all preprocessors failed)
            extraction = {"title_block": {}, "features": []}
            # Try rule-based first (faster, deterministic)
            if dxf_entities:
                from app.ai.drawing_extractor import extract_features_from_dxf_entities
                rule_features = extract_features_from_dxf_entities(dxf_entities, drawing_type)
                if rule_features:
                    logger.info(
                        "drawing_text_rule_extraction",
                        drawing_id=drawing_id,
                        features=len(rule_features),
                    )
                    extraction["features"] = rule_features
            if not extraction.get("features"):
                extraction = await extract_drawing_features(
                    drawing_text=drawing_text,
                    drawing_entities=dxf_entities or None,
                    model=vlm_model,
                )
        title_block = extraction.get("title_block", {})
        features_data = extraction.get("features", [])

        # ── Validation ───────────────────────────────────────────────────────
        # Validates extracted features; auto-fixes Ra/tolerance artifacts in-place.
        validation_report_dict: dict = {}
        try:
            from app.ai.drawing_validator import report_to_dict, validate_drawing_extraction
            val_report = validate_drawing_extraction(
                drawing_id=drawing_uuid,
                features_data=features_data,
                dxf_entities=dxf_entities or None,
            )
            validation_report_dict = report_to_dict(val_report)
        except Exception as _val_exc:
            logger.warning("drawing_validation_failed", error=str(_val_exc))

        # Save SVG to MinIO if generated
        svg_path = None
        thumbnail_path = None
        if svg_content:
            svg_path, thumbnail_path = await _save_svg_artifacts(
                drawing_id=drawing_id,
                svg_content=svg_content,
                drawing=drawing,
            )

        # Ensure Qdrant collections exist
        ensure_drawing_collections()

        async with _get_session_factory()() as db:
            drawing = await db.get(Drawing, drawing_uuid)
            if not drawing:
                return {"error": "Drawing not found after parse"}

            drawing.title_block = title_block
            drawing.svg_path = svg_path
            drawing.thumbnail_path = thumbnail_path
            drawing.drawing_number = (
                title_block.get("drawing_number") or drawing.drawing_number
            )
            drawing.drawing_type = drawing_type
            if classification:
                drawing.part_class = classification.part_class
                # Merge part_name into title_block if title is missing
                if not title_block.get("title") and classification.part_name:
                    title_block["title"] = classification.part_name
                    drawing.title_block = title_block
            # Persist 3D bounding box from STEP/IGES for blank selection
            if step_geometry and step_geometry.bounding_box_mm:
                drawing.bounding_box = step_geometry.bounding_box_mm
            # Store validation report in metadata; set status based on result
            if validation_report_dict:
                drawing.metadata_ = {
                    **(drawing.metadata_ or {}),
                    "validation_report": validation_report_dict,
                }
            # needs_review overrides analyzed status for human QA queue
            if validation_report_dict.get("needs_review"):
                drawing.status = DrawingStatus.needs_review
            else:
                drawing.status = DrawingStatus.analyzed

            await db.flush()

            features_created = []
            for idx, feat_data in enumerate(features_data[:100]):
                feature = DrawingFeature(
                    drawing_id=drawing_uuid,
                    feature_type=_safe_feature_type(feat_data.get("feature_type", "other")),
                    name=feat_data.get("name") or f"Элемент {idx + 1}",
                    description=feat_data.get("description"),
                    sort_order=idx,
                    confidence=float(feat_data.get("confidence", 0.5)),
                    source_view=feat_data.get("source_view"),
                    confirmed_by_views=feat_data.get("confirmed_by_views"),
                    confidence_votes=int(feat_data.get("confidence_votes", 1)),
                    ai_raw=feat_data,
                )
                db.add(feature)
                await db.flush()

                # Contours
                for c_data in feat_data.get("contours", [])[:50]:
                    contour = FeatureContour(
                        feature_id=feature.id,
                        primitive_type=_safe_primitive_type(c_data.get("primitive_type", "line")),
                        params=c_data.get("params") or {},
                        layer=c_data.get("layer"),
                        line_type=c_data.get("line_type", "solid"),
                    )
                    db.add(contour)

                # Dimensions
                for d_data in feat_data.get("dimensions", [])[:20]:
                    dim = FeatureDimension(
                        feature_id=feature.id,
                        dim_type=_safe_dim_type(d_data.get("dim_type", "linear")),
                        nominal=float(d_data.get("nominal", 0)),
                        upper_tol=_safe_float(d_data.get("upper_tol")),
                        lower_tol=_safe_float(d_data.get("lower_tol")),
                        unit=d_data.get("unit", "mm"),
                        fit_system=d_data.get("fit_system"),
                        label=d_data.get("label"),
                        annotation_position=d_data.get("annotation_position"),
                    )
                    db.add(dim)

                # Surfaces
                for s_data in feat_data.get("surfaces", [])[:10]:
                    surf = FeatureSurface(
                        feature_id=feature.id,
                        roughness_type=_safe_roughness_type(s_data.get("roughness_type", "Ra")),
                        value=float(s_data.get("value", 0)),
                        direction=s_data.get("direction"),
                        lay_symbol=s_data.get("lay_symbol"),
                        machining_required=bool(s_data.get("machining_required", True)),
                        annotation_position=s_data.get("annotation_position"),
                    )
                    db.add(surf)

                # GDT
                for g_data in feat_data.get("gdt", [])[:10]:
                    gdt = FeatureGDT(
                        feature_id=feature.id,
                        symbol=g_data.get("symbol", ""),
                        tolerance_value=float(g_data.get("tolerance_value", 0)),
                        tolerance_zone=g_data.get("tolerance_zone"),
                        datum_reference=g_data.get("datum_reference"),
                        material_condition=g_data.get("material_condition"),
                        annotation_position=g_data.get("annotation_position"),
                    )
                    db.add(gdt)

                features_created.append(feature)

            await db.flush()

            # Embed drawing → Qdrant
            drawing_text_for_embed = _build_drawing_embed_text(drawing, title_block, features_data)
            drawing_vector = await get_text_embedding(drawing_text_for_embed)
            if drawing_vector:
                upsert_drawing(
                    drawing_id=str(drawing_uuid),
                    vector=drawing_vector,
                    drawing_number=drawing.drawing_number,
                    status=drawing.status.value,
                    filename=drawing.filename,
                    title=title_block.get("title"),
                )
                drawing.embedding_id = f"drawing:{drawing_uuid}"

            # Embed each feature → Qdrant
            for feature in features_created:
                feat_text = _build_feature_embed_text(feature, feat_data)
                feat_vector = await get_text_embedding(feat_text)
                if feat_vector:
                    upsert_drawing_feature(
                        feature_id=str(feature.id),
                        vector=feat_vector,
                        drawing_id=str(drawing_uuid),
                        feature_type=feature.feature_type.value,
                        name=feature.name,
                        description=feature.description,
                    )
                    feature.embedding_id = f"drawing_feature:{feature.id}"

            # Build graph
            try:
                await ingest_drawing_graph(drawing_uuid, db)
            except Exception as graph_exc:
                logger.warning("drawing_graph_ingest_failed", error=str(graph_exc))

            drawing.status = DrawingStatus.analyzed
            await db.commit()

        # Notify via WebSocket
        await _notify_drawing_analyzed(drawing_id, len(features_created))

        logger.info(
            "drawing_analyzed",
            drawing_id=drawing_id,
            features=len(features_created),
        )
        return {
            "drawing_id": drawing_id,
            "features_count": len(features_created),
            "title_block": title_block,
            "svg_path": svg_path,
        }

    except Exception as exc:
        logger.error("analyze_drawing_failed", drawing_id=drawing_id, error=str(exc))
        async with _get_session_factory()() as db:
            drawing = await db.get(Drawing, drawing_uuid)
            if drawing:
                from app.db.models import DrawingStatus
                drawing.status = DrawingStatus.failed
                drawing.analysis_error = str(exc)[:2000]
                await db.commit()
        raise


# ── Assembly BOM Extraction Task ──────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="drawing_analysis.extract_assembly_bom",
    max_retries=2,
    soft_time_limit=300,   # 5 min — table detection + VLM parse + balloon OCR
    time_limit=360,
)
def extract_assembly_bom_task(
    self,
    drawing_id: str,
    allow_cloud: bool = False,
) -> dict:
    """Extract BOM (спецификация) from an assembly drawing.

    Standalone from analyze_drawing: OpenCV table detection + VLM parsing +
    balloon OCR (assembly_extractor.extract_assembly_bom), persisted as
    DrawingAssemblyBOM rows. Re-runnable — replaces prior rows for the drawing.
    """
    return run_async(_extract_assembly_bom_async(drawing_id, allow_cloud))


async def _extract_assembly_bom_async(drawing_id: str, allow_cloud: bool = False) -> dict:
    from sqlalchemy import delete

    from app.ai.assembly_extractor import extract_assembly_bom
    from app.db.models import Drawing, DrawingAssemblyBOM
    from app.db.session import _get_session_factory

    router = None
    try:
        from app.ai.router import AIRouter
        router = AIRouter()
    except Exception as _router_exc:
        logger.warning("assembly_bom_router_unavailable", error=str(_router_exc))

    drawing_uuid = uuid.UUID(drawing_id)

    async with _get_session_factory()() as db:
        drawing = await db.get(Drawing, drawing_uuid)
        if not drawing:
            logger.error("extract_assembly_bom_not_found", drawing_id=drawing_id)
            return {"error": "Drawing not found"}

    try:
        file_bytes = await _load_drawing_file(drawing)
        image_bytes = await _render_drawing_sheet_png(file_bytes, (drawing.format or "").lower())
        if image_bytes is None:
            logger.warning(
                "assembly_bom_no_raster", drawing_id=drawing_id, fmt=drawing.format
            )
            return {"error": "Не удалось получить растровое изображение чертежа"}

        result = await extract_assembly_bom(
            image_bytes,
            router=router,
            drawing=drawing,
            allow_cloud=allow_cloud,
        )

        async with _get_session_factory()() as db:
            # Idempotent re-run: replace prior BOM rows for this drawing rather
            # than appending duplicates.
            await db.execute(
                delete(DrawingAssemblyBOM).where(DrawingAssemblyBOM.drawing_id == drawing_uuid)
            )
            for item in result.items[:200]:
                db.add(DrawingAssemblyBOM(
                    drawing_id=drawing_uuid,
                    item_no=item.item_no,
                    designation=item.designation,
                    quantity=item.quantity,
                    unit=item.unit,
                    material=item.material,
                    drawing_number=item.drawing_number,
                    note=item.note,
                    balloon_coords=item.balloon_coords,
                    confidence=item.confidence,
                ))
            await db.commit()

        logger.info(
            "assembly_bom_task_completed",
            drawing_id=drawing_id,
            items=len(result.items),
            balloons=len(result.balloons),
        )
        return {
            "drawing_id": drawing_id,
            "items_count": len(result.items),
            "balloons_count": len(result.balloons),
            "table_bbox": list(result.table_bbox) if result.table_bbox else None,
            "confidence": result.confidence,
        }
    except Exception as exc:
        logger.error("extract_assembly_bom_failed", drawing_id=drawing_id, error=str(exc))
        raise


async def _render_drawing_sheet_png(file_bytes: bytes, fmt: str) -> bytes | None:
    """Render a drawing file to a single full-sheet PNG for BOM/balloon detection.

    Deliberately skips the multi-view segmentation used by analyze_drawing —
    the ГОСТ BOM table (upper-right) and position balloons are scattered
    across the WHOLE sheet, not confined to any one segmented view.
    """
    if fmt == "dwg":
        dxf_bytes = await _convert_dwg_to_dxf(file_bytes)
        if not dxf_bytes:
            return None
        svg_content, _, _ = await _parse_dxf(dxf_bytes, "drawing.dxf")
        return await _svg_to_png_bytes(svg_content) if svg_content else None
    if fmt == "dxf":
        svg_content, _, _ = await _parse_dxf(file_bytes, "drawing.dxf")
        return await _svg_to_png_bytes(svg_content) if svg_content else None
    if fmt == "svg":
        svg_content = file_bytes.decode("utf-8", errors="replace")
        return await _svg_to_png_bytes(svg_content)
    if fmt == "pdf":
        return await _pdf_to_png_bytes(file_bytes)
    if fmt in RASTER_FORMATS:
        return await _normalize_raster_to_png(file_bytes, fmt)
    return None


# ── Sync helper: create Drawing from Document (called from Celery sync tasks) ─


def _create_drawing_from_doc_sync(
    document_id: str, filename: str, fmt: str, storage_path: str
) -> None:
    """Sync helper: create Drawing record and enqueue analyze_drawing from a document."""

    async def _inner() -> None:
        from app.db.models import Drawing, DrawingStatus
        from app.db.session import _get_session_factory
        async with _get_session_factory()() as db:
            drawing = Drawing(
                document_id=uuid.UUID(document_id),
                filename=filename,
                format=fmt,
                is_confidential=True,
                status=DrawingStatus.uploaded,
                metadata_={"storage_path": storage_path, "from_document": True},
            )
            db.add(drawing)
            await db.commit()
            await db.refresh(drawing)
            try:
                analyze_drawing.delay(str(drawing.id), None, False, 6, None)
                logger.info(
                    "drawing_from_doc_enqueued",
                    document_id=document_id,
                    drawing_id=str(drawing.id),
                )
            except Exception as exc:
                logger.warning("drawing_from_doc_enqueue_failed", error=str(exc))

    run_async(_inner())


# ── Supplier Catalog Ingestion Task ───────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="drawing_analysis.ingest_supplier_catalog",
    max_retries=2,
    # A real price list is minutes of parsing plus one embedding per row; the
    # app-wide default (soft 300s) killed it halfway and, with acks_late, then
    # retried the same doomed work. Same treatment the web path already got.
    soft_time_limit=3600,
    time_limit=3660,
)
def ingest_supplier_catalog(self, supplier_id: str, file_path: str, filename: str) -> dict:
    """
    Parse supplier tool catalog file and ingest into DB + Qdrant + Graph.
    Supports: PDF (table extraction), Excel (.xlsx), CSV, JSON.
    """
    return run_async(
        _ingest_catalog_async(supplier_id, file_path, filename)
    )


async def _ingest_catalog_async(
    supplier_id: str, file_path: str, filename: str
) -> dict:
    from app.db.models import ToolSupplier
    from app.db.session import _get_session_factory
    from app.vector.qdrant_store import ensure_drawing_collections

    supplier_uuid = uuid.UUID(supplier_id)
    file_ext = Path(filename).suffix.lower()

    # Load catalog file
    catalog_bytes = await _load_catalog_file(file_path)
    if not catalog_bytes:
        return {"error": "Could not load catalog file"}

    # Parse based on format
    rows = await _parse_catalog_file(catalog_bytes, file_ext, filename)
    logger.info("catalog_rows_parsed", supplier_id=supplier_id, rows=len(rows))

    ensure_drawing_collections()

    async with _get_session_factory()() as db:
        supplier = await db.get(ToolSupplier, supplier_uuid)
        if not supplier:
            return {"error": f"Supplier {supplier_id} not found"}

        # discovery_method="manual_upload" (the default _create_catalog_entries_from_rows
        # falls back to when provenance omits it) deliberately does NOT set
        # metadata_.review_status — manual uploads through this endpoint stay
        # immediately usable, unchanged from before Ф3 added the web-sourced
        # draft-first path below.
        result = await _create_catalog_entries_from_rows(db, supplier_uuid, rows)
        await db.commit()

    logger.info(
        "catalog_ingested",
        supplier_id=supplier_id,
        created=result["created"],
        skipped=result["skipped"],
        skipped_by_reason=result.get("skipped_by_reason") or {},
        rows_parsed=len(rows),
    )
    return {
        "supplier_id": supplier_id,
        "entries_created": result["created"],
        "entries_updated": 0,
        "entries_skipped": result["skipped"],
        "errors": result["errors"][:10],
    }


async def _create_catalog_entries_from_rows(
    db: Any,
    supplier_uuid: uuid.UUID,
    rows: list[dict[str, Any]],
    *,
    provenance: dict[str, Any] | None = None,
    source_document_id: uuid.UUID | None = None,
    # Off by default: the pre-pass costs an LLM call, so only the real ingestion
    # paths turn it on — unit tests and cheap callers stay dictionary-only.
    infer_types_with_llm: bool = False,
) -> dict[str, Any]:
    """Ф3 (AGENT_AUTONOMY_ROADMAP.md): the entry-creation/embed/graph loop,
    split out of _ingest_catalog_async so both the file-upload path (above)
    and the web-sourced path (ingest_web_catalog_source below) share exactly
    one code path for turning normalized rows into ToolCatalogEntry rows —
    same Qdrant upsert, same graph ingestion, same field mapping, instead of
    two copies that would drift.

    provenance (only set by the web-sourced caller — the file-upload path
    passes none, preserving its pre-Ф3 behaviour exactly):
      - discovery_method: "web_discover" | "manual_upload" (default)
      - source_url, fetched_at, title: stored in metadata_ verbatim when present

    Draft-first: when discovery_method != "manual_upload", new entries get
    metadata_.review_status="ingested" and a supplier_id+part_number collision
    against an existing entry becomes an AnomalyCard instead of a silent
    overwrite — the existing entry is left untouched, the new one is created
    alongside as "needs_review" so a human can compare and decide (see
    backend/app/api/tool_catalog.py's approve endpoint).
    """
    from app.ai.embeddings import embed_text as get_text_embedding
    from app.db.models import AnomalyCard, AnomalySeverity, AnomalyStatus, AnomalyType, ToolCatalogEntry, ToolTypeEnum
    from app.domain.drawing_graph import ingest_tool_catalog_graph
    from app.vector.qdrant_store import upsert_tool_catalog_entry
    from sqlalchemy import select

    prov = dict(provenance or {})
    discovery_method = prov.get("discovery_method", "manual_upload")
    is_web_sourced = discovery_method != "manual_upload"

    # Pre-pass: whatever the dictionary cannot classify goes to the model in
    # batches — one call per 50 names instead of per row, and only for the
    # leftovers. The row is kept either way; this only improves its type.
    llm_types: dict[str, str] = {}
    if infer_types_with_llm:
        unresolved_names = []
        for row in rows:
            if row.get("tool_type") or not row.get("name"):
                continue
            if _infer_tool_type(row.get("name"), row.get("description"), row.get("part_number")):
                continue
            unresolved_names.append(str(row["name"])[:200])
        if unresolved_names:
            llm_types = await _infer_tool_types_via_llm(sorted(set(unresolved_names)))

    created = 0
    conflicted = 0
    skipped = 0
    pending_embeddings: list[tuple[Any, str, str]] = []
    # Why rows were dropped, not just how many — "0 из 5000" with no reason is
    # what made a silently-empty catalog look like a successful import.
    skipped_by_reason: dict[str, int] = {}
    errors: list[str] = []
    anomaly_ids: list[uuid.UUID] = []

    for row in rows:
        try:
            if not row.get("name"):
                skipped += 1
                skipped_by_reason["no_name"] = skipped_by_reason.get("no_name", 0) + 1
                continue

            # The type is derived, never required: column → name heuristic →
            # "other". A row is no longer lost just because the supplier's price
            # list has no "тип инструмента" column (it almost never does).
            raw_tool_type = row.get("tool_type") or ""
            if raw_tool_type:
                tool_type_str = _normalize_tool_type(raw_tool_type)
            else:
                tool_type_str = (
                    _infer_tool_type(
                        row.get("name"), row.get("description"), row.get("part_number")
                    )
                    or llm_types.get(str(row.get("name") or "")[:200])
                    or "other"
                )
            try:
                tool_type = ToolTypeEnum(tool_type_str)
            except ValueError:
                tool_type = ToolTypeEnum.other

            part_number = row.get("part_number")
            name = str(row.get("name", ""))[:500]
            price_value = _safe_float(row.get("price"))

            metadata: dict[str, Any] = {}
            if is_web_sourced:
                metadata["review_status"] = "ingested"
                for key in ("source_url", "fetched_at", "title"):
                    if prov.get(key):
                        metadata[key] = prov[key]

            # Conflict check: an existing entry for this supplier+part_number
            # with materially different price/name is not silently overwritten.
            existing = None
            if part_number:
                existing = (
                    await db.execute(
                        select(ToolCatalogEntry).where(
                            ToolCatalogEntry.supplier_id == supplier_uuid,
                            ToolCatalogEntry.part_number == part_number,
                            ToolCatalogEntry.is_active.is_(True),
                        )
                    )
                ).scalars().first()
            conflict = existing is not None and (
                existing.name != name
                or (
                    price_value is not None
                    and existing.price_value is not None
                    and abs(existing.price_value - price_value) > 0.01 * max(existing.price_value, 1.0)
                )
            )
            if conflict:
                metadata["review_status"] = "needs_review"
                metadata["conflicts_with_entry_id"] = str(existing.id)

            entry = ToolCatalogEntry(
                supplier_id=supplier_uuid,
                source_document_id=source_document_id,
                part_number=part_number,
                tool_type=tool_type,
                name=name,
                description=row.get("description"),
                diameter_mm=_safe_float(row.get("diameter_mm") or row.get("diameter")),
                length_mm=_safe_float(row.get("length_mm") or row.get("length")),
                material=row.get("material"),
                coating=row.get("coating"),
                price_currency=row.get("currency", "RUB"),
                price_value=price_value,
                catalog_page=_safe_int(row.get("catalog_page") or row.get("page")),
                parameters={k: v for k, v in row.items()
                            if k not in ("name", "tool_type", "part_number", "description",
                                        "diameter_mm", "diameter", "length_mm", "length",
                                        "material", "coating", "currency", "price",
                                        "catalog_page", "page")},
                metadata_=metadata or None,
            )
            db.add(entry)
            await db.flush()
            # The entry itself is persisted here — count it as created now,
            # before any enrichment below. Found while adding this function
            # (Ф3): the original _ingest_catalog_async counted a row as
            # "skipped" whenever the embed/Qdrant call after this point
            # raised, even though db.flush() had already put a real row in
            # the session that the caller's db.commit() would still persist
            # — an entry could be silently both "created" (really, in the DB)
            # and "skipped" (in the stats a caller/exploratory report reads).
            # Ф1.D's whole point is honest counts, so this is fixed here:
            # embedding/graph are best-effort enrichment of an already-real
            # entry, not preconditions for it counting as created.
            created += 1

            try:
                if conflict:
                    anomaly = AnomalyCard(
                        anomaly_type=AnomalyType.duplicate,
                        severity=AnomalySeverity.warning,
                        status=AnomalyStatus.open,
                        entity_type="tool_catalog_entry",
                        entity_id=entry.id,
                        title=f"Расхождение в каталоге: {name} ({part_number})",
                        description=(
                            f"Новая запись из web-источника отличается от уже существующей "
                            f"({existing.name!r}, цена {existing.price_value}) для того же "
                            f"поставщика и артикула."
                        ),
                        details={
                            "new_entry_id": str(entry.id),
                            "existing_entry_id": str(existing.id),
                            "source_url": prov.get("source_url"),
                        },
                    )
                    db.add(anomaly)
                    await db.flush()
                    anomaly_ids.append(anomaly.id)
                    conflicted += 1

                # Embedding is collected now and done in ONE batch after the
                # loop: a call per position meant thousands of round trips to
                # the same GPU that parses the catalog's pages.
                pending_embeddings.append(
                    (
                        entry,
                        tool_type.value,
                        f"{tool_type.value} {entry.name} "
                        + (f"Ø{entry.diameter_mm}мм " if entry.diameter_mm else "")
                        + (f"{entry.material} " if entry.material else "")
                        + (f"{entry.coating} " if entry.coating else "")
                        + (entry.description or ""),
                    )
                )

                # Graph node
                try:
                    await ingest_tool_catalog_graph(entry.id, db)
                except Exception:
                    pass
            except Exception as enrich_exc:
                errors.append(f"enrichment_failed:{str(enrich_exc)[:180]}")

        except Exception as row_exc:
            errors.append(str(row_exc)[:200])
            skipped += 1

    if pending_embeddings:
        try:
            from app.ai.embeddings import embed_texts

            vectors = await embed_texts([text for _entry, _type, text in pending_embeddings])
            for (entry, type_value, _text), vector in zip(pending_embeddings, vectors):
                if not vector or not any(vector):
                    continue
                upsert_tool_catalog_entry(
                    entry_id=str(entry.id),
                    vector=vector,
                    tool_type=type_value,
                    name=entry.name,
                    supplier_id=str(supplier_uuid),
                    diameter_mm=entry.diameter_mm,
                    material=entry.material,
                    part_number=entry.part_number,
                    catalog_document_id=(
                        str(source_document_id) if source_document_id else None
                    ),
                    catalog_page=entry.catalog_page,
                    has_image=bool(entry.image_path),
                    price_value=entry.price_value,
                )
                entry.embedding_id = f"tool_catalog:{entry.id}"
        except Exception as exc:  # noqa: BLE001 — entries are real either way
            logger.warning("catalog_batch_embedding_failed", error=str(exc)[:200])
            errors.append(f"embedding: {str(exc)[:150]}")

    return {
        "created": created,
        "conflicted": conflicted,
        "skipped": skipped,
        "skipped_by_reason": skipped_by_reason,
        "errors": errors,
        "anomaly_ids": anomaly_ids,
    }


_CATALOG_SIGNAL_RE = re.compile(
    r"[A-ZА-Я0-9]{2,}[-–/][A-ZА-Я0-9]{2,}"          # article numbers: MT245-040G16
    r"|\bØ\s*\d|\d+[,.]?\d*\s*мм\b"             # dimensions
    r"|\d[\d\s]{2,}\s*(?:руб|₽|RUB)",              # prices
    re.IGNORECASE,
)


def _catalog_density(chunk: str) -> int:
    """How many catalog-ish signals (article numbers, sizes, prices) a chunk has."""
    return len(_CATALOG_SIGNAL_RE.findall(chunk))


async def _parse_catalog_text_via_llm(
    text: str,
    *,
    hint: str | None = None,
    chunk_chars: int = 2500,
    max_chunks: int = 8,
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """Ф3: structure free-form/HTML-derived catalog text into row dicts, for
    content that arrived as already-extracted text (web_discover's fetch_page
    output — HTML pages and OCR'd PDFs alike are normalized to text before
    this ever sees them) rather than a file _parse_catalog_file can dispatch
    on by extension. Same row-dict shape as the file parsers
    (_parse_excel_catalog etc.) so _create_catalog_entries_from_rows doesn't
    need to know which path produced them.

    Long sources are parsed in CHUNKS, not truncated. A real supplier PDF
    catalog opens with a cover, a foreword and a table of contents: the first
    single-shot 20 000-character window contained no articles at all, so a
    200 000-character catalog of real cutting tools yielded exactly zero rows
    (verified live on mirstan.ru's СКИФ-М catalog). ``max_chunks`` bounds the
    cost; anything beyond it is left unparsed rather than silently claimed.

    The chunk is deliberately small (2 500 chars): a dense catalog page yields
    roughly three times its own length in JSON, and at 15 000 chars the reply
    hit the token ceiling mid-string — the whole chunk was then lost to a
    JSONDecodeError plus two futile retries (observed live, ~2 min each).
    Chunks are parsed one at a time: the local Ollama server serves a single
    slot, so "concurrent" chunks only queue behind each other — and the ones
    waiting burned their own request timeout while doing nothing.

    Best-effort: a malformed/empty LLM response yields an empty row list
    (honest "nothing extracted"), never an exception — one bad chunk must not
    break the source, and one bad source must not break a multi-source
    exploratory discovery step.
    """
    import asyncio as _asyncio

    from app.ai.model_resolver import get_verify_model
    from app.ai.ollama_client import generate_json

    # Structured extraction, not reasoning: the model comes from the
    # STRUCTURED_EXTRACTION assignment in settings (same slot invoice-line
    # extraction uses), so changing it is a GUI decision, not a code edit.
    model_config = get_verify_model()
    # The type list is a HINT, not a gate. Measured live on a real supplier's
    # measuring-instrument catalog: with "tool_type must be one of <cutting
    # tools>" the model decided the page "is not a tool catalog" and returned
    # {"rows": []} for every chunk — 948 pages of real products imported as
    # zero. Suppliers sell gauges, machines, fixtures and consumables too; the
    # type is derived downstream from the name anyway (_infer_tool_type).
    system = """Extract a product catalog (tools, instruments, equipment, accessories,
consumables — any goods a supplier sells) from this page text into JSON rows.
Return JSON only: {"rows": [{"part_number","name","tool_type","description","diameter_mm",
"length_mm","material","coating","currency","price","catalog_page"}]}.
name and part_number matter most — extract a row whenever you can see an article code or a
product name, even if there is no price. For tool_type prefer one of: drill, endmill, insert,
holder, tap, reamer, boring_bar, thread_mill, grinder, turning_tool, milling_cutter,
countersink, counterbore, other — and use "other" for anything that does not fit (gauges,
machines, fixtures). NEVER return an empty list just because the products are not cutting
tools. Omit fields you cannot find — never invent a value. Return {"rows": []} only when the
text contains no products at all (a cover page, contacts, a foreword)."""

    # Rank chunks by catalog density instead of taking the first N: a real PDF
    # catalog opens with a cover, a foreword and a table of contents, and a
    # first-N window spends the whole budget there and returns zero rows
    # (measured live: 24 000 leading chars of the СКИФ-М catalog → 0 rows,
    # while the same budget spent on its dense pages → hundreds). Order within
    # the source is preserved for the chunks that are actually parsed.
    all_chunks = [
        text[offset : offset + chunk_chars]
        for offset in range(0, len(text), chunk_chars)
    ]
    scored = [
        (index, chunk, _catalog_density(chunk))
        for index, chunk in enumerate(all_chunks)
    ]
    ranked = sorted(scored, key=lambda item: item[2], reverse=True)[:max_chunks]
    selected = [item for item in sorted(ranked, key=lambda item: item[0]) if item[2] > 0]
    chunks = [chunk for _index, chunk, _score in selected]
    if not chunks:
        # No chunk shows any catalog signal — that is exactly what a graphical
        # PDF catalog looks like after text extraction. Parsing ONE chunk of a
        # 200 000-character catalog and reporting success was the live failure
        # here (chunks=1, rows=0); spend the whole budget instead.
        chunks = [item[1] for item in ranked[:max_chunks]]
    semaphore = _asyncio.Semaphore(max(1, concurrency))

    failed_chunks = 0

    async def _parse_chunk(chunk: str) -> list[dict[str, Any]]:
        nonlocal failed_chunks
        prompt = json.dumps({"text": chunk, "hint": hint}, ensure_ascii=False)
        async with semaphore:
            try:
                raw = await generate_json(
                    prompt,
                    model=model_config.model,
                    provider=model_config.provider,
                    system=system,
                    temperature=0.0,
                    max_tokens=8192,
                    timeout_seconds=300,
                    # A dense catalog page can overflow the token ceiling
                    # mid-row; keep the rows that completed instead of losing
                    # the whole chunk to a JSONDecodeError (live finding).
                    salvage_truncated=True,
                )
            except Exception as exc:  # noqa: BLE001 - one bad chunk can't break the source
                failed_chunks += 1
                logger.warning("catalog_text_llm_parse_failed", error=str(exc)[:200])
                return []
        rows = raw.get("rows") if isinstance(raw, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    parsed = await _asyncio.gather(*(_parse_chunk(chunk) for chunk in chunks))

    # De-duplicate across chunks: catalogs repeat a part in a summary table and
    # again in its detail block, and chunk boundaries can cut a row in two.
    seen: set[tuple[str, str]] = set()
    rows_out: list[dict[str, Any]] = []
    for chunk_rows in parsed:
        for row in chunk_rows:
            key = (
                str(row.get("part_number") or "").strip().lower(),
                str(row.get("name") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            rows_out.append(row)
    logger.info(
        "catalog_text_parsed",
        chars=len(text),
        chunks=len(chunks),
        failed_chunks=failed_chunks,
        truncated=len(text) > len(chunks) * chunk_chars,
        rows=len(rows_out),
    )
    return rows_out


async def ingest_web_catalog_source(
    db: Any,
    supplier_id: str,
    *,
    url: str,
    title: str | None,
    text: str,
    snippet: str | None = None,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    """Ф3: structure one web_discover-fetched source into ToolCatalogEntry
    rows for a supplier — the bridge between Ф2's web_discover output and the
    same entry-creation/embed/graph pipeline _ingest_catalog_async uses for
    uploaded files.

    Takes ``db`` from the caller rather than opening its own session via
    _get_session_factory() (the convention every other function in this file
    uses) — deliberately, because unlike those, this isn't a detached Celery
    task: it's called synchronously from the ingest-web-source HTTP endpoint,
    which already has a properly request-scoped session from Depends(get_db).
    Opening a second, independent session there would silently diverge from
    it (found exactly this way: the endpoint's own session and this
    function's self-opened one look identical in production against one real
    database, but pointed at two different databases under test — the
    supplier a test creates via the request-scoped session was invisible to
    this function's own session, a 404 with no code path at fault except this
    one not accepting the session it should have used).

    The raw fetched text is stored in MinIO alongside the file-upload
    catalogs' bucket convention (tool-catalogs/{supplier_id}/...) for
    provenance/audit, even though nothing later reads it back — the point is
    "what did we actually see", the same reason ComputerUseAction keeps a
    hash of everything it fetches (Ф2).
    """
    import hashlib
    from datetime import UTC, datetime

    from app.db.models import ToolSupplier
    from app.storage import upload_file
    from app.vector.qdrant_store import ensure_drawing_collections

    supplier_uuid = uuid.UUID(supplier_id)
    fetched_at = datetime.now(UTC).isoformat()

    try:
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        upload_file(
            text.encode("utf-8"),
            f"tool-catalogs/{supplier_id}/web/{digest}.txt",
            content_type="text/plain; charset=utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - provenance storage must not block ingestion
        logger.warning("web_catalog_source_storage_failed", url=url, error=str(exc)[:200])

    rows = await _parse_catalog_text_via_llm(
        text,
        hint=title or snippet,
        **({"max_chunks": max_chunks} if max_chunks else {}),
    )
    logger.info("web_catalog_rows_parsed", supplier_id=supplier_id, url=url, rows=len(rows))

    ensure_drawing_collections()

    supplier = await db.get(ToolSupplier, supplier_uuid)
    if not supplier:
        return {"error": f"Supplier {supplier_id} not found"}

    result = await _create_catalog_entries_from_rows(
        db,
        supplier_uuid,
        rows,
        provenance={
            "discovery_method": "web_discover",
            "source_url": url,
            "fetched_at": fetched_at,
            "title": title,
        },
    )
    await db.commit()

    logger.info(
        "web_catalog_ingested",
        supplier_id=supplier_id,
        url=url,
        created=result["created"],
        conflicted=result["conflicted"],
    )
    return {
        "supplier_id": supplier_id,
        "source_url": url,
        "entries_created": result["created"],
        "entries_conflicted": result["conflicted"],
        "entries_skipped": result["skipped"],
        "anomaly_ids": [str(a) for a in result["anomaly_ids"]],
        "errors": result["errors"][:10],
    }


# ── DXF Parsing ───────────────────────────────────────────────────────────────


async def _convert_dwg_to_dxf(file_bytes: bytes) -> bytes | None:
    """
    Convert DWG binary file to DXF using dwg2dxf (libredwg).

    Strategy:
    - Write DWG to a temp file
    - Run dwg2dxf (libredwg) subprocess with 60s timeout
    - Read and return resulting DXF bytes
    - Falls back to ezdxf odafc addon if dwg2dxf is not found

    libredwg covers ~90% of entity types for DWG R13–R2018.
    """
    import asyncio
    import os
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="dwg_conv_") as tmpdir:
        dwg_path = os.path.join(tmpdir, "input.dwg")
        dxf_path = os.path.join(tmpdir, "input.dxf")
        with open(dwg_path, "wb") as f:
            f.write(file_bytes)

        # Primary: dwg2dxf from libredwg
        dwg2dxf_bin = shutil.which("dwg2dxf")
        if dwg2dxf_bin:
            try:
                proc = await asyncio.create_subprocess_exec(
                    dwg2dxf_bin,
                    "--as", "R2018",
                    "-o", dxf_path,
                    dwg_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0 and os.path.exists(dxf_path):
                    with open(dxf_path, "rb") as f:
                        result = f.read()
                    logger.info("dwg2dxf_ok", size_in=len(file_bytes), size_out=len(result))
                    return result
                else:
                    logger.warning(
                        "dwg2dxf_nonzero_exit",
                        returncode=proc.returncode,
                        stderr=(stderr or b"").decode(errors="replace")[:300],
                    )
            except TimeoutError:
                logger.warning("dwg2dxf_timeout")
            except Exception as exc:
                logger.warning("dwg2dxf_exception", error=str(exc))

        # Fallback: ezdxf odafc addon (ODA File Converter, if installed separately)
        try:
            from ezdxf.addons import odafc
            if odafc.is_installed():
                doc = odafc.readfile(dwg_path)
                dxf_io = io.StringIO()
                doc.write(dxf_io)
                return dxf_io.getvalue().encode("utf-8")
        except Exception as exc:
            logger.warning("odafc_fallback_failed", error=str(exc))

    logger.error("dwg_conversion_failed", size=len(file_bytes))
    return None


def _extract_dxf_entities(msp: Any, doc: Any) -> tuple[list[dict], list[dict]]:
    """
    Extract ALL drawing entities from DXF modelspace.

    Covers full entity set required to not miss manufacturing drawing elements:
    - Geometry: CIRCLE, ARC, LINE, LWPOLYLINE, POLYLINE, SPLINE, ELLIPSE
    - Annotations: TEXT, MTEXT, ATTRIB, ATTDEF
    - Dimensions: DIMENSION (all subtypes), LEADER, MULTILEADER, QLEADER
    - GD&T: TOLERANCE (feature control frames)
    - Fills: HATCH, SOLID, TRACE
    - References: INSERT (with one-level block expansion)

    Returns (entities_list, texts_list).
    """

    entities: list[dict] = []
    texts: list[dict] = []

    def _layer(e: Any) -> str:
        try:
            return str(e.dxf.layer)
        except Exception:
            return "0"

    def _process_entity(entity: Any, depth: int = 0) -> None:  # noqa: C901
        etype = entity.dxftype()
        try:
            if etype == "CIRCLE":
                c = entity.dxf.center
                entities.append({
                    "type": "CIRCLE",
                    "center_x": float(c.x),
                    "center_y": float(c.y),
                    "radius": float(entity.dxf.radius),
                    "layer": _layer(entity),
                })
            elif etype == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                entities.append({
                    "type": "LINE",
                    "x1": float(s.x), "y1": float(s.y),
                    "x2": float(e.x), "y2": float(e.y),
                    "layer": _layer(entity),
                })
            elif etype == "ARC":
                c = entity.dxf.center
                entities.append({
                    "type": "ARC",
                    "center_x": float(c.x),
                    "center_y": float(c.y),
                    "radius": float(entity.dxf.radius),
                    "start_angle": float(entity.dxf.start_angle),
                    "end_angle": float(entity.dxf.end_angle),
                    "layer": _layer(entity),
                })
            elif etype == "ELLIPSE":
                c = entity.dxf.center
                entities.append({
                    "type": "ELLIPSE",
                    "center_x": float(c.x),
                    "center_y": float(c.y),
                    "major_axis_x": float(entity.dxf.major_axis.x),
                    "major_axis_y": float(entity.dxf.major_axis.y),
                    "ratio": float(entity.dxf.ratio),
                    "start_param": float(entity.dxf.start_param),
                    "end_param": float(entity.dxf.end_param),
                    "layer": _layer(entity),
                })
            elif etype == "LWPOLYLINE":
                points = [(float(p[0]), float(p[1])) for p in entity.get_points()]
                entities.append({
                    "type": "LWPOLYLINE",
                    "points": points[:100],
                    "closed": bool(entity.closed),
                    "layer": _layer(entity),
                })
            elif etype == "POLYLINE":
                try:
                    points = [(float(v.dxf.location.x), float(v.dxf.location.y))
                              for v in entity.vertices]
                    entities.append({
                        "type": "POLYLINE",
                        "points": points[:100],
                        "closed": bool(entity.is_closed),
                        "layer": _layer(entity),
                    })
                except Exception:
                    pass
            elif etype == "SPLINE":
                try:
                    # Sample spline as polyline for representation
                    pts = [(float(p[0]), float(p[1])) for p in entity.flattening(0.1)]
                    entities.append({
                        "type": "SPLINE",
                        "points": pts[:100],
                        "closed": bool(entity.closed),
                        "degree": int(entity.dxf.degree),
                        "layer": _layer(entity),
                    })
                except Exception:
                    control_pts = [(float(p[0]), float(p[1])) for p in entity.control_points]
                    entities.append({
                        "type": "SPLINE",
                        "control_points": control_pts[:50],
                        "degree": int(entity.dxf.degree),
                        "layer": _layer(entity),
                    })
            elif etype in ("TEXT", "ATTRIB", "ATTDEF"):
                text_val = ""
                try:
                    text_val = entity.dxf.text or ""
                except Exception:
                    pass
                if text_val:
                    entry = {"type": etype, "text": text_val, "layer": _layer(entity)}
                    try:
                        pos = entity.dxf.insert
                        entry["x"] = float(pos.x)
                        entry["y"] = float(pos.y)
                    except Exception:
                        pass
                    try:
                        entry["height"] = float(entity.dxf.height)
                    except Exception:
                        pass
                    texts.append(entry)
                    entities.append({"type": etype, "text": text_val, "layer": _layer(entity)})
            elif etype == "MTEXT":
                try:
                    text_val = entity.plain_mtext()
                except Exception:
                    try:
                        text_val = entity.dxf.text or ""
                    except Exception:
                        text_val = ""
                if text_val:
                    entry = {"type": "MTEXT", "text": text_val, "layer": _layer(entity)}
                    try:
                        pos = entity.dxf.insert
                        entry["x"] = float(pos.x)
                        entry["y"] = float(pos.y)
                    except Exception:
                        pass
                    texts.append(entry)
                    entities.append({"type": "MTEXT", "text": text_val, "layer": _layer(entity)})
            elif etype == "DIMENSION":
                dim_info: dict = {
                    "type": "DIMENSION",
                    "layer": _layer(entity),
                }
                try:
                    dim_info["measurement"] = float(entity.dxf.actual_measurement)
                except Exception:
                    pass
                try:
                    dim_info["dim_type_code"] = int(entity.dimtype)
                    # Decode dimtype: 0=linear, 1=aligned, 2=angular, 3=diameter,
                    #                 4=radius, 5=angular3p, 6=ordinate
                    dim_type_names = {
                        0: "linear", 1: "aligned", 2: "angular",
                        3: "diameter", 4: "radius", 5: "angular3p", 6: "ordinate",
                    }
                    dim_info["dim_type_name"] = dim_type_names.get(
                        entity.dimtype & 0x0F, "linear"
                    )
                except Exception:
                    pass
                try:
                    # Dimension text override (e.g. "Ø12H7" or "50±0.1")
                    dim_info["text_override"] = entity.dxf.text or ""
                except Exception:
                    pass
                try:
                    dim_info["dim_style"] = str(entity.dxf.dimstyle)
                except Exception:
                    pass
                entities.append(dim_info)
                # Also harvest the text for AI analysis
                text_val = dim_info.get("text_override") or str(
                    dim_info.get("measurement", "")
                )
                if text_val:
                    texts.append({
                        "type": "DIMENSION",
                        "text": text_val,
                        "layer": _layer(entity),
                    })
            elif etype in ("LEADER", "QLEADER"):
                try:
                    text_val = entity.dxf.text if hasattr(entity.dxf, "text") else ""
                    if text_val:
                        texts.append({"type": etype, "text": text_val, "layer": _layer(entity)})
                    verts = [
                        (float(v.x), float(v.y))
                        for v in entity.vertices
                    ]
                    entities.append({
                        "type": etype,
                        "vertices": verts[:20],
                        "text": text_val,
                        "layer": _layer(entity),
                    })
                except Exception:
                    pass
            elif etype == "MULTILEADER":
                try:
                    text_val = ""
                    try:
                        text_val = entity.context.mtext.insert or ""
                    except Exception:
                        pass
                    if text_val:
                        texts.append({"type": "MULTILEADER", "text": text_val, "layer": _layer(entity)})
                    entities.append({
                        "type": "MULTILEADER",
                        "text": text_val,
                        "layer": _layer(entity),
                    })
                except Exception:
                    pass
            elif etype == "TOLERANCE":
                # GD&T feature control frames (⊥, ∥, ⌀, etc.)
                try:
                    text_val = entity.dxf.string or ""
                    if text_val:
                        texts.append({
                            "type": "TOLERANCE",
                            "text": text_val,
                            "layer": _layer(entity),
                        })
                    entities.append({
                        "type": "TOLERANCE",
                        "text": text_val,
                        "layer": _layer(entity),
                    })
                except Exception:
                    pass
            elif etype == "HATCH":
                # Cross-hatching indicates section cuts, material patterns
                try:
                    entities.append({
                        "type": "HATCH",
                        "pattern_name": str(entity.dxf.pattern_name),
                        "solid_fill": bool(entity.dxf.solid_fill),
                        "layer": _layer(entity),
                        "path_count": len(list(entity.paths)),
                    })
                except Exception:
                    pass
            elif etype in ("SOLID", "TRACE"):
                try:
                    pts = [
                        (float(entity.dxf.vtx0.x), float(entity.dxf.vtx0.y)),
                        (float(entity.dxf.vtx1.x), float(entity.dxf.vtx1.y)),
                        (float(entity.dxf.vtx2.x), float(entity.dxf.vtx2.y)),
                        (float(entity.dxf.vtx3.x), float(entity.dxf.vtx3.y)),
                    ]
                    entities.append({
                        "type": etype,
                        "points": pts,
                        "layer": _layer(entity),
                    })
                except Exception:
                    pass
            elif etype == "INSERT" and depth == 0:
                # Block reference — expand one level to get nested geometry
                block_name = ""
                try:
                    block_name = str(entity.dxf.name)
                except Exception:
                    pass
                entities.append({
                    "type": "INSERT",
                    "block": block_name,
                    "layer": _layer(entity),
                })
                # Expand block content (one level deep to avoid infinite recursion)
                try:
                    block = doc.blocks.get(block_name)
                    if block:
                        for sub_entity in block:
                            if sub_entity.dxftype() not in ("BLOCK", "ENDBLK"):
                                _process_entity(sub_entity, depth=1)
                except Exception:
                    pass
        except Exception:
            pass

    for entity in msp:
        _process_entity(entity, depth=0)

    return entities, texts


async def _parse_dxf(
    file_bytes: bytes, filename: str
) -> tuple[str | None, list[dict], str]:
    """
    Parse DXF file bytes using ezdxf.

    Handles both ASCII DXF and binary DXF formats.
    Extracts all entity types relevant to manufacturing drawings.
    Generates SVG for viewer and VLM rasterization.

    NOTE: For DWG files, convert to DXF first using _convert_dwg_to_dxf().
    """
    import os
    import tempfile

    try:
        import ezdxf
        import ezdxf.recover as recover

        doc = None
        # Try ASCII DXF first
        try:
            doc = ezdxf.read(io.StringIO(file_bytes.decode("utf-8", errors="replace")))
        except Exception:
            pass

        # Try via temp file (handles binary DXF and broken ASCII DXF)
        if doc is None:
            with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tf:
                tf.write(file_bytes)
                tmp_path = tf.name
            try:
                try:
                    doc = ezdxf.readfile(tmp_path)
                except Exception:
                    doc, _ = recover.readfile(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if doc is None:
            logger.error("dxf_parse_all_methods_failed", filename=filename)
            return None, [], ""

        msp = doc.modelspace()
        entities, texts = _extract_dxf_entities(msp, doc)

        # Generate SVG for viewer
        svg_content: str | None = None
        try:
            from ezdxf.addons.drawing import Frontend, RenderContext
            from ezdxf.addons.drawing.layout import Page, Units
            from ezdxf.addons.drawing.svg import SVGBackend

            ctx = RenderContext(doc)
            backend = SVGBackend()
            frontend = Frontend(ctx, backend)
            frontend.draw_layout(msp)
            page = Page(420, 297, units=Units.mm)  # A3 landscape
            svg_content = backend.get_string(page=page)
        except Exception as svg_exc:
            logger.warning("dxf_svg_export_failed", error=str(svg_exc))

        text_parts = [t["text"] for t in texts if t.get("text")]
        drawing_text = "\n".join(text_parts)

        logger.info(
            "dxf_parsed",
            filename=filename,
            entities=len(entities),
            texts=len(text_parts),
            has_svg=svg_content is not None,
        )
        return svg_content, entities, drawing_text

    except ImportError:
        logger.warning("ezdxf_not_installed")
        return None, [], ""
    except Exception as exc:
        logger.error("dxf_parse_failed", filename=filename, error=str(exc))
        return None, [], ""


async def _parse_pdf_drawing(file_bytes: bytes) -> tuple[str | None, str]:
    """Rasterize PDF drawing page and extract text for OCR analysis."""
    text_content = ""
    svg_content = None

    try:
        import fitz  # PyMuPDF — already in pyproject.toml as pymupdf

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if doc.page_count > 0:
            page = doc[0]
            text_content = page.get_text("text")

            # Export as SVG for viewer
            try:
                svg_bytes = page.get_svg_image()
                svg_content = svg_bytes if isinstance(svg_bytes, str) else svg_bytes.decode("utf-8")
            except Exception:
                pass

        doc.close()
    except Exception as exc:
        logger.error("pdf_drawing_parse_failed", error=str(exc))

    return svg_content, text_content


# ── Raster / VLM Helpers ─────────────────────────────────────────────────────


async def _svg_to_png_bytes(svg_content: str, width: int = 2048) -> bytes | None:
    """Render SVG to PNG bytes for VLM analysis."""
    try:
        import cairosvg
        png_bytes = cairosvg.svg2png(bytestring=svg_content.encode("utf-8"), output_width=width)
        return png_bytes
    except Exception:
        pass
    try:
        # Fallback: use Pillow + svglib if cairosvg unavailable
        import io
        import os
        import tempfile

        from reportlab.graphics import renderPM  # type: ignore
        from svglib.svglib import svg2rlg  # type: ignore
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
            f.write(svg_content.encode("utf-8"))
            tmp = f.name
        try:
            drawing = svg2rlg(tmp)
            if drawing:
                buf = io.BytesIO()
                renderPM.drawToFile(drawing, buf, fmt="PNG")
                return buf.getvalue()
        finally:
            os.unlink(tmp)
    except Exception as exc:
        logger.warning("svg_to_png_failed", error=str(exc))
    return None


async def _pdf_to_png_bytes(file_bytes: bytes, page_index: int = 0, dpi: int = 200) -> bytes | None:
    """Render PDF page to PNG bytes for VLM analysis using PyMuPDF."""
    try:

        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if doc.page_count > page_index:
            page = doc[page_index]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            doc.close()
            return png_bytes
        doc.close()
    except Exception as exc:
        logger.warning("pdf_to_png_failed", error=str(exc))
    return None


async def _normalize_raster_to_png(file_bytes: bytes, fmt: str) -> bytes | None:
    """Convert any raster image format to PNG bytes for VLM."""
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(file_bytes))
        # Convert to RGB if needed (VLM doesn't handle CMYK, palette modes well)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        # Resize if too large (>4096px side) to save tokens/memory
        max_side = 4096
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.warning("raster_to_png_failed", fmt=fmt, error=str(exc))
        # Return original bytes as fallback — some VLMs accept JPEG directly
        return file_bytes


def _extract_text_from_svg(svg_content: str) -> str:
    """Extract visible text elements from SVG for additional context."""
    import re
    texts = re.findall(r"<text[^>]*>(.*?)</text>", svg_content, re.DOTALL)
    return " ".join(t.strip() for t in texts if t.strip())[:4000]


def _parse_step_to_info_svg(file_bytes: bytes, filename: str) -> tuple[str | None, str]:
    """Parse a STEP/IGES file and return (info_svg, extracted_text).

    Generates an SVG info card with product names and entity statistics.
    Falls back to (None, plain_text) if the file is not parseable.
    """
    import html
    from collections import Counter

    text = file_bytes.decode("utf-8", errors="replace")

    # Extract PRODUCT names (STEP ISO 10303-21)
    products = list(dict.fromkeys(re.findall(r"PRODUCT\s*\(\s*'([^']{1,80})'", text)))[:8]
    # Extract PART_NAME from IGES header
    if not products:
        products = list(dict.fromkeys(re.findall(r"PART_NAME\s*=\s*'([^']{1,80})'", text)))[:4]
    if not products:
        products = [filename]

    # Count entity types (STEP: lines starting with #N = ENTITY_TYPE)
    entity_matches = re.findall(r"^#\d+\s*=\s*([A-Z_]{3,})\s*\(", text[:500_000], re.MULTILINE)
    entity_counts = Counter(entity_matches).most_common(6)

    # Build extracted_text for AI
    extracted_text = (
        f"3D файл: {filename}\n"
        f"Изделия: {', '.join(products[:4])}\n"
        + (
            "Типы сущностей: "
            + ", ".join(f"{k}({v})" for k, v in entity_counts)
            if entity_counts
            else ""
        )
    )

    # ── SVG info card ──────────────────────────────────────────────────────
    W, H = 800, 500
    rows_svg = ""
    y = 310
    for etype, count in entity_counts:
        bar_w = min(int(count / max(1, entity_counts[0][1]) * 340), 340)
        rows_svg += (
            f'<text x="60" y="{y}" fill="#94a3b8" font-size="13">'
            f'{html.escape(etype)}</text>'
            f'<rect x="270" y="{y - 12}" width="{bar_w}" height="12" fill="#3b82f6" opacity="0.7" rx="2"/>'
            f'<text x="{270 + bar_w + 6}" y="{y}" fill="#cbd5e1" font-size="12">{count}</text>'
        )
        y += 28

    product_lines = ""
    for i, p in enumerate(products[:4]):
        product_lines += (
            f'<text x="60" y="{200 + i * 26}" fill="#e2e8f0" font-size="15" '
            f'font-weight="{"600" if i == 0 else "400"}">'
            f'{html.escape(p[:55])}</text>'
        )

    fmt_label = "STEP ISO 10303" if filename.lower().endswith((".step", ".stp")) else "IGES"
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
  <rect width="{W}" height="{H}" fill="#0f172a" rx="8"/>
  <!-- 3D cube icon -->
  <g transform="translate(40,40)">
    <polygon points="40,10 70,28 70,64 40,82 10,64 10,28" fill="none" stroke="#3b82f6" stroke-width="2"/>
    <polygon points="40,10 70,28 40,46 10,28" fill="#1e3a5f" stroke="#3b82f6" stroke-width="1"/>
    <polygon points="10,28 40,46 40,82 10,64" fill="#162d47" stroke="#3b82f6" stroke-width="1"/>
    <polygon points="70,28 40,46 40,82 70,64" fill="#1a3357" stroke="#3b82f6" stroke-width="1"/>
  </g>
  <text x="140" y="65" fill="#3b82f6" font-size="13" font-family="monospace">{html.escape(fmt_label)}</text>
  <text x="140" y="88" fill="#64748b" font-size="12" font-family="monospace">{html.escape(filename[:50])}</text>
  <line x1="40" y1="148" x2="{W - 40}" y2="148" stroke="#1e293b" stroke-width="1"/>
  <text x="60" y="175" fill="#94a3b8" font-size="12" font-weight="600" letter-spacing="1">ИЗДЕЛИЯ</text>
  {product_lines}
  <line x1="40" y1="290" x2="{W - 40}" y2="290" stroke="#1e293b" stroke-width="1"/>
  <text x="60" y="305" fill="#94a3b8" font-size="12" font-weight="600" letter-spacing="1">ТИПЫ СУЩНОСТЕЙ</text>
  {rows_svg}
  <text x="{W//2}" y="{H - 20}" fill="#334155" font-size="11" text-anchor="middle">
    Превью сформировано автоматически · для полного просмотра используйте CAD-приложение
  </text>
</svg>"""
    return svg, extracted_text


# ── Catalog File Parsing ──────────────────────────────────────────────────────


async def _parse_catalog_file(
    file_bytes: bytes, file_ext: str, filename: str
) -> list[dict[str, Any]]:
    """Parse catalog file into list of row dicts."""
    rows: list[dict] = []

    if file_ext in (".xlsx", ".xlsm"):
        rows = _parse_excel_catalog(file_bytes)
    elif file_ext == ".xls":
        rows = _parse_xls_catalog(file_bytes)
    elif file_ext == ".csv":
        rows = _parse_csv_catalog(file_bytes)
    elif file_ext == ".json":
        rows = _parse_json_catalog(file_bytes)
    elif file_ext == ".pdf":
        rows = await _parse_pdf_catalog(file_bytes)

    # A table extractor that returns JUNK is worse than one that returns
    # nothing: measured live on a real graphical PDF catalog, pdfplumber
    # produced 20 "rows" whose only key was a run of underscores, the
    # `if not rows` gate below saw a non-empty list, the text/LLM fallback never
    # ran, and the import finished with rows_parsed=20, created=0.
    usable = _usable_row_count(rows)
    if rows and usable == 0:
        logger.info(
            "catalog_tables_discarded_as_unusable",
            ext=file_ext,
            filename=filename,
            rows=len(rows),
        )
        rows = []
        if file_ext == ".pdf":
            # Keep the PDF-aware path (OCR when the text layer is unreadable,
            # chunk budget scaled to the file) instead of the generic one, and
            # do NOT fall through afterwards: the generic path re-extracts the
            # same unreadable text layer and spends another 120 LLM calls on it
            # (measured: four minutes of GPU on "(cid:NN)" noise).
            return await _parse_pdf_text_path(file_bytes)

    if not rows:
        # Anything else — .txt, .docx, .html, .xml, an unknown extension — goes
        # through the shared document parser and then the LLM row extractor.
        # The old behaviour was a warning and zero rows, i.e. a task that
        # "succeeded" while importing nothing.
        rows = await _parse_catalog_any_format(file_bytes, file_ext, filename)

    return rows


def _usable_row_count(rows: list[dict[str, Any]]) -> int:
    """Rows a human would recognise as catalog positions.

    A name, or an article plus a price — anything less is layout noise from a
    table extractor, not a product.
    """
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if len(name) >= 3:
            count += 1
            continue
        if row.get("part_number") and row.get("price"):
            count += 1
    return count


async def _parse_catalog_any_format(
    file_bytes: bytes, file_ext: str, filename: str
) -> list[dict[str, Any]]:
    """Last-resort path: extract text with the shared parser, then ask the LLM.

    Reuses app.ai.parsers.registry (the same one document ingest uses), so any
    format it understands becomes a catalog source without new code here.
    """
    try:
        from app.ai.parsers.registry import parse_document

        parsed = parse_document(file_bytes, filename)
        text = (getattr(parsed, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — unknown formats must not crash ingest
        logger.warning("catalog_generic_parse_failed", ext=file_ext, error=str(exc)[:200])
        return []

    if len(text) < 50:
        logger.warning(
            "catalog_format_unreadable", ext=file_ext, filename=filename, chars=len(text)
        )
        return []
    return await _parse_catalog_text_via_llm(
        text, hint=filename, max_chunks=_chunk_budget_for(text)
    )


# Header cells a supplier price list actually uses. A row is treated as the
# header when enough of its cells map to known fields — real files start with a
# company banner, a date and empty rows, so "row 1 is the header" was wrong for
# most of them.
_KNOWN_HEADER_FIELDS = {
    "name", "part_number", "price", "description", "diameter_mm",
    "length_mm", "material", "coating", "currency", "tool_type", "unit", "quantity",
}


def _find_header_row(raw_rows: list[tuple], scan_limit: int = 20) -> tuple[int, list[str]]:
    """Index and normalised names of the header row.

    Returns (-1, []) when nothing in the first ``scan_limit`` rows looks like a
    header, so the caller can fall back instead of silently treating a banner
    line as column names.
    """
    best_idx, best_headers, best_score = -1, [], 0
    for idx, row in enumerate(raw_rows[:scan_limit]):
        headers = [_normalize_header(str(cell or "")) for cell in row]
        score = sum(1 for h in headers if h in _KNOWN_HEADER_FIELDS)
        if score > best_score:
            best_idx, best_headers, best_score = idx, headers, score
    # One recognised column is a coincidence; two is a header.
    if best_score < 2:
        return -1, []
    return best_idx, best_headers


def _rows_from_sheet(raw_rows: list[tuple], sheet_name: str | None = None) -> list[dict]:
    """Turn one sheet's raw cells into catalog rows."""
    header_idx, headers = _find_header_row(raw_rows)
    if header_idx < 0:
        return []
    rows: list[dict] = []
    # A lone non-empty cell is usually a category separator ("Фрезы концевые");
    # it is the only type hint many price lists carry, so it is remembered and
    # attached to the rows that follow instead of being dropped.
    current_category: str | None = None
    for raw in raw_rows[header_idx + 1:]:
        filled = [cell for cell in raw if cell not in (None, "")]
        if not filled:
            continue
        if len(filled) == 1 and isinstance(filled[0], str):
            current_category = str(filled[0]).strip()
            continue
        row_dict: dict[str, Any] = {}
        for j, cell in enumerate(raw):
            if j < len(headers) and headers[j]:
                row_dict[headers[j]] = cell
        if not row_dict:
            continue
        if current_category:
            row_dict.setdefault("_category", current_category)
        if sheet_name:
            row_dict.setdefault("_sheet", sheet_name)
        rows.append(row_dict)
    return rows


def _parse_excel_catalog(file_bytes: bytes) -> list[dict]:
    """Parse an .xlsx catalog: every sheet, header found rather than assumed.

    Previously only ``wb.active`` was read and the header had to sit in row 1 —
    a two-sheet price list with a banner on top produced zero rows.
    ``data_only=True`` matters too: without it a price computed by a formula
    arrives as the literal "=B2*1.2".
    """
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        rows: list[dict] = []
        for ws in wb.worksheets:
            raw_rows = list(ws.iter_rows(values_only=True))
            rows.extend(_rows_from_sheet(raw_rows, ws.title))
        wb.close()
        return rows
    except Exception as exc:
        logger.error("excel_catalog_parse_failed", error=str(exc))
        return []


def _parse_xls_catalog(file_bytes: bytes) -> list[dict]:
    """Parse a legacy binary .xls catalog.

    openpyxl cannot read this format at all — the file used to land in the
    .xlsx branch and yield zero rows without any error.
    """
    try:
        import xlrd
    except ImportError:
        logger.warning("xls_support_missing", hint="pip install xlrd>=2.0")
        return []
    try:
        book = xlrd.open_workbook(file_contents=file_bytes)
        rows: list[dict] = []
        for sheet in book.sheets():
            raw_rows = [tuple(sheet.row_values(i)) for i in range(sheet.nrows)]
            rows.extend(_rows_from_sheet(raw_rows, sheet.name))
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.error("xls_catalog_parse_failed", error=str(exc))
        return []


def _sniff_csv_delimiter(sample: str) -> str:
    """Pick the delimiter by counting candidates on the header line.

    Russian Excel writes ";" by default and uses "," as the DECIMAL separator,
    so a comma-assuming reader splits "3450,50" into two fields — measured
    live: every row of a real semicolon price list either crashed the parser
    (row longer than the single-column header → restkey None → .lower() on
    None) or silently fell through to the LLM, which returned 2 of 3 rows.
    """
    import csv

    head = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {sep: head.count(sep) for sep in (";", "\t", "|", ",")}
    best = max(counts, key=lambda sep: counts[sep])
    if counts[best] > 0:
        return best
    try:
        return csv.Sniffer().sniff(sample[:4096], delimiters=";,\t|").delimiter
    except Exception:  # noqa: BLE001 — a single-column file is still valid
        return ","


def _parse_csv_catalog(file_bytes: bytes) -> list[dict]:
    """Parse a CSV/TSV catalog, delimiter sniffed and banner rows skipped."""
    import csv

    try:
        text = file_bytes.decode("utf-8-sig", errors="replace")
        delimiter = _sniff_csv_delimiter(text)
        raw_rows = [
            [cell for cell in row]
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        ]
        if not raw_rows:
            return []
        # Real price lists start with a company banner and a date; reuse the
        # same header detection the Excel path uses instead of assuming row 1.
        header_index, headers = _find_header_row(raw_rows)
        if header_index < 0:
            header_index, headers = 0, [_normalize_header(str(c)) for c in raw_rows[0]]

        rows: list[dict] = []
        for raw in raw_rows[header_index + 1 :]:
            if not any(str(cell).strip() for cell in raw):
                continue
            row: dict[str, Any] = {}
            for index, cell in enumerate(raw):
                key = headers[index] if index < len(headers) else f"column_{index}"
                if not key:
                    continue
                value = str(cell).strip()
                if value:
                    row[key] = value
            if row:
                rows.append(row)
        return rows
    except Exception as exc:
        logger.error("csv_catalog_parse_failed", error=str(exc))
        return []


def _parse_json_catalog(file_bytes: bytes) -> list[dict]:
    """Parse JSON catalog file."""
    try:
        data = json.loads(file_bytes.decode("utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        return []
    except Exception as exc:
        logger.error("json_catalog_parse_failed", error=str(exc))
        return []


async def _parse_pdf_catalog(file_bytes: bytes, max_pages: int = 500) -> list[dict]:
    """Extract catalog rows from a PDF.

    Two passes, because supplier catalogs come in both shapes: ruled tables
    (pdfplumber reads them directly) and free layout with columns drawn by
    whitespace (tables yield nothing — then the accumulated text goes through
    the LLM row extractor, the same one the web path uses).

    The old code stopped at page 50 and had no text fallback at all: a
    200-page catalog without table borders imported zero rows and reported
    success.
    """
    rows: list[dict] = []
    text_parts: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages[:max_pages]:
                tables = page.extract_tables()
                page_rows = 0
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header_idx, headers = _find_header_row([tuple(r) for r in table[:3]])
                    if header_idx < 0:
                        headers = [_normalize_header(str(h or "")) for h in table[0]]
                        header_idx = 0
                    for row in table[header_idx + 1:]:
                        if not any(row):
                            continue
                        row_dict: dict = {}
                        for j, cell in enumerate(row):
                            if j < len(headers) and headers[j]:
                                row_dict[headers[j]] = cell
                        if row_dict:
                            rows.append(row_dict)
                            page_rows += 1
                if page_rows == 0:
                    # No usable table on this page — keep its text for the
                    # LLM pass instead of discarding the page.
                    try:
                        text_parts.append(page.extract_text() or "")
                    except Exception:  # noqa: BLE001
                        pass
    except ImportError:
        logger.warning("pdfplumber_not_installed")
    except Exception as exc:
        logger.error("pdf_catalog_parse_failed", error=str(exc))

    if rows:
        logger.info("pdf_catalog_tables_parsed", rows=len(rows))
        return rows

    return await _parse_pdf_text_path(file_bytes, text_parts, max_pages)


async def _parse_pdf_text_path(
    file_bytes: bytes, text_parts: list[str] | None = None, max_pages: int = 500
) -> list[dict]:
    """Text (or OCR) → LLM rows for a PDF whose tables gave nothing usable.

    Reached both when pdfplumber finds no tables AND when it finds junk ones
    (a graphical catalog draws its layout with rules, so extract_tables returns
    thousands of underscore-keyed rows) — the second case used to fall through
    to the generic any-format path with the chat-sized 8-chunk budget, which
    read 1.4% of a 1.5 M character catalog and reported success.
    """
    if text_parts is None:
        text_parts = []
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages[:max_pages]:
                    try:
                        text_parts.append(page.extract_text() or "")
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf_text_extract_failed", error=str(exc)[:150])

    text = "\n".join(part for part in text_parts if part).strip()
    if _pdf_text_is_unreadable(text):
        # A PDF whose fonts carry no ToUnicode map extracts as "(cid:12)(cid:7)…".
        # Measured live on a real 948-page supplier catalog: 1.47 M characters of
        # it, catalog density 0, and the LLM was fed pure noise while the import
        # reported success. Render and OCR instead.
        logger.info("pdf_catalog_text_unreadable_switching_to_ocr", chars=len(text))
        text = await _ocr_pdf_text(file_bytes, max_pages=min(max_pages, 60))

    if len(text) < 50:
        logger.warning("pdf_catalog_no_text_layer", chars=len(text))
        return []
    logger.info("pdf_catalog_text_fallback", chars=len(text))
    return await _parse_catalog_text_via_llm(
        text, hint="PDF-каталог", max_chunks=_chunk_budget_for(text)
    )


def _chunk_budget_for(text: str, chunk_chars: int = 2500) -> int:
    """How many chunks a FILE-sized catalog deserves.

    The chat-turn default (8) is right for a web page and absurd for a 1.5 M
    character catalog — it read 1.4% of it and reported success. A file is
    parsed by a worker with an hour-long budget, so scale with the content and
    cap where the wall-clock stops being reasonable.
    """
    needed = max(1, len(text) // chunk_chars)
    return min(needed, 120)


def _pdf_text_is_unreadable(text: str) -> bool:
    """Does this "text layer" actually contain words?

    Two real shapes of failure: CID placeholders (no ToUnicode map) and a page
    of punctuation from a scanned page's stray artefacts.
    """
    sample = text[:20000]
    if not sample.strip():
        return False  # genuinely empty — the caller handles that separately
    if sample.count("(cid:") > 20:
        return True
    letters = sum(1 for ch in sample if ch.isalpha())
    return letters / max(len(sample), 1) < 0.25


async def _ocr_pdf_text(file_bytes: bytes, max_pages: int = 60) -> str:
    """Render pages and OCR them (rus+eng), bounded by page budget."""
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover — depends on the image
        logger.warning("pdf_ocr_unavailable", error=str(exc))
        return ""

    def _run() -> str:
        parts: list[str] = []
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            total = doc.page_count
            # Sample ACROSS the document, never the first N pages: a real
            # 948-page catalog opens with covers and ~30 pages of contents, so
            # the first 60 pages OCR'd cleanly and contained not one article
            # (measured live — 92 000 characters, zero rows).
            if total <= max_pages:
                indices = list(range(total))
            else:
                stride = total / max_pages
                indices = sorted({int(i * stride) for i in range(max_pages)})
            for index in indices:
                page = doc.load_page(index)
                pixmap = page.get_pixmap(dpi=200)
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                try:
                    parts.append(pytesseract.image_to_string(image, lang="rus+eng"))
                except Exception as exc:  # noqa: BLE001 — one bad page is not fatal
                    logger.warning("pdf_ocr_page_failed", page=index, error=str(exc)[:120])
            if total > max_pages:
                logger.info(
                    "pdf_ocr_sampled",
                    pages_ocr=len(indices),
                    pages_total=total,
                    stride=round(total / max_pages, 1),
                )
        return "\n".join(parts)

    return await asyncio.to_thread(_run)


# ── MinIO Helpers ─────────────────────────────────────────────────────────────


async def _load_drawing_file(drawing: Any) -> bytes:
    """Load drawing file bytes from MinIO.

    Resolution order:
    1. metadata_.storage_path  — set by both upload and document-auto-create flows
    2. drawing.document.storage_path — if document FK is loaded
    3. drawings/{id}/{filename} — canonical drawing bucket path
    """
    try:
        from minio import Minio

        from app.config import settings

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

        # Try all path sources in priority order
        candidates: list[str] = []
        meta_path = (drawing.metadata_ or {}).get("storage_path")
        if meta_path:
            candidates.append(meta_path)
        try:
            if drawing.document and drawing.document.storage_path:
                candidates.append(drawing.document.storage_path)
        except Exception:
            pass
        candidates.append(f"drawings/{drawing.id}/{drawing.filename}")

        bucket = settings.minio_bucket
        last_exc: Exception | None = None
        for path in candidates:
            try:
                response = client.get_object(bucket, path)
                data = response.read()
                response.close()
                response.release_conn()
                return data
            except Exception as exc:
                last_exc = exc
                continue

        logger.error("load_drawing_file_all_paths_failed", drawing_id=str(drawing.id),
                     tried=candidates, error=str(last_exc))
        return b""
    except Exception as exc:
        logger.error("load_drawing_file_failed", error=str(exc))
        return b""


async def _load_catalog_file(file_path: str) -> bytes | None:
    """Load catalog file from MinIO path."""
    try:
        from minio import Minio

        from app.config import settings

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = settings.minio_bucket
        response = client.get_object(bucket, file_path)
        data = response.read()
        response.close()
        response.release_conn()
        return data
    except Exception as exc:
        logger.error("load_catalog_file_failed", file_path=file_path, error=str(exc))
        return None


async def _save_svg_artifacts(
    drawing_id: str,
    svg_content: str,
    drawing: Any,
) -> tuple[str | None, str | None]:
    """Save SVG and thumbnail to MinIO, return (svg_path, thumbnail_path)."""
    try:
        from minio import Minio

        from app.config import settings

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = settings.minio_bucket
        svg_path = f"drawings/{drawing_id}/drawing.svg"
        svg_bytes = svg_content.encode("utf-8")
        client.put_object(
            bucket, svg_path,
            io.BytesIO(svg_bytes), len(svg_bytes),
            content_type="image/svg+xml",
        )

        thumbnail_path = None
        try:
            import cairosvg
            png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=400)
            thumb_path = f"drawings/{drawing_id}/thumbnail.png"
            client.put_object(
                bucket, thumb_path,
                io.BytesIO(png_bytes), len(png_bytes),
                content_type="image/png",
            )
            thumbnail_path = thumb_path
        except Exception:
            pass

        return svg_path, thumbnail_path

    except Exception as exc:
        logger.error("save_svg_artifacts_failed", error=str(exc))
        return None, None


# ── Notification ──────────────────────────────────────────────────────────────


async def _notify_drawing_analyzed(drawing_id: str, features_count: int) -> None:
    """Send WebSocket notification via chat bus."""
    try:
        from app.core.chat_bus import chat_bus
        await chat_bus.publish({
            "type": "drawing_analyzed",
            "drawing_id": drawing_id,
            "features_count": features_count,
        })
    except Exception:
        pass


# ── Normalization Helpers ─────────────────────────────────────────────────────


def _safe_feature_type(value: str) -> Any:
    from app.db.models import DrawingFeatureType
    try:
        return DrawingFeatureType(value)
    except ValueError:
        return DrawingFeatureType.other


def _safe_primitive_type(value: str) -> Any:
    from app.db.models import FeaturePrimitiveType
    try:
        return FeaturePrimitiveType(value)
    except ValueError:
        return FeaturePrimitiveType.line


def _safe_dim_type(value: str) -> Any:
    from app.db.models import FeatureDimType
    try:
        return FeatureDimType(value)
    except ValueError:
        return FeatureDimType.linear


def _safe_roughness_type(value: str) -> Any:
    from app.db.models import RoughnessType
    try:
        return RoughnessType(value)
    except ValueError:
        return RoughnessType.Ra


def _safe_float(value: Any) -> float | None:
    """Parse a number as a Russian price list writes it.

    "1 200,00" (thin/non-breaking space as the thousands separator, comma as
    the decimal one) previously returned None — the price of every four-digit
    item was silently dropped while the row itself was imported.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Strip currency markers and thousands separators (space, NBSP, thin space,
    # apostrophe), then normalise the decimal comma.
    text = re.sub(r"[₽$€]|руб\.?|rub|eur|usd", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s\u00a0\u2009\u202f']", "", text)
    if "," in text and "." in text:
        # "1.234,56" — dot as thousands, comma as decimal.
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _normalize_header(header: str) -> str:
    """Normalize catalog column header to a known field name."""
    h = header.lower().strip()
    mappings = {
        "наименование": "name", "name": "name", "название": "name",
        "тип инструмента": "tool_type", "tool_type": "tool_type", "тип": "tool_type",
        "артикул": "part_number", "part_number": "part_number", "код": "part_number",
        "описание": "description", "description": "description",
        "диаметр": "diameter_mm", "diameter": "diameter_mm", "d": "diameter_mm", "ø": "diameter_mm",
        "длина": "length_mm", "length": "length_mm", "l": "length_mm",
        "материал": "material", "material": "material",
        "покрытие": "coating", "coating": "coating",
        "цена": "price", "price": "price", "стоимость": "price",
        "валюта": "currency", "currency": "currency",
        "страница": "catalog_page", "page": "catalog_page",
    }
    return mappings.get(h, re.sub(r"[^a-z0-9_]", "_", h))


# Название инструмента → ToolTypeEnum. Порядок важен: более длинные и более
# специфичные маркеры проверяются первыми («фреза концевая» перед «фреза»,
# «фреза дисковая» — это уже milling_cutter, а не endmill).
_TOOL_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("фреза концев", "endmill"), ("концевая фреза", "endmill"), ("endmill", "endmill"),
    ("end mill", "endmill"), ("фреза сферическ", "endmill"), ("фреза радиусн", "endmill"),
    ("фреза дисков", "milling_cutter"), ("фреза торцов", "milling_cutter"),
    ("фреза червячн", "milling_cutter"), ("фреза шпоночн", "endmill"),
    ("milling cutter", "milling_cutter"), ("фреза", "milling_cutter"),
    ("резьбофрез", "thread_mill"), ("thread mill", "thread_mill"),
    ("сверло", "drill"), ("сверл", "drill"), ("drill", "drill"), ("бор ", "drill"),
    ("метчик", "tap"), ("tap", "tap"),
    ("развертк", "reamer"), ("развёртк", "reamer"), ("reamer", "reamer"),
    ("зенковк", "countersink"), ("зенкер", "countersink"), ("countersink", "countersink"),
    ("цековк", "counterbore"), ("counterbore", "counterbore"),
    ("пластина", "insert"), ("пластин", "insert"), ("insert", "insert"), ("смп", "insert"),
    ("резец", "turning_tool"), ("резц", "turning_tool"), ("turning", "turning_tool"),
    ("расточн", "boring_bar"), ("boring", "boring_bar"),
    ("оправк", "holder"), ("патрон", "holder"), ("держател", "holder"),
    ("цанг", "holder"), ("holder", "holder"), ("chuck", "holder"),
    ("шлифов", "grinder"), ("круг", "grinder"), ("grind", "grinder"),
)


async def _infer_tool_types_via_llm(names: list[str]) -> dict[str, str]:
    """Second pass for rows the dictionary could not classify.

    One call per 50 names, not per row: the dictionary already resolves the
    common Russian wording, so this only sees the leftovers (brand-only names,
    English-only lines, transliterations). Failure is not fatal — the caller
    keeps "other" and the row stays in the catalog either way.
    """
    if not names:
        return {}

    from app.ai import ollama_client
    from app.ai.model_resolver import get_verify_model
    from app.db.models import ToolTypeEnum

    allowed = {t.value for t in ToolTypeEnum}
    model_config = get_verify_model()
    system = (
        "Classify each item name into one tool type. Return JSON only: "
        '{"types": {"<name>": "<type>"}}. The type must be one of: '
        + ", ".join(sorted(allowed))
        + ". Use \"other\" when the name is not a cutting/measuring tool. "
        "Never invent names that were not given."
    )

    resolved: dict[str, str] = {}
    for start in range(0, len(names), 50):
        batch = names[start : start + 50]
        try:
            response = await ollama_client.generate_json(
                json.dumps({"names": batch}, ensure_ascii=False),
                model=model_config.model,
                provider=model_config.provider,
                system=system,
            )
        except Exception as exc:  # noqa: BLE001 — classification is enrichment
            logger.warning("tool_type_llm_batch_failed", error=str(exc)[:200])
            continue
        types = (response or {}).get("types") or {}
        if not isinstance(types, dict):
            continue
        for name, value in types.items():
            normalized = _normalize_tool_type(str(value))
            if normalized in allowed and name in batch:
                resolved[name] = normalized
    return resolved


def _infer_tool_type(*parts: str | None) -> str | None:
    """Guess the tool type from the item's own text.

    Real price lists almost never carry a "тип инструмента" column, and the
    ingest used to DROP every row without one — a successful task with an empty
    catalog (measured: created=0, skipped=2 on a two-row CSV). Guessing from
    the name keeps the row; when nothing matches the caller falls back to
    ``other`` rather than discarding data.
    """
    haystack = " ".join(part for part in parts if part).lower().replace("ё", "е")
    if not haystack.strip():
        return None
    best: tuple[int, str] | None = None
    for marker, tool_type in _TOOL_TYPE_MARKERS:
        if marker in haystack and (best is None or len(marker) > best[0]):
            best = (len(marker), tool_type)
    return best[1] if best else None


def _normalize_tool_type(value: str) -> str:
    """Map Russian/mixed tool type names to ToolTypeEnum values."""
    v = value.lower().strip()
    mappings = {
        "сверло": "drill", "drill": "drill",
        "фреза": "endmill", "endmill": "endmill", "концевая фреза": "endmill",
        "пластина": "insert", "insert": "insert", "режущая пластина": "insert",
        "оправка": "holder", "holder": "holder",
        "метчик": "tap", "tap": "tap",
        "развёртка": "reamer", "reamer": "reamer",
        "расточная оправка": "boring_bar", "boring_bar": "boring_bar",
        "резьбофреза": "thread_mill", "thread_mill": "thread_mill",
        "шлифовальный": "grinder", "grinder": "grinder",
        "токарный резец": "turning_tool", "turning_tool": "turning_tool", "резец": "turning_tool",
        "фреза дисковая": "milling_cutter", "milling_cutter": "milling_cutter",
        "зенковка": "countersink", "countersink": "countersink",
        "цековка": "counterbore", "counterbore": "counterbore",
    }
    for key, mapped in mappings.items():
        if key in v:
            return mapped
    return "other"


def _build_drawing_embed_text(drawing: Any, title_block: dict, features: list) -> str:
    """Build text for drawing embedding."""
    parts = [
        drawing.filename,
        title_block.get("title", ""),
        title_block.get("drawing_number", ""),
        f"Материал: {title_block.get('material', '')}",
        f"Масштаб: {title_block.get('scale', '')}",
    ]
    for feat in features[:10]:
        parts.append(feat.get("name", ""))
    return " ".join(p for p in parts if p)


def _build_feature_embed_text(feature: Any, feat_data: dict) -> str:
    """Build text for feature embedding."""
    parts = [feature.feature_type.value, feature.name, feature.description or ""]
    for dim in feat_data.get("dimensions", [])[:3]:
        parts.append(dim.get("label", "") or f"{dim.get('nominal', '')} {dim.get('fit_system', '')}")
    for surf in feat_data.get("surfaces", [])[:2]:
        parts.append(f"{surf.get('roughness_type', 'Ra')} {surf.get('value', '')}")
    return " ".join(p for p in parts if p)


async def _load_few_shot_corrections(db: Any, *, drawing_type: str, limit: int = 10) -> list[dict]:
    """Load recent user corrections for use as few-shot examples in VLM prompts."""
    from sqlalchemy import select as sa_select

    from app.db.models import DrawingFeatureCorrection

    result = await db.execute(
        sa_select(DrawingFeatureCorrection)
        .where(DrawingFeatureCorrection.drawing_type == drawing_type)
        .order_by(DrawingFeatureCorrection.created_at.desc())
        .limit(limit)
    )
    corrections = result.scalars().all()
    return [
        {
            "description": f"{c.original_name} (VLM: {c.original_type})",
            "correct_type": c.corrected_type,
        }
        for c in corrections
    ]


# ── Background web-catalog ingestion (Ф3 / live-finding follow-up) ────────────


@celery_app.task(
    bind=True,
    name="drawing_analysis.ingest_web_catalog_sources",
    max_retries=0,
    # One source per task, with its own generous limits: the app-wide default
    # (soft 300s) killed a real catalog mid-parse and left the remaining
    # sources stuck at "queued" forever (caught on a live run).
    soft_time_limit=1800,
    time_limit=1860,
)
def ingest_web_catalog_sources(
    self,
    supplier_id: str,
    urls: list[str],
    max_pages: int = 10,
    max_chunks: int = 8,
) -> dict:
    """Fetch and ingest web catalog sources for one supplier.

    Runs in the worker because a real PDF catalog is minutes of local-LLM work
    per fragment: holding a chat turn open for that is the wrong shape — the
    agent queues the work, reports what it queued, and the per-source progress
    is readable from app.domain.catalog_ingest_status while it runs.

    Callers normally enqueue ONE url per task (see the attach endpoint), so a
    slow or broken source cannot consume another source's time budget.
    """
    from billiard.exceptions import SoftTimeLimitExceeded

    from app.domain.catalog_ingest_status import record_source_status

    try:
        return run_async(
            _ingest_web_catalog_sources_async(supplier_id, urls, max_pages, max_chunks)
        )
    except SoftTimeLimitExceeded:
        # Be explicit about what was cut off — a source silently frozen at
        # "running"/"queued" is indistinguishable from one still in progress.
        for url in urls:
            record_source_status(
                supplier_id, url, status="error",
                message="Разбор прерван по таймауту — источник слишком большой.",
            )
        raise


async def _ingest_web_catalog_sources_async(
    supplier_id: str, urls: list[str], max_pages: int, max_chunks: int
) -> dict:
    from app.api.web_search import WebFetchRequest, fetch_page
    from app.db.session import _get_session_factory
    from app.domain.catalog_ingest_status import record_source_status

    session_factory = _get_session_factory()
    totals = {"created": 0, "conflicted": 0, "sources": len(urls)}

    for url in urls:
        record_source_status(supplier_id, url, status="running", message="Читаю источник…")
        try:
            fetched = await fetch_page(
                WebFetchRequest(
                    url=url,
                    max_chars=200000,
                    ocr=max_pages > 0,
                    ocr_max_pages=max_pages,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one dead link can't stop the rest
            record_source_status(
                supplier_id, url, status="error",
                message=f"Не удалось открыть источник: {str(exc)[:200]}",
            )
            continue

        text = (fetched.text or "").strip()
        if len(text) < 200:
            record_source_status(
                supplier_id, url, status="empty", title=fetched.title,
                message=(
                    f"Читаемого текста нет ({len(text)} симв.) — JS-каталог или файл, "
                    "требующий скачивания."
                ),
            )
            continue

        record_source_status(
            supplier_id, url, status="running", title=fetched.title,
            message="Разбираю позиции каталога…",
        )
        try:
            async with session_factory() as db:
                result = await ingest_web_catalog_source(
                    db,
                    supplier_id,
                    url=url,
                    title=fetched.title,
                    text=text,
                    snippet=None,
                    max_chunks=max_chunks,
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            record_source_status(
                supplier_id, url, status="error", title=fetched.title,
                message=f"Ошибка разбора: {str(exc)[:200]}",
            )
            continue

        if result.get("error"):
            record_source_status(
                supplier_id, url, status="error", title=fetched.title,
                message=str(result["error"])[:200],
            )
            continue

        created = int(result["entries_created"])
        conflicted = int(result["entries_conflicted"])
        totals["created"] += created
        totals["conflicted"] += conflicted
        record_source_status(
            supplier_id, url,
            status="attached" if created or conflicted else "empty",
            title=fetched.title,
            entries_created=created,
            entries_conflicted=conflicted,
            message=(
                f"Добавлено позиций: {created}"
                + (f", на проверку: {conflicted}" if conflicted else "")
                if created or conflicted
                else "Позиций каталога в тексте не найдено."
            ),
        )

    logger.info(
        "web_catalog_sources_ingested",
        supplier_id=supplier_id, sources=len(urls), created=totals["created"],
    )
    return totals
