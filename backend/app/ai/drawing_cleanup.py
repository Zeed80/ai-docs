"""Deterministic classical-CV cleanup pass for the studio's "Очистить чертёж"
(cleanup) operation — straight lines, artifact removal, crisp binarization.

Why classical CV and not more diffusion: diffusion is a generative model —
it cannot guarantee a line that should be straight actually comes out
straight (confirmed repeatedly this session: the ControlNet ablation and
live hatching patterns came out wavy/chaotic even with conditioning).
Diffusion IS good at photo-to-illustration style transfer (removing paper
texture, shadows, color) — that stays in the ComfyUI workflow. This module
runs classical, deterministic operations around it: once before diffusion
(denoise/deskew/contrast, so a poor-quality photo gives diffusion a better
starting point) and once after (binarize, remove speckle artifacts, snap
near-straight lines to mathematically straight) to enforce the geometric
precision diffusion cannot promise on its own.

`regularize_technical_drawing` deliberately does NOT attempt full-scene line
reconstruction (would risk mangling circles/arcs/hatching at odd angles) —
it only snaps segments whose Hough-fitted angle already sits close to a
canonical ЕСКД angle (0°/45°/90°/135°, which covers the overwhelming
majority of real contour/dimension/hatching lines) to exactly that angle,
straight. Everything else (curves, off-angle construction lines) is left as
the artifact-cleaned base produced without it.
"""

from __future__ import annotations

import io
import math

import structlog

logger = structlog.get_logger()

_CANONICAL_ANGLES_DEG = (0.0, 45.0, 90.0, 135.0)
_CANONICAL_TOLERANCE_DEG = 4.0
_MIN_SPECK_AREA_FRACTION = 0.00003  # relative to image area; below this = noise, not ink


def enhance_source_for_diffusion(image_bytes: bytes) -> bytes:
    """Pre-diffusion conditioning for a poor-quality photo/scan: perspective
    dewarp (phone photos of a bound album are never shot square-on) + deskew +
    denoise + contrast, kept as a natural (non-binarized) image so diffusion
    still has photographic context to work with. Best-effort: returns the
    original bytes unchanged if OpenCV isn't available or anything fails.

    NOTE for callers: the output's geometry differs from the input's whenever
    dewarp/deskew fires — anything computed against the source in pixel
    coordinates (OCR text boxes for text_preserve!) must be computed against
    THIS function's output, not the original photo."""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        img = _open_on_white(image_bytes)
        arr = np.asarray(img)

        # Perspective correction first: find the sheet's quad and warp it
        # square. Confirmed live: diffusion faithfully reproduces the photo's
        # perspective tilt (the result is a skewed sheet), and the vector
        # regularizer can only snap lines to ЕСКД angles if the sheet's own
        # verticals/horizontals actually run vertical/horizontal.
        dewarped = _dewarp_sheet(arr)
        if dewarped is not None:
            arr = _erase_binding_blocks(dewarped)

        # Denoise (median blur — cheap, preserves edges better than gaussian
        # for scan/JPEG speckle) so deskew's edge detection isn't thrown off
        # by sensor noise.
        arr = cv2.medianBlur(arr, 3)

        angle = _detect_skew_angle(arr)
        if abs(angle) > 0.5:
            arr = _rotate(arr, angle)

        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        arr = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2RGB)

        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        logger.debug("drawing_cleanup_no_cv2")
        return image_bytes
    except Exception as exc:  # noqa: BLE001 — best-effort, never block the pipeline
        logger.warning("enhance_source_failed", error=str(exc))
        return image_bytes


def regularize_technical_drawing(
    image_bytes: bytes, snap_lines: bool = False, vectorize: bool = True
) -> bytes:
    """Post-diffusion pass: binarize, strip speckle artifacts, then (default)
    rebuild every stroke via vector reconstruction — straight uniform lines
    snapped to ЕСКД angles, perfect circles/arcs, curves redrawn 1:1. See
    drawing_vectorize.py for the design and why it's safe to default on where
    the older ``snap_lines`` pass wasn't: it *replaces* strokes one-to-one
    from the skeleton instead of overpainting Hough fragments, and it
    self-verifies coverage against the original ink, declining (falling back
    to the plain binarized image) rather than shipping a corrupted redraw.
    Best-effort: returns the input unchanged if OpenCV is unavailable or
    anything fails.

    ``snap_lines`` (default off, superseded by ``vectorize``): the older
    canonical-angle line-straightening pass (see ``_snap_canonical_lines``).
    It drew corrected lines on top of the existing raster and needed three
    rounds of live-corruption fixes without reaching trustworthy — kept only
    as an opt-in escape hatch and for its regression tests.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        img = _open_on_white(image_bytes)
        arr = np.asarray(img)
        h, w = arr.shape[:2]

        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        # No pre-threshold median blur here: it erases genuine 1px strokes
        # entirely (confirmed on a real thin-line CAD export — most of the
        # geometry vanished), while the speckle it would have suppressed is
        # already removed component-wise by _remove_small_specks below.
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # `binary`: ink = 0 (black), background = 255 (white). Work with an
        # inverted mask (ink = 255) for morphology/contour conventions.
        ink = cv2.bitwise_not(binary)

        ink = _remove_small_specks(ink, w * h)
        ink = _close_small_gaps(ink)

        if vectorize:
            from app.ai.drawing_vectorize import redraw_ink

            canvas = redraw_ink(ink, _text_exclusion_boxes(image_bytes, w, h))
            if canvas is not None:
                buf = io.BytesIO()
                Image.fromarray(canvas).convert("RGB").save(buf, format="PNG")
                return buf.getvalue()
            # Declined (self-verification failed or not a line drawing) —
            # fall through to the plain binarized output below.

        if snap_lines:
            ink = _snap_canonical_lines(ink, w, h, _text_exclusion_boxes(image_bytes, w, h))

        out = np.full((h, w, 3), 255, dtype=np.uint8)
        out[ink > 0] = (0, 0, 0)

        buf = io.BytesIO()
        Image.fromarray(out).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        logger.debug("drawing_cleanup_no_cv2")
        return image_bytes
    except Exception as exc:  # noqa: BLE001
        logger.warning("regularize_drawing_failed", error=str(exc))
        return image_bytes


# ── Internal helpers ─────────────────────────────────────────────────────────


def _open_on_white(image_bytes: bytes):
    """Open as RGB with any alpha composited onto WHITE. A plain
    ``convert("RGB")`` renders transparent PNG background as black, which
    binarization then reads as a solid sheet of ink (confirmed on real
    transparent-background drawings: ink fraction came out 0.94-1.0)."""
    import io as _io

    from PIL import Image

    img = Image.open(_io.BytesIO(image_bytes))
    if "A" in img.getbands():
        img = img.convert("RGBA")
        base = Image.new("RGBA", img.size, (255, 255, 255, 255))
        base.alpha_composite(img)
        return base.convert("RGB")
    return img.convert("RGB")


def _dewarp_sheet(arr):
    """Find the sheet of paper in a phone photo and perspective-correct it to
    a straight-on view (classic document-scanner dewarp). Returns the warped
    RGB array, or None when there is no confident sheet quad — callers keep
    the image as-is then.

    Sheet detection is saturation-based: paper is near-achromatic and bright,
    while the desk/table around it (wood, cloth) is colored. That mask is far
    more reliable here than brightness alone — a light wooden desk passes an
    Otsu brightness threshold but fails the saturation test."""
    import cv2
    import numpy as np

    h, w = arr.shape[:2]
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    mask = ((hsv[..., 1] < 70) & (hsv[..., 2] > 120)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    sheet = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(sheet)
    # Too small = that's not the sheet; nearly the whole frame = already a
    # square-on scan, warping would only resample it for nothing.
    if area < 0.35 * w * h or area > 0.97 * w * h:
        return None
    # Convex hull first: a real album photo's sheet contour is never a clean
    # 4-gon — spiral-binding perforations nibble the edges and a sliver of
    # the page behind pokes out above the binding (confirmed live: raw
    # approxPolyDP came back 6-cornered/non-convex and dewarp never fired).
    # The hull bridges those, then an epsilon ladder simplifies to 4 corners.
    hull = cv2.convexHull(sheet)
    peri = cv2.arcLength(hull, True)
    quad = None
    for eps_frac in (0.02, 0.03, 0.05, 0.08):
        candidate = cv2.approxPolyDP(hull, eps_frac * peri, True)
        if len(candidate) == 4:
            quad = candidate
            break
    if quad is None or not cv2.isContourConvex(quad):
        return None

    pts = quad.reshape(4, 2).astype(np.float32)
    sums, diffs = pts.sum(axis=1), pts[:, 1] - pts[:, 0]
    tl, br = pts[np.argmin(sums)], pts[np.argmax(sums)]
    tr, bl = pts[np.argmin(diffs)], pts[np.argmax(diffs)]
    ordered = np.array([tl, tr, br, bl], dtype=np.float32)

    out_w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    out_h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if out_w < 200 or out_h < 200:
        return None
    target = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32
    )
    m = cv2.getPerspectiveTransform(ordered, target)
    warped = cv2.warpPerspective(
        arr, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
    )
    logger.info("dewarp_applied", out_w=out_w, out_h=out_h, area_fraction=round(area / (w * h), 2))
    return warped


def _erase_binding_blocks(arr):
    """Paint out spiral/comb-binding blocks along the top of a dewarped album
    photo. The sheet quad is a convex hull, so the binding (a row of solid
    dark rectangles) rides along at the top and survives diffusion as black
    blocks on the clean result (confirmed live).

    A row-based crop cannot work here: the binding sits out of the sheet's
    plane, so after the perspective warp it runs *diagonally* and overlaps
    the ГОСТ header table row-wise (confirmed live — the crop took the top
    of the table with it). Instead, detect the individual binding blocks by
    their unmistakable component signature — ≥5 similar SOLID dark rectangles
    in the top zone — and whiten exactly those. Table linework/text never
    matches: its components are thin strokes (fill ratio far below solid) or
    the table grid (one big low-fill component)."""
    import cv2
    import numpy as np

    h, w = arr.shape[:2]
    zone_h = int(h * 0.18)
    zone = arr[:zone_h]
    # "Not paper": dark OR saturated. Binding teeth come in both — black
    # comb spines and beige/yellow spiral coils (confirmed on real album
    # photos: the beige coils pass a darkness test untouched, then diffusion
    # renders them as solid black blocks anyway).
    hsv = cv2.cvtColor(zone, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(zone, cv2.COLOR_RGB2GRAY)
    not_paper = ((gray < 100) | (hsv[..., 1] > 80)).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(not_paper, connectivity=8)

    min_side, max_w, max_h = max(6, h // 120), w // 8, zone_h
    candidates = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if cw < min_side or ch < min_side or cw > max_w or ch > max_h:
            continue
        # Solidity: real binding teeth measure 0.40-0.45 on live photos
        # (specular highlights punch holes in them); linework/text sits at
        # 0.10-0.25. 0.35 splits the two with margin on both sides.
        if area / (cw * ch) < 0.35:
            continue
        candidates.append((x, y, cw, ch))
    if len(candidates) < 5:
        return arr

    # Require similar sizes (one binding = one комб of identical teeth).
    widths = sorted(c[2] for c in candidates)
    heights = sorted(c[3] for c in candidates)
    med_w, med_h = widths[len(widths) // 2], heights[len(heights) // 2]
    blocks = [
        (x, y, cw, ch)
        for x, y, cw, ch in candidates
        if 0.4 * med_w <= cw <= 2.5 * med_w and 0.4 * med_h <= ch <= 2.5 * med_h
    ]
    if len(blocks) < 5:
        return arr

    # Binding teeth always sit along one (possibly tilted) line at the sheet
    # edge. A dark table cell in the ГОСТ header can pass the size/solidity
    # tests (confirmed live: two header cells got whitened) — but it can't
    # also sit on the teeth's shared line. Keep only the collinear row.
    centers = np.array([(x + cw / 2, y + ch / 2) for x, y, cw, ch in blocks], dtype=np.float64)
    slope, intercept = np.polyfit(centers[:, 0], centers[:, 1], 1)
    residuals = np.abs(centers[:, 1] - (slope * centers[:, 0] + intercept))
    tolerance = max(15.0, 1.5 * med_h)
    blocks = [b for b, r in zip(blocks, residuals) if r <= tolerance]
    if len(blocks) < 5:
        return arr

    out = arr.copy()
    pad = 3
    for x, y, cw, ch in blocks:
        out[max(0, y - pad):min(zone_h, y + ch + pad), max(0, x - pad):min(w, x + cw + pad)] = 255
    logger.info("binding_blocks_erased", blocks=len(blocks))
    return out


def _text_exclusion_boxes(image_bytes: bytes, w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Regions any stroke-rewriting pass must keep away from. Dense text
    (title block, spec table) reads geometrically almost exactly like a
    cluster of short axis-aligned "lines" — confirmed live: without this
    exclusion, line-snapping turned real characters into black blobs. Text is
    already handled far more reliably elsewhere (OCR-anchored exact-pixel
    preservation, text_preserve.py) — reuse its detector here purely for its
    *locations*.

    OCR isn't exhaustive (confirmed live: it missed a stylised part-name word
    that then got smudged) — the ГОСТ title block itself (bottom 15% × right
    30%, same convention as drawing_preprocessor.py's _detect_title_block) is
    *always* dense text/table content, never real engineering geometry, so
    that whole corner is excluded unconditionally as a backstop."""
    text_boxes: list[tuple[int, int, int, int]] = []
    try:
        from app.ai.text_preserve import detect_text_regions

        text_boxes = [
            (r.x, r.y, r.x + r.w, r.y + r.h) for r in detect_text_regions(image_bytes)
        ]
    except Exception as exc:  # noqa: BLE001 — nice-to-have guard, not required
        logger.debug("regularize_text_exclusion_unavailable", error=str(exc))

    text_boxes.append((int(w * 0.70), int(h * 0.85), w, h))
    return text_boxes


def _detect_skew_angle(arr) -> float:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, rho=1, theta=np.pi / 180, threshold=100,
        minLineLength=arr.shape[1] // 4, maxLineGap=20,
    )
    if lines is None:
        return 0.0
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 != x1:
            a = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if abs(a) < 10:
                angles.append(a)
    if not angles:
        return 0.0
    angles.sort()
    return angles[len(angles) // 2]


def _rotate(arr, angle: float):
    import cv2

    h, w = arr.shape[:2]
    m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(
        arr, m, (w, h), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
    )


def _remove_small_specks(ink, image_area: int):
    """Drop connected components smaller than a noise-sized threshold —
    scan dust, JPEG speckle, diffusion halos — while keeping every real
    stroke (even short dashes in a штрихпунктирная line) intact."""
    import cv2

    min_area = max(4, int(image_area * _MIN_SPECK_AREA_FRACTION))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    cleaned = ink.copy()
    for i in range(1, n):  # label 0 = background
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            cleaned[labels == i] = 0
    return cleaned


def _close_small_gaps(ink):
    """Reconnect small breaks in a stroke caused by scan/JPEG artifacts
    without thickening real gaps (dash spacing, distinct nearby strokes)."""
    import cv2

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel)


def _line_signature(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float]:
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    if angle < 0:
        angle += 180.0
    rad = math.radians(angle)
    nx, ny = -math.sin(rad), math.cos(rad)
    offset = x1 * nx + y1 * ny
    return angle, offset


def _nearest_canonical_angle(angle_deg: float) -> float | None:
    for canon in _CANONICAL_ANGLES_DEG:
        diff = min(abs(angle_deg - canon), abs(angle_deg - canon - 180), abs(angle_deg - canon + 180))
        if diff <= _CANONICAL_TOLERANCE_DEG:
            return canon
    return None


_MAX_PLAUSIBLE_LINE_THICKNESS = 6  # px; ЕСКД strokes are hairline-to-contour, never this fat


def _probe_thickness_at(ink, cx: float, cy: float, angle_deg: float, max_probe: int) -> int:
    h, w = ink.shape[:2]
    rad = math.radians(angle_deg + 90)
    dx, dy = math.cos(rad), math.sin(rad)
    count = 0
    for sign in (1, -1):
        for step in range(1, max_probe + 1):
            x, y = int(round(cx + sign * step * dx)), int(round(cy + sign * step * dy))
            if 0 <= x < w and 0 <= y < h and ink[y, x] > 0:
                count += 1
            else:
                break
    return count


def _measure_stroke_width(
    ink, cx: float, cy: float, angle_deg: float, direction, half_len: float, max_probe: int = 12
) -> int | None:
    """Sample the ink mask perpendicular to the line, at several points along
    its length (not just the midpoint), and take the median. A single-point
    probe is fragile: if the midpoint happens to land on a text character or
    a dense table grid (title block / spec table — confirmed live: this is
    exactly where it went wrong on a real drawing) it reads as one solid fat
    blob and the redrawn line comes out absurdly thick. Returns None — "don't
    draw this one" — if even the median is implausibly thick for ЕСКД line
    conventions, rather than silently clobbering real content with a bar.
    """
    import numpy as np

    offsets = [0.0] if half_len < 1 else np.linspace(-half_len, half_len, 5)
    samples = [
        _probe_thickness_at(ink, cx + o * direction[0], cy + o * direction[1], angle_deg, max_probe)
        for o in offsets
    ]
    samples = [s for s in samples if s > 0] or [1]
    thickness = int(np.median(samples))
    if thickness > _MAX_PLAUSIBLE_LINE_THICKNESS:
        return None
    return max(1, thickness)


def _in_any_box(x: float, y: float, boxes: list[tuple[int, int, int, int]], pad: int = 3) -> bool:
    return any(x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad for x0, y0, x1, y1 in boxes)


def _snap_canonical_lines(ink, w: int, h: int, text_boxes: list[tuple[int, int, int, int]] | None = None):
    """Detect long straight-ish segments; for the ones already close to a
    canonical ЕСКД angle (0/45/90/135°), replace the local band with a
    mathematically straight, precisely-angled stroke of the same measured
    thickness. Segments at other angles (arcs, circles, off-angle
    construction lines) are left completely untouched.

    Merging is angle+offset bucketed AND gap-limited along the line's own
    axis — two segments only ever combine if they're genuinely close/
    overlapping *along the direction of travel*, never just because they
    happen to share a similar angle+offset somewhere else on the sheet. An
    earlier version matched on angle+offset alone and merged unrelated,
    far-apart segments into one absurd cross-sheet "line" that clobbered
    real content when redrawn — confirmed by direct visual inspection on a
    real test drawing before this was caught.
    """
    import cv2
    import numpy as np

    text_boxes = text_boxes or []
    diag = math.hypot(w, h)
    min_len = max(20, int(diag * 0.03))
    max_gap = max(10, int(diag * 0.01))
    lines = cv2.HoughLinesP(
        ink, rho=1, theta=np.pi / 360, threshold=30,
        minLineLength=min_len, maxLineGap=4,
    )
    if lines is None:
        return ink

    # Bucket by (canonical angle, offset rounded to a small grid) — only
    # near-coincident parallel segments ever land in the same bucket, so two
    # genuinely distinct parallel lines a few px apart never merge.
    buckets: dict[tuple[float, int], list[tuple[float, float, float, float]]] = {}
    for line in lines:
        x1, y1, x2, y2 = (float(v) for v in line[0])
        if text_boxes and _in_any_box((x1 + x2) / 2, (y1 + y2) / 2, text_boxes):
            continue
        angle, offset = _line_signature(x1, y1, x2, y2)
        canon = _nearest_canonical_angle(angle)
        if canon is None:
            continue
        buckets.setdefault((canon, round(offset / 2.0)), []).append((x1, y1, x2, y2))

    out = ink.copy()
    for (canon, _bucket_key), segs in buckets.items():
        rad = math.radians(canon)
        direction = np.array([math.cos(rad), math.sin(rad)])

        intervals = []
        for x1, y1, x2, y2 in segs:
            p1v, p2v = np.array([x1, y1]), np.array([x2, y2])
            t1, t2 = float(p1v @ direction), float(p2v @ direction)
            lo, hi = (t1, t2) if t1 <= t2 else (t2, t1)
            intervals.append([lo, hi, [p1v, p2v]])
        intervals.sort(key=lambda iv: iv[0])

        merged: list[list] = []
        for lo, hi, pts in intervals:
            if merged and lo <= merged[-1][1] + max_gap:
                merged[-1][1] = max(merged[-1][1], hi)
                merged[-1][2].extend(pts)
            else:
                merged.append([lo, hi, list(pts)])

        for lo, hi, pts in merged:
            if hi - lo < min_len:
                continue
            pts_arr = np.array(pts, dtype=np.float32)
            mean = pts_arr.mean(axis=0)
            t_mean = float(mean @ direction)
            p1 = mean + direction * (lo - t_mean)
            p2 = mean + direction * (hi - t_mean)
            thickness = _measure_stroke_width(
                ink, float(mean[0]), float(mean[1]), canon, direction, (hi - lo) / 2
            )
            if thickness is None:
                # Median probe came back implausibly thick — almost always
                # means this "line" actually sits over dense text/table
                # content (title block, spec sheet), not a real stroke.
                # Leave that area exactly as the artifact-cleaned base had it.
                continue
            cv2.line(
                out, (int(round(p1[0])), int(round(p1[1]))), (int(round(p2[0])), int(round(p2[1]))),
                color=255, thickness=thickness, lineType=cv2.LINE_AA,
            )
    return out
