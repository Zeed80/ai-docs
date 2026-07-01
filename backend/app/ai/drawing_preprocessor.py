"""Drawing pre-processing pipeline.

Prepares technical drawing images for VLM analysis:
- Adaptive resolution scaling (2048-4096px long edge)
- CLAHE contrast enhancement for scanned drawings
- Deskewing for tilted scans
- Title block detection (GOST bottom-right corner)
- View segmentation: front/side/top/section crops

OpenCV is required for CLAHE, deskew, and view segmentation.
If OpenCV is unavailable, only basic PIL scaling is applied.
"""

from __future__ import annotations

import io
import math
import structlog
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger()

# Target long edge for VLM input (Qwen3-VL optimal is 2048-4096px)
_TARGET_LONG_EDGE = 3072
_MIN_LONG_EDGE = 2048
_MAX_LONG_EDGE = 4096

# GOST title block location: bottom 15% × right 30%
_TITLE_BLOCK_HEIGHT_RATIO = 0.15
_TITLE_BLOCK_WIDTH_RATIO = 0.30

# Minimum fraction of image dimension for a line to be considered a view separator
_SEPARATOR_LINE_MIN_FRACTION = 0.65


@dataclass
class ViewCrop:
    view_type: str          # "front"|"side"|"top"|"section"|"isometric"|"detail"|"page_N"
    image_bytes: bytes
    bbox: tuple[int, int, int, int]  # (x, y, w, h) in pixels on the full sheet
    label: str              # "front", "A-A", "page_2", etc.
    confidence: float = 1.0


@dataclass
class PreprocessedDrawing:
    full_image: bytes           # Enhanced full drawing image (PNG)
    title_block: bytes | None   # Cropped title block / stamp (ГОСТ: bottom-right corner)
    views: list[ViewCrop] = field(default_factory=list)
    dpi_effective: int = 200
    was_enhanced: bool = False  # Whether CLAHE/deskew was applied
    page_count: int = 1         # For multi-page PDFs


def preprocess_drawing_image(
    raw_bytes: bytes,
    fmt: str = "png",
    max_views: int = 6,
) -> PreprocessedDrawing:
    """Main entry point: preprocess a drawing image for VLM analysis.

    Args:
        raw_bytes: Raw PNG/JPEG/TIFF image bytes
        fmt: Source format hint ("png", "jpg", "dxf_raster", "pdf_raster", etc.)
        max_views: Maximum number of view crops to extract

    Returns:
        PreprocessedDrawing with enhanced full image, title block crop, and view segments
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg

        # Step 1: Adaptive scaling
        img = _adaptive_scale(img, _TARGET_LONG_EDGE)

        was_enhanced = False

        # Step 2: CLAHE + deskew (only if OpenCV available and image looks like a scan)
        try:
            import cv2
            import numpy as np

            img_np = np.array(img)
            enhanced, skew_corrected = _clahe_and_deskew(img_np)
            was_enhanced = True
            img = Image.fromarray(enhanced)
        except ImportError:
            logger.debug("drawing_preprocessor_no_cv2")
        except Exception as exc:
            logger.warning("drawing_preprocessor_enhance_failed", error=str(exc))

        full_bytes = _img_to_png_bytes(img)

        # Step 3: Title block detection (bottom-right GOST position)
        title_block_bytes = _detect_title_block(img)

        # Step 4: View segmentation
        views: list[ViewCrop] = []
        try:
            views = _segment_views_opencv(img, max_views=max_views)
        except ImportError:
            logger.debug("drawing_preprocessor_no_cv2_segmentation")
        except Exception as exc:
            logger.warning("drawing_preprocessor_segmentation_failed", error=str(exc))

        # If segmentation failed or found nothing, use full image as single view
        if not views:
            views = [ViewCrop(
                view_type="front",
                image_bytes=full_bytes,
                bbox=(0, 0, img.width, img.height),
                label="full",
                confidence=1.0,
            )]

        return PreprocessedDrawing(
            full_image=full_bytes,
            title_block=title_block_bytes,
            views=views[:max_views],
            dpi_effective=_estimate_dpi(img.width, img.height),
            was_enhanced=was_enhanced,
        )

    except Exception as exc:
        logger.error("drawing_preprocessor_failed", error=str(exc))
        # Minimal fallback: return raw bytes as a single view
        return PreprocessedDrawing(
            full_image=raw_bytes,
            title_block=None,
            views=[ViewCrop(
                view_type="front",
                image_bytes=raw_bytes,
                bbox=(0, 0, 0, 0),
                label="full",
                confidence=0.5,
            )],
        )


def preprocess_pdf_pages(
    pdf_bytes: bytes,
    max_pages: int = 10,
    dpi: int = 200,
) -> list[ViewCrop]:
    """Extract and preprocess all pages from a PDF drawing.

    Returns a list of ViewCrop, one per page, preprocessed for VLM.
    Each page is treated as an independent view.
    """
    pages: list[ViewCrop] = []
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n_pages = min(doc.page_count, max_pages)

        for page_idx in range(n_pages):
            page = doc[page_idx]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")

            # Preprocess each page
            preprocessed = preprocess_drawing_image(png_bytes, fmt="pdf_raster", max_views=1)
            enhanced = preprocessed.full_image

            label = "page_1" if n_pages == 1 else f"page_{page_idx + 1}"
            view_type = "front" if page_idx == 0 else f"page_{page_idx + 1}"

            pages.append(ViewCrop(
                view_type=view_type,
                image_bytes=enhanced,
                bbox=(0, 0, 0, 0),
                label=label,
                confidence=1.0,
            ))

        doc.close()
        logger.info("pdf_pages_preprocessed", count=len(pages))

    except Exception as exc:
        logger.error("pdf_pages_preprocess_failed", error=str(exc))

    return pages


# ── Internal helpers ───────────────────────────────────────────────────────────


def _adaptive_scale(img: Any, target_long_edge: int = _TARGET_LONG_EDGE) -> Any:
    """Scale image so the long edge is between MIN and MAX target pixels."""
    from PIL import Image as PILImage

    w, h = img.size
    long_edge = max(w, h)

    if _MIN_LONG_EDGE <= long_edge <= _MAX_LONG_EDGE:
        return img  # Already in optimal range

    ratio = target_long_edge / long_edge
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))

    resample = PILImage.LANCZOS if hasattr(PILImage, "LANCZOS") else PILImage.ANTIALIAS
    return img.resize((new_w, new_h), resample)


def _clahe_and_deskew(img_np: Any) -> tuple[Any, bool]:
    """Apply CLAHE enhancement and deskew correction using OpenCV.

    Returns (enhanced_np_array, was_deskewed).
    """
    import cv2
    import numpy as np

    # Convert to LAB for CLAHE on L channel only (preserves color info)
    if img_np.ndim == 3 and img_np.shape[2] == 3:
        lab = cv2.cvtColor(img_np, cv2.COLOR_RGB2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)

        # Apply CLAHE to L channel
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_enhanced = clahe.apply(l_channel)

        lab_enhanced = cv2.merge([l_enhanced, a_channel, b_channel])
        img_np = cv2.cvtColor(lab_enhanced, cv2.COLOR_LAB2RGB)
    else:
        # Grayscale — apply CLAHE directly
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        if img_np.ndim == 3:
            img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        img_np = clahe.apply(img_np)
        img_np = cv2.cvtColor(img_np, cv2.COLOR_GRAY2RGB)

    # Deskew: detect dominant angle via Hough lines
    skew_angle = _detect_skew_angle(img_np)
    was_deskewed = False
    if abs(skew_angle) > 0.5:
        img_np = _rotate_image(img_np, skew_angle)
        was_deskewed = True

    return img_np, was_deskewed


def _detect_skew_angle(img_np: Any) -> float:
    """Detect skew angle of a drawing using Hough line transform.

    Returns angle in degrees (positive = clockwise tilt).
    """
    import cv2
    import numpy as np

    try:
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY) if img_np.ndim == 3 else img_np
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180,
            threshold=100,
            minLineLength=img_np.shape[1] // 4,
            maxLineGap=20,
        )
        if lines is None:
            return 0.0

        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 != x1:
                angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
                # Only consider near-horizontal lines (±10°)
                if abs(angle) < 10:
                    angles.append(angle)

        if not angles:
            return 0.0

        # Use median angle
        angles.sort()
        median_angle = angles[len(angles) // 2]
        return median_angle

    except Exception:
        return 0.0


def _rotate_image(img_np: Any, angle: float) -> Any:
    """Rotate image by angle degrees around center."""
    import cv2

    h, w = img_np.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        img_np, M, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return rotated


def _detect_title_block(img: Any) -> bytes | None:
    """Detect and crop the GOST title block (основная надпись).

    Per GOST, the title block is in the bottom-right corner of the sheet.
    Returns cropped PNG bytes, or None if detection fails.
    """
    try:
        w, h = img.size
        # Bottom 15% × right 30%
        x = int(w * (1 - _TITLE_BLOCK_WIDTH_RATIO))
        y = int(h * (1 - _TITLE_BLOCK_HEIGHT_RATIO))
        crop = img.crop((x, y, w, h))
        return _img_to_png_bytes(crop)
    except Exception as exc:
        logger.warning("title_block_detection_failed", error=str(exc))
        return None


def _segment_views_opencv(img: Any, max_views: int = 6) -> list[ViewCrop]:
    """Segment a multi-view drawing into individual view crops using OpenCV.

    Strategy: find long horizontal and vertical lines that span >65% of the
    image width/height — these are likely view separator borders. Use them
    to partition the sheet into rectangular regions.

    Returns list of ViewCrop sorted by area (largest first = primary view).
    """
    import cv2
    import numpy as np

    img_np = np.array(img)
    if img_np.ndim == 3:
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_np

    h, w = gray.shape[:2]

    # Threshold to binary: drawing lines are dark on white background
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    # Find long lines using morphological operations
    # Horizontal lines: elements spanning >65% of width
    h_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (int(w * _SEPARATOR_LINE_MIN_FRACTION), 1)
    )
    h_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

    # Vertical lines: elements spanning >65% of height
    v_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, int(h * _SEPARATOR_LINE_MIN_FRACTION))
    )
    v_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    # Find y-coordinates of horizontal separators
    h_rows = np.where(h_lines_mask.sum(axis=1) > w * 0.5)[0]
    v_cols = np.where(v_lines_mask.sum(axis=0) > h * 0.5)[0]

    # Cluster nearby rows/cols to get unique separator positions
    h_separators = _cluster_positions(h_rows, threshold=20)
    v_separators = _cluster_positions(v_cols, threshold=20)

    # Build partition grid
    y_cuts = [0] + h_separators + [h]
    x_cuts = [0] + v_separators + [w]

    if len(y_cuts) <= 2 and len(x_cuts) <= 2:
        # No separators found — single view
        return []

    # Generate rectangles from the grid
    rects: list[tuple[int, int, int, int, int]] = []  # (x, y, rw, rh, area)
    for yi in range(len(y_cuts) - 1):
        for xi in range(len(x_cuts) - 1):
            rx = x_cuts[xi]
            ry = y_cuts[yi]
            rw = x_cuts[xi + 1] - rx
            rh = y_cuts[yi + 1] - ry
            area = rw * rh
            # Skip tiny regions (< 3% of total area)
            if area > w * h * 0.03:
                rects.append((rx, ry, rw, rh, area))

    if not rects:
        return []

    # Sort by area descending — largest = primary/front view
    rects.sort(key=lambda r: r[4], reverse=True)

    views: list[ViewCrop] = []
    view_labels = _assign_view_labels(rects, w, h)

    for idx, ((rx, ry, rw, rh, area), label) in enumerate(zip(rects[:max_views], view_labels)):
        crop = img.crop((rx, ry, rx + rw, ry + rh))
        crop_bytes = _img_to_png_bytes(crop)
        view_type = _label_to_type(label)
        views.append(ViewCrop(
            view_type=view_type,
            image_bytes=crop_bytes,
            bbox=(rx, ry, rw, rh),
            label=label,
            confidence=0.8 if idx == 0 else 0.7,
        ))

    logger.info(
        "drawing_views_segmented",
        count=len(views),
        h_separators=len(h_separators),
        v_separators=len(v_separators),
    )
    return views


def _cluster_positions(positions: Any, threshold: int = 20) -> list[int]:
    """Cluster nearby line positions and return median of each cluster."""
    if not len(positions):
        return []

    clusters: list[list[int]] = []
    current: list[int] = [int(positions[0])]

    for pos in positions[1:]:
        pos = int(pos)
        if pos - current[-1] < threshold:
            current.append(pos)
        else:
            clusters.append(current)
            current = [pos]
    clusters.append(current)

    return [int(sum(c) / len(c)) for c in clusters]


def _assign_view_labels(
    rects: list[tuple[int, int, int, int, int]],
    sheet_w: int,
    sheet_h: int,
) -> list[str]:
    """Assign human-readable labels to view rectangles based on position."""
    labels = []
    for idx, (rx, ry, rw, rh, area) in enumerate(rects):
        cx = rx + rw / 2
        cy = ry + rh / 2

        if idx == 0:
            label = "front"  # Largest = front view
        elif cy < sheet_h * 0.5 and cx > sheet_w * 0.5:
            label = "isometric"  # Upper right — often isometric/3D
        elif cy < sheet_h * 0.5:
            label = "top"    # Upper half
        elif cx > sheet_w * 0.6:
            label = "side"   # Right half
        else:
            label = f"view_{idx + 1}"

        labels.append(label)

    return labels


def _label_to_type(label: str) -> str:
    """Map label string to canonical view type."""
    _map = {
        "front": "front",
        "side": "side",
        "top": "top",
        "isometric": "isometric",
    }
    return _map.get(label, "detail")


def _img_to_png_bytes(img: Any) -> bytes:
    """Convert PIL Image to PNG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def canny_edge_map(image_bytes: bytes, low: int = 50, high: int = 150) -> bytes:
    """Canny edge map (PNG bytes) for a ControlNet conditioning image.

    Preprocessing happens here (backend), not as a ComfyUI custom node — the
    node pack that would do this in-graph (comfyui_controlnet_aux) isn't
    guaranteed to be installed on the ComfyUI host, while cv2 already is (used
    above for deskew). The edge map is uploaded as a plain image and fed into
    ControlNetApplyAdvanced's ``image`` input.
    """
    import cv2
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, low, high, apertureSize=3)
    # ControlNet conditioning images are typically RGB (white edges on black).
    edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
    return _img_to_png_bytes(Image.fromarray(edges_rgb))


def _estimate_dpi(width: int, height: int) -> int:
    """Estimate effective DPI from pixel dimensions (assuming A1 sheet ~594×841mm)."""
    # A1 sheet is 594mm × 841mm
    # At 200 DPI: 4677 × 6614 pixels
    # Use longest dimension to estimate
    long_px = max(width, height)
    long_mm = 841  # A1 longest side in mm
    dpi = int(long_px / (long_mm / 25.4))
    return max(72, min(600, dpi))
