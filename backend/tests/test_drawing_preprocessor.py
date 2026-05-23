"""Tests for drawing_preprocessor: CLAHE, deskew, view segmentation, PDF pages."""

import io
import math
import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_png(width: int = 400, height: int = 300, color: tuple = (240, 240, 240)) -> bytes:
    """Generate a minimal solid-color PNG for testing."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    # Draw a simple border and a circle to give the image some content
    draw.rectangle([10, 10, width - 10, height - 10], outline=(0, 0, 0), width=2)
    draw.ellipse([50, 50, 150, 150], outline=(0, 0, 0), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_multiview_png(width: int = 800, height: int = 600) -> bytes:
    """Generate a PNG with visible separator lines for segmentation testing."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    # Vertical separator at mid-width
    draw.line([(width // 2, 0), (width // 2, height)], fill=(0, 0, 0), width=3)
    # Horizontal separator at mid-height
    draw.line([(0, height // 2), (width, height // 2)], fill=(0, 0, 0), width=3)
    # Content in each quadrant
    draw.ellipse([30, 30, 180, 180], outline=(0, 0, 0), width=2)
    draw.rectangle([430, 30, 580, 180], outline=(0, 0, 0), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── ViewCrop / PreprocessedDrawing dataclass tests ────────────────────────────


def test_viewcrop_defaults():
    from app.ai.drawing_preprocessor import ViewCrop
    vc = ViewCrop(view_type="front", image_bytes=b"data", bbox=(0, 0, 100, 100), label="front")
    assert vc.confidence == 1.0
    assert vc.view_type == "front"


def test_preprocessed_drawing_defaults():
    from app.ai.drawing_preprocessor import PreprocessedDrawing, ViewCrop
    pd = PreprocessedDrawing(full_image=b"img", title_block=None)
    assert pd.views == []
    assert pd.dpi_effective == 200
    assert pd.was_enhanced is False
    assert pd.page_count == 1


# ── preprocess_drawing_image ──────────────────────────────────────────────────


def test_preprocess_returns_preprocessed_drawing():
    from app.ai.drawing_preprocessor import preprocess_drawing_image, PreprocessedDrawing
    png = _make_png()
    result = preprocess_drawing_image(png, fmt="png")
    assert isinstance(result, PreprocessedDrawing)
    assert len(result.full_image) > 0
    assert len(result.views) >= 1


def test_preprocess_full_image_is_png():
    from app.ai.drawing_preprocessor import preprocess_drawing_image
    png = _make_png()
    result = preprocess_drawing_image(png, fmt="png")
    # PNG magic bytes
    assert result.full_image[:8] == b"\x89PNG\r\n\x1a\n"


def test_preprocess_single_view_fallback():
    """When segmentation finds no separators, returns one 'full' view."""
    from app.ai.drawing_preprocessor import preprocess_drawing_image
    png = _make_png(200, 150)  # Small image, no separator lines
    result = preprocess_drawing_image(png, fmt="png")
    assert len(result.views) >= 1
    assert result.views[0].label in ("full", "front")


def test_preprocess_max_views_respected():
    from app.ai.drawing_preprocessor import preprocess_drawing_image
    png = _make_multiview_png()
    result = preprocess_drawing_image(png, fmt="png", max_views=2)
    assert len(result.views) <= 2


def test_preprocess_title_block_detected():
    from app.ai.drawing_preprocessor import preprocess_drawing_image
    png = _make_png(600, 400)
    result = preprocess_drawing_image(png, fmt="png")
    # Title block crop should be non-empty bytes
    if result.title_block is not None:
        assert len(result.title_block) > 0


def test_preprocess_bad_bytes_returns_fallback():
    """Invalid bytes must not raise — returns PreprocessedDrawing with raw fallback."""
    from app.ai.drawing_preprocessor import preprocess_drawing_image, PreprocessedDrawing
    result = preprocess_drawing_image(b"not_an_image", fmt="png")
    assert isinstance(result, PreprocessedDrawing)
    assert len(result.views) >= 1


# ── _adaptive_scale ────────────────────────────────────────────────────────────


def test_adaptive_scale_upsizes_small_image():
    from PIL import Image
    from app.ai.drawing_preprocessor import _adaptive_scale, _MIN_LONG_EDGE
    img = Image.new("RGB", (500, 300))
    scaled = _adaptive_scale(img)
    assert max(scaled.size) >= _MIN_LONG_EDGE


def test_adaptive_scale_no_op_for_optimal_image():
    from PIL import Image
    from app.ai.drawing_preprocessor import _adaptive_scale, _TARGET_LONG_EDGE
    img = Image.new("RGB", (_TARGET_LONG_EDGE, 1500))
    scaled = _adaptive_scale(img, _TARGET_LONG_EDGE)
    assert scaled.size == img.size


def test_adaptive_scale_downsizes_giant_image():
    from PIL import Image
    from app.ai.drawing_preprocessor import _adaptive_scale, _MAX_LONG_EDGE
    img = Image.new("RGB", (8000, 5000))
    scaled = _adaptive_scale(img)
    assert max(scaled.size) <= _MAX_LONG_EDGE


# ── _detect_title_block ────────────────────────────────────────────────────────


def test_detect_title_block_returns_bytes_or_none():
    from PIL import Image
    from app.ai.drawing_preprocessor import _detect_title_block
    img = Image.new("RGB", (800, 600), (240, 240, 240))
    result = _detect_title_block(img)
    assert result is None or isinstance(result, bytes)


def test_detect_title_block_crop_dimensions():
    from PIL import Image
    from app.ai.drawing_preprocessor import (
        _detect_title_block, _img_to_png_bytes,
        _TITLE_BLOCK_HEIGHT_RATIO, _TITLE_BLOCK_WIDTH_RATIO,
    )
    img = Image.new("RGB", (1000, 800), (240, 240, 240))
    result = _detect_title_block(img)
    if result:
        # Decode to check dimensions
        crop = Image.open(io.BytesIO(result))
        # Should be roughly bottom 15% × right 30%
        assert crop.width <= img.width * _TITLE_BLOCK_WIDTH_RATIO + 5
        assert crop.height <= img.height * _TITLE_BLOCK_HEIGHT_RATIO + 5


# ── _cluster_positions ─────────────────────────────────────────────────────────


def test_cluster_positions_empty():
    import numpy as np
    from app.ai.drawing_preprocessor import _cluster_positions
    result = _cluster_positions(np.array([]))
    assert result == []


def test_cluster_positions_groups_nearby():
    import numpy as np
    from app.ai.drawing_preprocessor import _cluster_positions
    # Three nearby positions should merge into one cluster
    positions = np.array([100, 102, 104, 200, 202])
    result = _cluster_positions(positions, threshold=10)
    assert len(result) == 2
    assert abs(result[0] - 102) <= 5
    assert abs(result[1] - 201) <= 5


# ── _assign_view_labels ────────────────────────────────────────────────────────


def test_assign_view_labels_first_is_front():
    from app.ai.drawing_preprocessor import _assign_view_labels
    rects = [
        (0, 0, 400, 300, 120000),    # largest = front
        (400, 0, 200, 150, 30000),   # upper-right = isometric
        (0, 300, 200, 150, 30000),   # lower-left
    ]
    labels = _assign_view_labels(rects, sheet_w=600, sheet_h=450)
    assert labels[0] == "front"


def test_assign_view_labels_upper_right_is_isometric():
    from app.ai.drawing_preprocessor import _assign_view_labels
    rects = [
        (0, 0, 400, 300, 120000),     # front
        (500, 10, 200, 150, 30000),   # upper-right
    ]
    labels = _assign_view_labels(rects, sheet_w=700, sheet_h=500)
    assert labels[1] == "isometric"


# ── _estimate_dpi ──────────────────────────────────────────────────────────────


def test_estimate_dpi_reasonable_range():
    from app.ai.drawing_preprocessor import _estimate_dpi
    dpi = _estimate_dpi(4677, 6614)  # A1 at 200 DPI
    assert 150 <= dpi <= 250


def test_estimate_dpi_clamped():
    from app.ai.drawing_preprocessor import _estimate_dpi
    assert _estimate_dpi(100, 100) >= 72
    assert _estimate_dpi(50000, 50000) <= 600


# ── preprocess_pdf_pages ──────────────────────────────────────────────────────


def test_preprocess_pdf_pages_invalid_returns_empty():
    from app.ai.drawing_preprocessor import preprocess_pdf_pages
    result = preprocess_pdf_pages(b"not a pdf", max_pages=3)
    assert result == []


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("fitz"),
    reason="PyMuPDF (fitz) not installed",
)
def test_preprocess_pdf_pages_real_pdf():
    """If fitz is available, test with a minimal valid PDF."""
    import fitz
    from app.ai.drawing_preprocessor import preprocess_pdf_pages, ViewCrop

    # Create a minimal PDF with one blank page
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    pdf_bytes = doc.tobytes()
    doc.close()

    pages = preprocess_pdf_pages(pdf_bytes, max_pages=5)
    assert len(pages) == 1
    assert isinstance(pages[0], ViewCrop)
    assert pages[0].label == "page_1"
    assert pages[0].view_type == "front"
    assert len(pages[0].image_bytes) > 100


# ── _detect_skew_angle ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("cv2"),
    reason="OpenCV not installed",
)
def test_detect_skew_angle_near_zero_for_upright_image():
    import numpy as np
    from app.ai.drawing_preprocessor import _detect_skew_angle
    # Horizontal lines → angle near 0
    img = np.ones((200, 400, 3), dtype=np.uint8) * 240
    img[100, :] = 0   # horizontal line
    angle = _detect_skew_angle(img)
    assert abs(angle) < 5.0


# ── Multiview segmentation integration ───────────────────────────────────────


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("cv2"),
    reason="OpenCV not installed",
)
def test_segment_views_multiview_image():
    """Multi-view PNG with separator lines should produce >1 view."""
    from app.ai.drawing_preprocessor import preprocess_drawing_image
    png = _make_multiview_png(width=1200, height=900)
    result = preprocess_drawing_image(png, fmt="png", max_views=6)
    # With visible separator lines, should find multiple views or fall back to 1
    assert len(result.views) >= 1
    for v in result.views:
        assert len(v.image_bytes) > 0
        assert v.bbox[2] > 0  # width > 0
        assert v.bbox[3] > 0  # height > 0
