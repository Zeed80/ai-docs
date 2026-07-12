"""Celery task: vectorize a scanned/photographed drawing into CAD IR + DXF.

The deterministic core of the «Точный чертёж» mode:

    source image → classical preprocess (dewarp/deskew/denoise/CLAHE)
    → binarize (Otsu + speck removal + gap closing)
    → recognize primitives: neural seq2seq arbitrated against CV by
      independent coverage scoring (cad_recognize.verify.arbitrate_recognition) —
      never picked on the model's own confidence
    → OCR text regions → TextEntity annotations (+ excluded from stroking)
    → sheet frame detection → px→mm scale (or SCALE_UNKNOWN)
    → assemble CAD IR → independent coverage verification → validate
    → render PNG/SVG/DXF, store IR revision 0

No diffusion and no LLM anywhere in this path. When a photo is too dirty,
the user first runs the existing diffusion *cleanup* operation and then
vectorizes its result (the composer already supports generation-as-source) —
the two pipelines stay composable instead of entangled.

CPU-only: no GPU lock, no ComfyUI dependency, so it runs in the default
Celery queue and works when the GPU is busy training LoRA.
"""

from __future__ import annotations

import re
import uuid

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

# ГОСТ 2.301 sheet sizes (portrait, mm) — landscape is matched by swapping.
_GOST_SHEETS = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}
_FRAME_MIN_AREA_FRACTION = 0.5
_FRAME_ASPECT_TOL = 0.06

# ГОСТ 2.104: основная надпись sits bottom-right — same bottom-15%×right-30%
# convention already used by drawing_cleanup._text_exclusion_boxes and
# drawing_preprocessor._detect_title_block; kept in sync deliberately.
_TITLE_BLOCK_WIDTH_RATIO = 0.30
_TITLE_BLOCK_HEIGHT_RATIO = 0.15
_TITLE_BLOCK_MIN_INK_FRACTION = 0.01


@celery_app.task(
    bind=True,
    name="cad_trace.run_cad_trace",
    max_retries=2,
    soft_time_limit=600,
    time_limit=660,
)
def run_cad_trace(self, generation_id: str) -> dict:
    return run_async(_run(generation_id, self.request.id))


# A global Otsu threshold assumes a roughly bimodal histogram (clean dark
# ink on clean light paper) — it fails on foxed/stained/uneven-lit paper by
# reading the mottled staining itself as "ink", inflating density well past
# what real line-drawing content would ever produce. Confirmed live
# (2026-07-11, an aged diazo-print photo in test_vector_files): Otsu read
# ink_fraction=0.34 (above extract_primitives' 0.30 density-decline gate —
# the exact "лист слишком плотный" failure the user hit), while a local
# adaptive threshold on the SAME image read 0.14 and, after the same speck/
# gap hygiene, produced 1301 usable entities passing the full production
# coverage bar (recall 0.85, precision 1.0). Otsu is tried first and used
# whenever it's not egregiously dense — it is the simpler, more literal
# read of the ink and the 4 other test files all pass comfortably below the
# retry trigger, so this never touches an already-working image.
_OTSU_RETRY_INK_FRACTION = 0.22
# Sauvola is the last binarization resort: local mean/stddev thresholding
# survives severe uneven lighting where even the Gaussian adaptive retry
# stays implausibly dense (the CV density-decline gate is 0.30).
_SAUVOLA_RETRY_INK_FRACTION = 0.30
# B1 degrade-vs-fail split: an empty recognition on a sheet denser than this
# is garbage input (a near-solid photo), not a drawing to review.
_DEGRADED_MAX_INK_FRACTION = 0.85


def _binarize(image_bytes: bytes):
    """Otsu binarization + speck/gap hygiene (same recipe the cleanup
    postprocess uses), retried with local adaptive thresholding when Otsu
    alone reads implausibly dense (uneven lighting/staining, not real ink).
    Returns (ink uint8 mask 255=ink, w, h)."""
    import cv2
    import numpy as np

    from app.ai.drawing_cleanup import _close_small_gaps, _open_on_white, _remove_small_specks

    img = _open_on_white(image_bytes)
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = cv2.bitwise_not(binary)
    ink = _remove_small_specks(ink, w * h)
    ink = _close_small_gaps(ink)

    if float((ink > 0).mean()) > _OTSU_RETRY_INK_FRACTION:
        block = (max(15, min(h, w) // 25)) | 1  # odd, scales with resolution
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 20
        )
        adaptive_ink = cv2.bitwise_not(adaptive)
        adaptive_ink = _remove_small_specks(adaptive_ink, w * h)
        adaptive_ink = _close_small_gaps(adaptive_ink)
        if float((adaptive_ink > 0).mean()) < float((ink > 0).mean()):
            logger.info(
                "cad_trace_binarize_adaptive_retry",
                otsu_ink_fraction=round(float((ink > 0).mean()), 4),
                adaptive_ink_fraction=round(float((adaptive_ink > 0).mean()), 4),
            )
            ink = adaptive_ink

    # Last resort of the cascade (B1): Sauvola local thresholding, for sheets
    # still implausibly dense after the Gaussian adaptive retry (severe
    # uneven lighting / aged paper). Picked only when it is both cleaner and
    # non-empty — the cascade must never turn a readable sheet into a blank.
    frac = float((ink > 0).mean())
    if frac > _SAUVOLA_RETRY_INK_FRACTION and hasattr(cv2, "ximgproc"):
        try:
            block = (max(15, min(h, w) // 25)) | 1
            sauvola = cv2.ximgproc.niBlackThreshold(
                gray, 255, cv2.THRESH_BINARY, block, 0.2,
                binarizationMethod=cv2.ximgproc.BINARIZATION_SAUVOLA,
            )
            sauvola_ink = cv2.bitwise_not(sauvola)
            sauvola_ink = _remove_small_specks(sauvola_ink, w * h)
            sauvola_ink = _close_small_gaps(sauvola_ink)
            s_frac = float((sauvola_ink > 0).mean())
            if 0.0 < s_frac < frac:
                logger.info(
                    "cad_trace_binarize_sauvola_retry",
                    prev_ink_fraction=round(frac, 4),
                    sauvola_ink_fraction=round(s_frac, 4),
                )
                ink = sauvola_ink
        except Exception as exc:  # noqa: BLE001 — cascade stage is best-effort
            logger.warning("cad_trace_binarize_sauvola_failed", error=str(exc)[:120])

    return ink, w, h


def _detect_sheet_frame_quad(ink, w: int, h: int):
    """Find the dominant near-full-page rectangle contour (the ГОСТ 2.301
    sheet frame) and return its 4 approximated corner points (cv2
    approxPolyDP's Nx1x2 int array, N==4), or None when no plausible frame
    is present."""
    import cv2

    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_area = -1.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < _FRAME_MIN_AREA_FRACTION * w * h:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        if area > best_area:
            best_area = area
            best = approx
    return best


def _scale_from_quad(
    quad,
    w: int,
    h: int,
    confirmed_format: str | None = None,
) -> tuple[float | None, str | None]:
    """Derive mm-per-px only from a user-confirmed ГОСТ sheet format.

    Every A-series sheet has the same aspect ratio. Image pixels therefore
    cannot distinguish A4 from A0; the former implementation always matched
    the first dict entry (A4) and silently scaled A3/A2/A1/A0 incorrectly.
    """
    import cv2

    if confirmed_format not in _GOST_SHEETS:
        return None, None
    _x, _y, fw, fh = cv2.boundingRect(quad)
    frame_aspect = max(fw, fh) / max(min(fw, fh), 1.0)
    short_mm, long_mm = _GOST_SHEETS[confirmed_format]
    expected_aspect = long_mm / short_mm
    if abs(frame_aspect - expected_aspect) / expected_aspect > _FRAME_ASPECT_TOL:
        return None, None
    scale = long_mm / max(fw, fh)
    logger.info("sheet_frame_scale_confirmed", format=confirmed_format, scale=round(scale, 5))
    return scale, confirmed_format


def _frame_segments_from_quad(quad):
    """Synthesize the 4 sides of a detected sheet frame as Segment entities.

    The skeleton-tracing recognizer (drawing_vectorize) reliably finds the
    frame's ink but routinely FRAGMENTS it into dozens of short polylines —
    ГОСТ frames carry small perpendicular tick marks (zone/fold references)
    along their length, and each one is a junction that splits the
    continuous border into a new piece. Confirmed live (2026-07-11): on a
    clean, perfectly digital source drawing (detal_126.png — the easiest
    possible case) this fragmentation alone accounted for most of a 37%
    coverage-recall shortfall, because a rectangle's 4 long straight sides
    are exactly the geometry precision won't tolerate re-deriving via noisy
    fragment-stitching. The quad is already known deterministically from
    contour detection (same one _scale_from_quad uses) — emitting it
    directly is strictly more reliable than reassembling it from skeleton
    fragments, at the cost of some duplicate short polylines already found
    by CV along the same border (harmless for recall/precision scoring and
    editable away later; not worth an exclusion-zone plumbing change to
    avoid for revision 0).
    """
    from app.ai.cad_ir.schema import Point, Segment

    pts = [(float(p[0][0]), float(p[0][1])) for p in quad]
    return [
        Segment(
            p1=Point(x=pts[i][0], y=pts[i][1]),
            p2=Point(x=pts[(i + 1) % 4][0], y=pts[(i + 1) % 4][1]),
            line_class="contour",
            width_class="main",
            confidence=0.9,
            origin="cv",
        )
        for i in range(4)
    ]


def _detect_title_block(ink, w: int, h: int) -> dict | None:
    """Bottom-right heuristic (ГОСТ 2.104 основная надпись). Conservative:
    only reports a detection when that corner actually carries meaningful
    ink — an essentially blank corner (no stamp at all, or one cropped out
    of the scan) reports None rather than a confident-looking empty dict."""
    x0 = int(w * (1 - _TITLE_BLOCK_WIDTH_RATIO))
    y0 = int(h * (1 - _TITLE_BLOCK_HEIGHT_RATIO))
    region = ink[y0:h, x0:w]
    if region.size == 0:
        return None
    ink_fraction = float((region > 0).mean())
    if ink_fraction < _TITLE_BLOCK_MIN_INK_FRACTION:
        return None
    return {
        "detected": True,
        "region": {"x0": x0, "y0": y0, "x1": w, "y1": h},
        "ink_fraction": round(ink_fraction, 4),
    }


# ГОСТ 2.109 stamp scale callout, e.g. "М 1:2". The "М"/"M" prefix is
# REQUIRED (not just decorative) — the stamp region also carries "Лист X
# Листов Y" and drawing-number fields that could otherwise coincidentally
# look like a bare "N:M" ratio; a wrongly-inferred scale would produce a
# false ESKD_SCALE_NONSTANDARD warning, exactly the noise this is meant to
# avoid, not add.
_STAMP_SCALE_PATTERN = re.compile(r"[MМ]\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)


def _extract_stamp_scale(text_entities: list, region: dict) -> str | None:
    """The one real producer of ``ir.sheet.title_block["scale"]`` —
    cad_validate.ESKD_SCALE_NONSTANDARD reads that field but nothing wrote
    it before this: scan OCR text that landed inside the detected stamp
    region for a "N:M" ratio callout, normalized to bare "N:M" (stripping
    the "М" prefix) to match cad_validate's expected format exactly."""
    x0, y0, x1, y1 = region["x0"], region["y0"], region["x1"], region["y1"]
    for e in text_entities:
        pos = getattr(e, "position", None)
        text = getattr(e, "text", None)
        if pos is None or not text:
            continue
        if not (x0 <= pos.x <= x1 and y0 <= pos.y <= y1):
            continue
        m = _STAMP_SCALE_PATTERN.search(text)
        if m:
            num = m.group(1).replace(",", ".")
            den = m.group(2).replace(",", ".")
            return f"{num}:{den}"
    return None


def _ocr_text_entities(image_bytes: bytes):
    """OCR → (TextEntity list, exclusion boxes for the recognizer). Low
    tesseract confidence maps straight into entity confidence — bad reads
    land in the review queue instead of silently shipping."""
    from app.ai.cad_ir.schema import Point, SourceRegion, TextEntity
    from app.ai.text_preserve import detect_text_regions

    entities = []
    boxes: list[tuple[int, int, int, int]] = []
    for region in detect_text_regions(image_bytes):
        boxes.append((region.x, region.y, region.x + region.w, region.y + region.h))
        text = (region.text or "").strip()
        if not text:
            continue
        entities.append(
            TextEntity(
                position=Point(x=float(region.x), y=float(region.y + region.h)),
                text=text,
                height=float(max(region.h, 4)),
                confidence=max(0.0, min(1.0, region.conf / 100.0)),
                origin="cv",
                source_region=SourceRegion(
                    x0=float(region.x),
                    y0=float(region.y),
                    x1=float(region.x + region.w),
                    y1=float(region.y + region.h),
                ),
                evidence=[f"ocr:conf={region.conf:.0f}"],
            )
        )
    return entities, boxes


_VLM_ENRICH_CONFIDENCE_THRESHOLD = 0.75


async def _enrich_text_with_vlm(text_entities: list, source_bytes: bytes) -> None:
    """Escalate low-confidence OCR text to a VLM crop read (Ф4.1), attaching
    ranked alternatives in place. Bounded by MAX_CROP_READS_PER_RUN; a VLM
    failure on one crop never aborts the batch (read_crop_hypotheses already
    degrades to []) or the pipeline."""
    from app.ai.cad_hypothesis import apply_vlm_readings
    from app.ai.vlm_dimensions import MAX_CROP_READS_PER_RUN, crop_bytes_for_region, read_crop_hypotheses

    candidates = [
        e for e in text_entities
        if e.confidence < _VLM_ENRICH_CONFIDENCE_THRESHOLD and e.source_region is not None
    ][:MAX_CROP_READS_PER_RUN]
    for entity in candidates:
        crop = crop_bytes_for_region(source_bytes, entity.source_region)
        if crop is None:
            continue
        readings = await read_crop_hypotheses(crop, confidential=True)
        if readings:
            apply_vlm_readings(entity, readings)


_VLM_LINE_BUDGET = 15


async def _enrich_lines_with_vlm(ir, source_bytes: bytes) -> None:
    """Escalate ambiguous thin-stroke Segments to a VLM line classification
    (Ф4.3), attaching geometric alternatives in place. Bounded budget; one
    failed crop never aborts the rest."""
    from app.ai.cad_hypothesis import apply_line_hypotheses
    from app.ai.cad_ir.schema import Segment
    from app.ai.vlm_dimensions import classify_line_hypotheses, crop_bytes_for_bbox

    candidates = [
        e for e in ir.entities
        if isinstance(e, Segment) and e.width_class == "thin" and e.assurance != "human_approved"
    ][:_VLM_LINE_BUDGET]
    for entity in candidates:
        x0, y0 = min(entity.p1.x, entity.p2.x), min(entity.p1.y, entity.p2.y)
        x1, y1 = max(entity.p1.x, entity.p2.x), max(entity.p1.y, entity.p2.y)
        crop = crop_bytes_for_bbox(source_bytes, x0, y0, x1, y1)
        if crop is None:
            continue
        result = await classify_line_hypotheses(crop, confidential=True)
        if result.get("line_readings"):
            apply_line_hypotheses(entity, result)


async def _run(generation_id: str, task_id: str | None) -> dict:
    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.schema import SheetInfo
    from app.ai.cad_recognize import CvRecognizer
    from app.ai.cad_recognize.technical_vectorizer import TechnicalVectorizerRecognizer
    from app.ai.cad_recognize.verify import apply_to_ir, arbitrate_recognition, score_coverage
    from app.ai.cad_validate import validate_ir
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import cad_ir_store, studio_queue
    from app.storage import download_file, upload_file

    factory = _get_session_factory()
    gen_uuid = uuid.UUID(generation_id)

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return {"error": "generation not found"}
        job = await studio_queue.job_for_generation(db, gen_uuid)
        if gen.status == ImageGenStatus.cancelled:
            return {"cancelled": True}
        gen.status = ImageGenStatus.running
        gen.celery_task_id = task_id
        await studio_queue.mark_job_running(db, job, task_id=task_id)
        await db.commit()
        owner_sub = gen.owner_sub
        params = dict(gen.params or {})
        source_paths = list(gen.source_image_paths or [])
        # Ancestry for pixel provenance: when the source is a previous
        # diffusion result, remember what THAT generation was made from.
        parent_operation: str | None = None
        parent_source_path: str | None = None
        if params.get("source_generation_id"):
            try:
                parent = await db.get(
                    ImageGeneration, uuid.UUID(str(params["source_generation_id"]))
                )
                if parent:
                    parent_operation = parent.operation
                    parent_source_path = (parent.source_image_paths or [None])[0]
            except Exception as _exc:  # noqa: BLE001 — best-effort ancestry
                logger.warning("parent_lookup_failed", error=str(_exc))

    async def _fail(message: str) -> dict:
        from app.tasks.image_generation import _mark_failed

        await _mark_failed(gen_uuid, message, owner_sub)
        return {"error": message}

    try:
        if not source_paths:
            return await _fail("Для оцифровки нужен исходный скан/фото.")
        content = download_file(source_paths[0])

        # Stage 1: classical preprocess — same module the cleanup path trusts.
        try:
            from app.ai.drawing_cleanup import enhance_source_for_diffusion

            content = enhance_source_for_diffusion(content)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("cad_trace_enhance_failed", generation_id=generation_id, error=str(exc))

        # Stage 2: binarize.
        ink, w, h = _binarize(content)

        # Stage 3: OCR text (annotations + exclusion zones).
        text_entities, text_boxes = _ocr_text_entities(content)

        # Stage 3.5 (Ф4.1, opt-in via params): escalate low-confidence OCR
        # reads to a VLM crop read. Off by default — an extra network round
        # trip per uncertain text on a pipeline that was previously pure-CPU
        # and fast; enable once evaluated for latency in your deployment.
        # The VLM NEVER overwrites truth here — apply_vlm_readings keeps the
        # result at assurance=inferred; Stage 6.7 cross-checks decide.
        if params.get("vlm_dimensions"):
            await _enrich_text_with_vlm(text_entities, content)

        # Stage 4: scale (manual override wins; else frame detection).
        manual_scale = params.get("scale_mm_per_px")
        confirmed_format = str(params.get("sheet_format") or "").upper() or None
        sheet_format = None
        frame_quad = None
        scale_source = None
        if manual_scale:
            scale = float(manual_scale)
            scale_source = "manual"
        else:
            frame_quad = _detect_sheet_frame_quad(ink, w, h)
            scale, sheet_format = (
                _scale_from_quad(frame_quad, w, h, confirmed_format)
                if frame_quad is not None
                else (None, None)
            )
            if scale is not None:
                scale_source = "sheet_format"

        # Stage 4.5 (Ф4.4): title block (основная надпись) presence, purely
        # geometric — no OCR/VLM read of its FIELDS yet, just "is there one
        # and where" so the UI/normcontrol can point at it. The one field we
        # DO read here: a stated scale ("М 1:2") from OCR text that already
        # landed inside the region — the real producer for
        # ESKD_SCALE_NONSTANDARD's title_block["scale"] check.
        title_block = _detect_title_block(ink, w, h) or {}
        if title_block:
            stamp_scale = _extract_stamp_scale(text_entities, title_block["region"])
            if stamp_scale:
                title_block["scale"] = stamp_scale

        # Stage 5: recognize geometry — neural (if available) arbitrated
        # against CV by independent coverage scoring, never by the model's
        # own say-so (see cad_recognize/verify.arbitrate_recognition).
        arbitration = arbitrate_recognition(ink, text_boxes, TechnicalVectorizerRecognizer(), CvRecognizer())
        # B1: an empty recognition is a hard failure only when the sheet
        # itself is pathological (no ink at all / near-solid black). Anything
        # in between degrades to a reviewable draft: the ink ships as raster
        # passthrough with whatever frame/text WAS recognized, flagged
        # RECOGNITION_EMPTY — the user reviews and traces in the editor
        # instead of hitting a dead "лист слишком плотный или пустой" error.
        degraded_recognition = not arbitration.entities
        ink_fraction = float((ink > 0).mean())
        if degraded_recognition and not 0.0 < ink_fraction <= _DEGRADED_MAX_INK_FRACTION:
            return await _fail(
                "Не удалось распознать линейную графику: лист "
                + ("пустой. " if ink_fraction <= 0.0 else "почти полностью залит — не похож на линейный чертёж. ")
                + "Попробуйте сначала пропустить фото через режим «Очистка»."
            )
        keep_raster = arbitration.keep_raster
        thin_px, thick_px = arbitration.thin_px, arbitration.thick_px
        if degraded_recognition:
            keep_raster = ink > 0

        # The frame quad (Stage 4) is emitted as real Segment entities here,
        # not just used for scale — see _frame_segments_from_quad for why
        # skeleton-traced fragments alone systematically under-recognize a
        # sheet border. Coverage is rescored below to reflect it.
        frame_segments = _frame_segments_from_quad(frame_quad) if frame_quad is not None else []

        ir = CadIR(
            source=SourceInfo(
                generation_id=generation_id, image_width=w, image_height=h, kind="scan"
            ),
            scale=scale,
            scale_source=scale_source,
            sheet=SheetInfo(format=sheet_format, frame=frame_quad is not None, title_block=title_block),
            entities=[*arbitration.entities, *frame_segments, *text_entities],
            recognizer_used=arbitration.recognizer_used,
        )

        # Stage 5.5 (Ф4.3, opt-in via params): escalate ambiguous thin lines
        # (axis/hidden/dim all render as the same thin stroke in raster —
        # the CV width-only heuristic can't tell them apart) to a VLM crop
        # classification. Same non-blocking, off-by-default contract as
        # Stage 3.5.
        if params.get("vlm_lines"):
            await _enrich_lines_with_vlm(ir, content)

        # Stage 6: verification score. Rescored when frame segments were
        # added (Stage 5) — arbitration.score predates them and would under-
        # report recall for a sheet whose frame just got recognized.
        score = (
            score_coverage(
                [*arbitration.entities, *frame_segments], ink,
                keep_raster, thin_px, thick_px,
            )
            if frame_segments
            else arbitration.score
        )
        apply_to_ir(ir, score)

        # Stage 6.7 (Ф4.2/4.3): cross-check any VLM reading/line-class
        # hypotheses attached in Stage 3.5/5.5 — promotes a decisive winner,
        # queues genuine ambiguity for human review. No-op when nothing has
        # alternatives.
        from app.ai.cad_hypothesis import resolve_hypotheses, resolve_line_hypotheses

        resolve_hypotheses(ir)
        resolve_line_hypotheses(ir)

        validate_ir(ir)

        # Recognition-provenance signals are appended AFTER validate_ir:
        # validate_ir rebuilds the issue list from IR-derivable checks (plus
        # sticky DIFFUSION_*) and cannot re-derive these pipeline facts —
        # appended before it, they were silently wiped (pre-existing bug for
        # NEURAL_UNAVAILABLE/RECOGNIZER_DISCREPANCY). Their lifecycle is
        # intentionally revision-0-only: the next revalidation after a human
        # edit drops them, while quality gating stays with COVERAGE_LOW.
        from app.ai.cad_ir.schema import ValidationIssueIR

        if degraded_recognition:
            ir.validation.issues.append(ValidationIssueIR(
                code="RECOGNITION_EMPTY", severity="error",
                message_ru=(
                    "Векторная геометрия не распознана — лист сохранён растровой подложкой "
                    "с рамкой и текстом. Проверьте исходник, попробуйте режим «Очистка» "
                    "или обведите геометрию вручную в редакторе."
                ),
            ))
        if not arbitration.neural_available:
            ir.validation.issues.append(ValidationIssueIR(
                code="NEURAL_UNAVAILABLE", severity="info",
                message_ru="Нейросетевой распознаватель недоступен — использован классический CV-путь.",
            ))
        if arbitration.discrepancy:
            n = arbitration.notes
            ir.validation.issues.append(ValidationIssueIR(
                code="RECOGNIZER_DISCREPANCY", severity="warn",
                message_ru=(
                    f"Нейросеть и классический CV дали расходящиеся результаты "
                    f"({n.get('neural_entities')} vs {n.get('cv_entities')} элементов) "
                    f"— использован результат {arbitration.recognizer_used}, сверьте с оригиналом."
                ),
            ))

        # Stage 6.5: pixel provenance for diffusion-derived sources — diffusion
        # output is not a truth source. Findings are sticky (survive later
        # revalidation) until the flagged entities are resolved by a human.
        if parent_operation in ("cleanup", "edit", "inpaint", "eskd", "generate"):
            from app.ai.cad_ir.schema import ReviewItem, ValidationIssueIR
            from app.ai.pixel_provenance import (
                diffusion_change_masks,
                entities_in_mask,
                mask_regions,
                uncovered_added_regions,
            )

            masks = None
            if parent_source_path:
                try:
                    masks = diffusion_change_masks(ink, download_file(parent_source_path))
                except Exception:  # noqa: BLE001
                    masks = None
            if masks is not None:
                added, removed = masks
                flagged = entities_in_mask(
                    ir.entities, added, arbitration.thin_px, arbitration.thick_px
                )
                logger.info("diffusion_provenance", flagged=len(flagged), removed_px=int(removed.sum()))
                if flagged:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_ADDED_INK",
                        severity="warn",
                        entity_ids=flagged,
                        message_ru=(
                            f"{len(flagged)} элемент(ов) распознаны из областей, ДОРИСОВАННЫХ "
                            "диффузионной очисткой, — их не было на исходном фото. Подтвердите или удалите."
                        ),
                    ))
                    queued = {r.entity_id for r in ir.review}
                    for eid in flagged:
                        if eid not in queued:
                            ir.review.append(ReviewItem(entity_id=eid, reason="diffusion_modified"))
                # Added ink that never became an entity (raster-passthrough
                # zones like OCR exclusions) must not ship silently either.
                flagged_entities = [e for e in ir.entities if e.id in set(flagged)]
                orphan_boxes = uncovered_added_regions(
                    added, flagged_entities, arbitration.thin_px, arbitration.thick_px
                )
                if orphan_boxes:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_ADDED_INK",
                        severity="warn",
                        message_ru=(
                            f"Диффузионная очистка ДОРИСОВАЛА графику в {len(orphan_boxes)} "
                            f"растровых зон(ах), не ставших элементами (крупнейшая: {orphan_boxes[0]}). "
                            "Сверьте эти области с оригиналом."
                        ),
                    ))
                boxes = mask_regions(removed)
                if boxes:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_REMOVED_INK",
                        severity="warn",
                        message_ru=(
                            f"Диффузионная очистка СТЁРЛА {len(boxes)} участок(ов) исходной графики "
                            f"(крупнейший: {boxes[0]}). Сверьте с оригиналом."
                        ),
                    ))
            else:
                ir.validation.issues.append(ValidationIssueIR(
                    code="DIFFUSION_SOURCE_UNVERIFIED",
                    severity="warn",
                    message_ru=(
                        "Источник — результат генеративной модели, и сверка с оригиналом недоступна: "
                        "происхождение графики не подтверждено. Проверяйте размеры по бумажному оригиналу."
                    ),
                ))

        # Stage 7: persist revision 0 + renders.
        async with factory() as db:
            gen = await db.get(ImageGeneration, gen_uuid)
            if not gen or gen.status == ImageGenStatus.cancelled:
                return {"cancelled": True}
            normalized_path = f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_normalized.png"
            upload_file(content, normalized_path, "image/png")
            gen.params = {
                **(gen.params or {}),
                "normalized_source_path": normalized_path,
            }
            await cad_ir_store.save_revision(
                db, gen, ir,
                origin="auto",
                created_by=owner_sub,
                keep_raster=keep_raster,
                thin_px=thin_px,
                thick_px=thick_px,
            )
            gen.status = ImageGenStatus.done
            job = await studio_queue.job_for_generation(db, gen_uuid)
            await studio_queue.mark_job_done(db, job)
            await db.commit()

            if owner_sub:
                try:
                    from app.services import push

                    await push.push_to_user(
                        db=db,
                        user_sub=owner_sub,
                        title="Оцифровка готова",
                        body="Чертёж распознан — DXF и проверка доступны в студии.",
                        action_url=f"/studio?id={generation_id}",
                        notification_type="image_ready",
                    )
                except Exception:  # noqa: BLE001
                    pass

        return {
            "ok": True,
            "generation_id": generation_id,
            "entities": len(ir.entities),
            "coverage": [arbitration.score.recall, arbitration.score.precision],
            "recognizer_used": arbitration.recognizer_used,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("cad_trace_failed", generation_id=generation_id, error=str(exc))
        return await _fail(f"{type(exc).__name__}: {exc}")
