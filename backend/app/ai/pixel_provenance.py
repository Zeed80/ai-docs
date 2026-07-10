"""Pixel-provenance masks: what did a diffusion pass add or remove?

A diffusion-cleaned sheet is not a truth source — the model may draw a
plausible line that never existed or silently drop one. When the vectorize
pipeline runs on a diffusion result, this module compares its binarized ink
against the diffusion generation's OWN source (the original photo/scan),
aligned into the result frame, and yields:

    added   — ink present in the result but absent in the source
              (hallucination candidates: entities here are demoted and
              queued for review)
    removed — ink present in the source but absent in the result
              (silently dropped strokes: reported as validation issues)

Best-effort: alignment failure returns None and the caller falls back to a
sheet-level "unverified diffusion source" warning instead of guessing.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

# A stroke has to be meaningfully displaced to count as added/removed; this
# mirrors the coverage dilation used by the verifier.
_CHANGE_DILATE_PX = 5
_MIN_CHANGED_COMPONENT_PX = 40  # ignore speck-level diffs
_ENTITY_CHANGED_OVERLAP = 0.35  # fraction of an entity's own ink inside `added`


def diffusion_change_masks(result_ink, source_image_bytes: bytes):
    """(added_mask, removed_mask) as bool arrays in the RESULT frame, or None."""
    import cv2
    import numpy as np

    from app.ai.image_align import estimate_source_to_result
    from app.tasks.cad_trace import _binarize

    try:
        # Same classical preprocess the pipeline itself uses: a raw photo has
        # perspective the affine aligner can't absorb — dewarp first.
        try:
            from app.ai.drawing_cleanup import enhance_source_for_diffusion

            source_image_bytes = enhance_source_for_diffusion(source_image_bytes)
        except Exception:  # noqa: BLE001
            pass
        source_ink, _sw, _sh = _binarize(source_image_bytes)

        # Align the source into the result frame. estimate_source_to_result
        # expects the result as PNG bytes — encode the ink raster back.
        h, w = result_ink.shape[:2]
        result_gray = np.where(result_ink > 0, 0, 255).astype(np.uint8)
        ok, buf = cv2.imencode(".png", result_gray)
        if not ok:
            return None
        matrix = estimate_source_to_result(buf.tobytes(), source_image_bytes)
        if matrix is None:
            src_warped = cv2.resize(source_ink, (w, h), interpolation=cv2.INTER_NEAREST)
        else:
            src_warped = cv2.warpAffine(
                source_ink, np.asarray(matrix, dtype=np.float32)[:2], (w, h),
                flags=cv2.INTER_NEAREST,
            )

        k = 2 * _CHANGE_DILATE_PX + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        src_grown = cv2.dilate(src_warped, kernel) > 0
        res_grown = cv2.dilate((result_ink > 0).astype(np.uint8), kernel) > 0

        added = (result_ink > 0) & ~src_grown
        removed = (src_warped > 0) & ~res_grown
        added = _drop_specks(added)
        removed = _drop_specks(removed)
        logger.info(
            "diffusion_change_masks",
            added_px=int(added.sum()),
            removed_px=int(removed.sum()),
            aligned=matrix is not None,
        )
        return added, removed
    except Exception as exc:  # noqa: BLE001 — provenance must not kill the run
        logger.warning("diffusion_change_masks_failed", error=str(exc))
        return None


def _drop_specks(mask):
    import cv2
    import numpy as np

    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    out = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= _MIN_CHANGED_COMPONENT_PX:
            out |= labels == i
    return out


def entities_in_mask(entities, added_mask, thin_px: int, thick_px: int) -> list[str]:
    """Ids of entities whose own rasterized ink lies substantially inside the
    ``added`` mask — i.e. geometry the diffusion pass invented."""
    import numpy as np

    from app.ai.cad_ir.png_render import rasterize_entities

    h, w = added_mask.shape[:2]
    flagged: list[str] = []
    for entity in entities:
        if entity.type in ("text",):  # text provenance handled by OCR path
            continue
        canvas = rasterize_entities([entity], w, h, thin_px, thick_px)
        own = canvas < 128
        total = int(own.sum())
        if total == 0:
            continue
        overlap = int((own & added_mask).sum()) / total
        if overlap >= _ENTITY_CHANGED_OVERLAP:
            flagged.append(entity.id)
    return flagged


def mask_regions(mask, max_regions: int = 20) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of significant connected components (x0, y0, x1, y1)."""
    import cv2

    n, _labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype("uint8"), connectivity=8
    )
    boxes = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= _MIN_CHANGED_COMPONENT_PX:
            boxes.append((int(x), int(y), int(x + w), int(y + h)))
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    return boxes[:max_regions]


# Backwards-friendly alias: removed-ink regions are just mask regions.
removed_regions = mask_regions


def uncovered_added_regions(
    added_mask, flagged_entities, thin_px: int, thick_px: int
) -> list[tuple[int, int, int, int]]:
    """Added-ink regions NOT explained by flagged entities — e.g. hallucinated
    ink that slipped into a raster-passthrough zone (OCR exclusion, solid
    fill) and never became an entity. These must surface as issues too:
    otherwise diffusion-added pixels ship silently inside the raster layer."""
    import cv2
    import numpy as np

    from app.ai.cad_ir.png_render import rasterize_entities

    residual = added_mask.copy()
    if flagged_entities:
        h, w = added_mask.shape[:2]
        covered = rasterize_entities(flagged_entities, w, h, thin_px, thick_px) < 128
        k = 2 * _CHANGE_DILATE_PX + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        covered = cv2.dilate(covered.astype(np.uint8), kernel) > 0
        residual = residual & ~covered
    return mask_regions(residual)
