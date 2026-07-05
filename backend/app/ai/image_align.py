"""Affine registration of a diffusion output back onto its source drawing.

Why: diffusion re-layouts the sheet a little on every run — and a cleanup
LoRA trained on rendered targets does it systematically (confirmed live:
the v1 LoRA put the drawing inside rendered-sheet margins, so
text_preserve's proportionally-mapped paste landed visibly off). Everything
downstream of diffusion (vector reconstruction, OCR-anchored text paste)
assumes the output's layout matches the source's. This module measures the
actual affine drift between the two ink patterns and warps the diffusion
output back — after it, proportional mapping is valid again.

Placement in the pipeline matters: alignment runs BETWEEN diffusion and the
vector reconstruction, so the resampling blur a warp introduces is erased by
the redraw that follows.

Best-effort by design: if ECC fails to converge or the found transform is
implausible (a wild scale/shift means the two images just don't correspond),
the original bytes are returned unchanged — identical to today's behaviour.
"""

from __future__ import annotations

import io

import structlog

logger = structlog.get_logger()

_WORK_LONG_SIDE = 1000        # ECC on full-res masks is slow and no more accurate
_MAX_ECC_ITERATIONS = 200
_ECC_EPS = 1e-6
# Confidence gate: ECC's correlation coefficient on a genuinely corresponding
# pair (same drawing, small drift) comes out high; a heavily re-drawn output
# (v1 LoRA re-layouts) yields ~0.2 with several contradictory "solutions" —
# ECC, phase correlation and AKAZE+RANSAC each returned a different answer on
# the same live pair. Below the gate we do nothing, which is never worse.
_MIN_ECC_CC = 0.5
# Plausibility gate: cleanup drift is a few percent of the sheet. Anything
# bigger means registration latched onto the wrong structure — keep identity.
_SCALE_RANGE = (0.85, 1.18)
_MAX_SHIFT_FRACTION = 0.08


def estimate_source_to_result(result_png: bytes, source_bytes: bytes):
    """Estimate the affine map from SOURCE pixel coordinates to RESULT pixel
    coordinates (result resized to the source's size), or None when no
    confident correspondence exists.

    Preferred over warping the result itself: warping drags the (straight,
    clean) diffusion output onto the source photo's residual tilt — confirmed
    live: windows came out as parallelograms. Mapping the text-paste
    coordinates through this matrix instead leaves the drawing exactly as
    drawn and moves only the paste targets."""
    try:
        import numpy as np

        got = _estimate(result_png, source_bytes)
        if got is None:
            return None
        warp, scale = got
        full = warp.copy()
        full[:, 2] /= scale
        return np.asarray(full)
    except Exception as exc:  # noqa: BLE001
        logger.warning("align_estimate_failed", error=str(exc))
        return None


def align_result_to_source(result_png: bytes, source_bytes: bytes) -> bytes:
    """Warp ``result_png`` (diffusion output) so its ink pattern lines up
    with ``source_bytes``'s. Returns the aligned PNG at the source's
    resolution, or the input unchanged when no confident alignment exists.

    NOTE: prefer ``estimate_source_to_result`` + coordinate mapping for the
    text paste — warping the whole result inherits the source photo's
    residual tilt (confirmed live)."""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        got = _estimate(result_png, source_bytes)
        if got is None:
            return result_png
        warp, scale = got
        full = warp.copy()
        full[:, 2] /= scale
        src_img = Image.open(io.BytesIO(source_bytes))
        res_img = Image.open(io.BytesIO(result_png)).convert("RGB")
        res_full = np.asarray(res_img.resize(src_img.size, Image.LANCZOS))
        aligned = cv2.warpAffine(
            res_full, full, src_img.size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
        )
        logger.info(
            "align_applied",
            scale_x=round(float(warp[0, 0]), 3), scale_y=round(float(warp[1, 1]), 3),
            dx=round(float(full[0, 2]), 1), dy=round(float(full[1, 2]), 1),
        )
        buf = io.BytesIO()
        Image.fromarray(aligned).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the pipeline
        logger.warning("align_failed", error=str(exc))
        return result_png


def _estimate(result_png: bytes, source_bytes: bytes):
    """Shared ECC estimation. Returns (warp_in_work_coords, work_scale) or
    None. ``warp`` maps source→result in the downscaled work frame."""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        src_img = Image.open(io.BytesIO(source_bytes)).convert("L")
        res_img = Image.open(io.BytesIO(result_png)).convert("RGB")

        scale = _WORK_LONG_SIDE / max(src_img.size)
        work_w, work_h = round(src_img.width * scale), round(src_img.height * scale)

        src_small = np.asarray(src_img.resize((work_w, work_h), Image.BILINEAR))
        res_small = np.asarray(
            res_img.convert("L").resize((work_w, work_h), Image.BILINEAR)
        )
        src_mask = _ink_mask(src_small)
        res_mask = _ink_mask(res_small)
        if src_mask.mean() < 0.002 or res_mask.mean() < 0.002:
            logger.info("align_skipped_no_ink")
            return None

        # Two independent starts, first survivor of the gates wins:
        # 1) coarse-to-fine from identity (wide basin, no seeding bias);
        # 2) phase-correlation seed at full resolution (rescues cases where
        #    the pyramid diverges on thin ridge-like masks — synthetic test).
        # Neither is trustworthy alone (phase correlation locks onto the
        # wrong window of a periodic facade — confirmed live, 141px off),
        # which is exactly what the cc/plausibility gates below are for.
        cc, warp = -1.0, None
        for candidate in (_ecc_pyramid, _ecc_phase_seeded):
            try:
                cc, warp = candidate(src_mask, res_mask, work_w, work_h)
                break
            except cv2.error as exc:
                logger.debug("align_candidate_failed", error=str(exc)[:100])
        if warp is None:
            logger.info("align_ecc_failed_all_candidates")
            return None

        if cc < _MIN_ECC_CC:
            logger.info("align_rejected_low_confidence", cc=round(float(cc), 3))
            return None
        if not _plausible(warp, work_w, work_h):
            logger.warning(
                "align_rejected_implausible", warp=[round(float(v), 3) for v in warp.ravel()]
            )
            return None
        logger.info(
            "align_estimated",
            cc=round(float(cc), 3),
            scale_x=round(float(warp[0, 0]), 3), scale_y=round(float(warp[1, 1]), 3),
        )
        return warp, scale
    except ImportError:
        logger.debug("align_no_cv2")
        return None


_ECC_CRITERIA = None  # set lazily (needs cv2)


def _criteria():
    import cv2

    return (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, _MAX_ECC_ITERATIONS, _ECC_EPS)


def _ecc_pyramid(src_mask, res_mask, work_w: int, work_h: int):
    import cv2
    import numpy as np

    quarter_src = cv2.resize(src_mask, (work_w // 4, work_h // 4))
    quarter_res = cv2.resize(res_mask, (work_w // 4, work_h // 4))
    warp = np.eye(2, 3, dtype=np.float32)
    _cc, warp = cv2.findTransformECC(
        quarter_src, quarter_res, warp, cv2.MOTION_AFFINE, _criteria(), None, 5
    )
    warp[:, 2] *= 4
    return cv2.findTransformECC(
        src_mask, res_mask, warp, cv2.MOTION_AFFINE, _criteria(), None, 5
    )


def _ecc_phase_seeded(src_mask, res_mask, work_w: int, work_h: int):
    import cv2
    import numpy as np

    (dx0, dy0), _resp = cv2.phaseCorrelate(src_mask, res_mask)
    warp = np.array([[1, 0, dx0], [0, 1, dy0]], dtype=np.float32)
    return cv2.findTransformECC(
        src_mask, res_mask, warp, cv2.MOTION_AFFINE, _criteria(), None, 5
    )


def _ink_mask(gray):
    """Float32 ink-probability image for ECC: dark STROKES → 1, paper → 0.

    Morphological blackhat, not a global threshold: the source here is a
    gray photo whose shadows/背景 gradients survive CLAHE — Otsu turned whole
    shaded regions into \"ink\" and ECC locked onto those clouds instead of
    the linework (confirmed live: it dragged an almost-aligned pair 138px
    away). Blackhat responds only to features darker than their local
    neighbourhood at stroke scale, which is exactly the drawing."""
    import cv2
    import numpy as np

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel).astype(np.float32)
    peak = float(np.percentile(blackhat, 99.5))
    if peak <= 1e-6:
        return np.zeros_like(blackhat)
    mask = np.clip(blackhat / peak, 0.0, 1.0)
    # Slight blur gives ECC a smooth gradient field to descend.
    return cv2.GaussianBlur(mask, (0, 0), 2.0)


def _plausible(warp, w: int, h: int) -> bool:
    sx, sy = float(warp[0, 0]), float(warp[1, 1])
    shear = max(abs(float(warp[0, 1])), abs(float(warp[1, 0])))
    dx, dy = abs(float(warp[0, 2])), abs(float(warp[1, 2]))
    return (
        _SCALE_RANGE[0] <= sx <= _SCALE_RANGE[1]
        and _SCALE_RANGE[0] <= sy <= _SCALE_RANGE[1]
        and shear <= 0.15
        and dx <= _MAX_SHIFT_FRACTION * w
        and dy <= _MAX_SHIFT_FRACTION * h
    )
