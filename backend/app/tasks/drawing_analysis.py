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
import structlog
from pathlib import Path
from typing import Any

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
    return asyncio.get_event_loop().run_until_complete(
        _analyze_drawing_async(drawing_id, model, allow_cloud, max_views, force_drawing_type)
    )


async def _analyze_drawing_async(
    drawing_id: str,
    model: str | None,
    allow_cloud: bool = False,
    max_views: int = 6,
    force_drawing_type: str | None = None,
) -> dict:
    from app.db.session import _get_session_factory
    from app.db.models import Drawing, DrawingStatus, DrawingFeature, FeatureContour, FeatureDimension, FeatureSurface, FeatureGDT
    from app.ai.drawing_extractor import extract_drawing_features, extract_features_from_image
    from app.domain.drawing_graph import ingest_drawing_graph
    from app.ai.embeddings import embed_text as get_text_embedding
    from app.vector.qdrant_store import (
        ensure_drawing_collections,
        upsert_drawing,
        upsert_drawing_feature,
        VECTOR_SIZE,
    )
    from sqlalchemy import select

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
        from app.ai.task_routing import resolve_model
        from app.ai.schemas import AITask as _AITask
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
                from app.ai.step_extractor import extract_step_geometry, StepGeometryResult
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
                from app.ai.drawing_extractor import classify_drawing_image, DrawingClassification
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
            from app.ai.drawing_validator import validate_drawing_extraction, report_to_dict
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

    asyncio.get_event_loop().run_until_complete(_inner())


# ── Supplier Catalog Ingestion Task ───────────────────────────────────────────


@celery_app.task(bind=True, name="drawing_analysis.ingest_supplier_catalog", max_retries=2)
def ingest_supplier_catalog(self, supplier_id: str, file_path: str, filename: str) -> dict:
    """
    Parse supplier tool catalog file and ingest into DB + Qdrant + Graph.
    Supports: PDF (table extraction), Excel (.xlsx), CSV, JSON.
    """
    return asyncio.get_event_loop().run_until_complete(
        _ingest_catalog_async(supplier_id, file_path, filename)
    )


async def _ingest_catalog_async(
    supplier_id: str, file_path: str, filename: str
) -> dict:
    from app.db.session import _get_session_factory
    from app.db.models import ToolCatalogEntry, ToolSupplier
    from app.domain.drawing_graph import ingest_tool_catalog_graph
    from app.ai.embeddings import embed_text as get_text_embedding
    from app.vector.qdrant_store import ensure_drawing_collections, upsert_tool_catalog_entry

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

    created = 0
    updated = 0
    skipped = 0
    errors: list[str] = []

    async with _get_session_factory()() as db:
        supplier = await db.get(ToolSupplier, supplier_uuid)
        if not supplier:
            return {"error": f"Supplier {supplier_id} not found"}

        for row in rows:
            try:
                if not row.get("name") or not row.get("tool_type"):
                    skipped += 1
                    continue

                from app.db.models import ToolTypeEnum
                tool_type_str = _normalize_tool_type(row.get("tool_type", ""))
                try:
                    tool_type = ToolTypeEnum(tool_type_str)
                except ValueError:
                    tool_type = ToolTypeEnum.other

                entry = ToolCatalogEntry(
                    supplier_id=supplier_uuid,
                    part_number=row.get("part_number"),
                    tool_type=tool_type,
                    name=str(row.get("name", ""))[:500],
                    description=row.get("description"),
                    diameter_mm=_safe_float(row.get("diameter_mm") or row.get("diameter")),
                    length_mm=_safe_float(row.get("length_mm") or row.get("length")),
                    material=row.get("material"),
                    coating=row.get("coating"),
                    price_currency=row.get("currency", "RUB"),
                    price_value=_safe_float(row.get("price")),
                    catalog_page=_safe_int(row.get("catalog_page") or row.get("page")),
                    parameters={k: v for k, v in row.items()
                                if k not in ("name", "tool_type", "part_number", "description",
                                            "diameter_mm", "diameter", "length_mm", "length",
                                            "material", "coating", "currency", "price",
                                            "catalog_page", "page")},
                )
                db.add(entry)
                await db.flush()

                # Embed → Qdrant
                embed_text = (
                    f"{tool_type.value} {entry.name} "
                    + (f"Ø{entry.diameter_mm}мм " if entry.diameter_mm else "")
                    + (f"{entry.material} " if entry.material else "")
                    + (f"{entry.coating} " if entry.coating else "")
                    + (entry.description or "")
                )
                vector = await get_text_embedding(embed_text)
                if vector:
                    upsert_tool_catalog_entry(
                        entry_id=str(entry.id),
                        vector=vector,
                        tool_type=tool_type.value,
                        name=entry.name,
                        supplier_id=str(supplier_uuid),
                        diameter_mm=entry.diameter_mm,
                        material=entry.material,
                    )
                    entry.embedding_id = f"tool_catalog:{entry.id}"

                # Graph node
                try:
                    await ingest_tool_catalog_graph(entry.id, db)
                except Exception:
                    pass

                created += 1

            except Exception as row_exc:
                errors.append(str(row_exc)[:200])
                skipped += 1

        await db.commit()

    logger.info(
        "catalog_ingested",
        supplier_id=supplier_id,
        created=created,
        skipped=skipped,
    )
    return {
        "supplier_id": supplier_id,
        "entries_created": created,
        "entries_updated": updated,
        "entries_skipped": skipped,
        "errors": errors[:10],
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
            except asyncio.TimeoutError:
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
    import ezdxf

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
    import tempfile
    import os

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
            from ezdxf.addons.drawing import RenderContext, Frontend
            from ezdxf.addons.drawing.svg import SVGBackend
            from ezdxf.addons.drawing.layout import Page, Units

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
        from svglib.svglib import svg2rlg  # type: ignore
        from reportlab.graphics import renderPM  # type: ignore
        import io
        import tempfile, os
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
        import io
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
    from collections import Counter
    import html

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

    if file_ext in (".xlsx", ".xls"):
        rows = _parse_excel_catalog(file_bytes)
    elif file_ext == ".csv":
        rows = _parse_csv_catalog(file_bytes)
    elif file_ext == ".json":
        rows = _parse_json_catalog(file_bytes)
    elif file_ext == ".pdf":
        rows = await _parse_pdf_catalog(file_bytes)
    else:
        logger.warning("unknown_catalog_format", ext=file_ext)

    return rows


def _parse_excel_catalog(file_bytes: bytes) -> list[dict]:
    """Parse Excel catalog file."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active
        rows = []
        headers: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [_normalize_header(str(c or "")) for c in row]
                continue
            if not any(row):
                continue
            row_dict = {}
            for j, cell in enumerate(row):
                if j < len(headers) and headers[j]:
                    row_dict[headers[j]] = cell
            if row_dict:
                rows.append(row_dict)
        wb.close()
        return rows
    except Exception as exc:
        logger.error("excel_catalog_parse_failed", error=str(exc))
        return []


def _parse_csv_catalog(file_bytes: bytes) -> list[dict]:
    """Parse CSV catalog file."""
    import csv

    try:
        text = file_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            normalized = {_normalize_header(k): v for k, v in row.items()}
            if normalized:
                rows.append(normalized)
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


async def _parse_pdf_catalog(file_bytes: bytes) -> list[dict]:
    """Extract tables from PDF catalog using pdfplumber."""
    rows = []
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages[:50]:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    headers = [_normalize_header(str(h or "")) for h in table[0]]
                    for row in table[1:]:
                        if not any(row):
                            continue
                        row_dict: dict = {}
                        for j, cell in enumerate(row):
                            if j < len(headers) and headers[j]:
                                row_dict[headers[j]] = cell
                        if row_dict:
                            rows.append(row_dict)
    except ImportError:
        logger.warning("pdfplumber_not_installed")
    except Exception as exc:
        logger.error("pdf_catalog_parse_failed", error=str(exc))
    return rows


# ── MinIO Helpers ─────────────────────────────────────────────────────────────


async def _load_drawing_file(drawing: Any) -> bytes:
    """Load drawing file bytes from MinIO.

    Resolution order:
    1. metadata_.storage_path  — set by both upload and document-auto-create flows
    2. drawing.document.storage_path — if document FK is loaded
    3. drawings/{id}/{filename} — canonical drawing bucket path
    """
    try:
        from app.config import settings
        from minio import Minio

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
        from app.config import settings
        from minio import Minio

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
        from app.config import settings
        from minio import Minio

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
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
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
