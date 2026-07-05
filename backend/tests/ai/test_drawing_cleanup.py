"""Tests for the classical-CV cleanup/regularization pass (studio "Очистить
чертёж"). Uses small synthetic PIL-drawn images (offline, deterministic).
"""

from __future__ import annotations

import io

import numpy as np
import pytest

pytest.importorskip("cv2")

from PIL import Image, ImageDraw  # noqa: E402

from app.ai.drawing_cleanup import (  # noqa: E402
    _in_any_box,
    _snap_canonical_lines,
    enhance_source_for_diffusion,
    regularize_technical_drawing,
)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ink_mask(png_bytes: bytes):
    """Binary ink mask (255 = ink) matching regularize_technical_drawing's
    own convention, for measuring the result."""
    import cv2

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.bitwise_not(binary)


def test_enhance_source_for_diffusion_returns_valid_same_size_image():
    img = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(img).rectangle([50, 50, 250, 150], outline="black", width=2)
    out = enhance_source_for_diffusion(_png_bytes(img))
    out_img = Image.open(io.BytesIO(out))
    assert out_img.size == (300, 200)


def test_regularize_technical_drawing_removes_isolated_speck_noise():
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([50, 50, 350, 250], outline="black", width=2)
    # Scatter isolated 1-2px noise dots (scan dust) far from any real ink.
    rng = np.random.default_rng(0)
    for _ in range(40):
        x, y = rng.integers(100, 300), rng.integers(100, 200)
        d.point((int(x), int(y)), fill="black")

    out = regularize_technical_drawing(_png_bytes(img))
    ink = _ink_mask(out)
    # The rectangle survives; the scattered single-pixel dust should not —
    # check no ink exists far from the rectangle's own border.
    interior = ink[110:190, 110:290]
    assert interior.sum() == 0, "isolated speck noise should have been removed"


def test_regularize_technical_drawing_defaults_to_no_line_snapping():
    """Line-snapping needed three rounds of live-testing fixes before it
    stopped introducing new corruption (see _snap_canonical_lines' own
    docstring) — it must stay opt-in, not silently on for every cleanup run.
    (Vector reconstruction, its replacement, defaults on — but with
    ``vectorize=False`` the older pass must still not sneak in.)"""
    img = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(img).ellipse([50, 50, 250, 250], outline="black", width=3)
    default_out = regularize_technical_drawing(_png_bytes(img), vectorize=False)
    explicit_off_out = regularize_technical_drawing(
        _png_bytes(img), snap_lines=False, vectorize=False
    )
    assert default_out == explicit_off_out


def test_regularize_technical_drawing_preserves_a_circle_with_line_snapping_on():
    img = Image.new("RGB", (300, 300), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([50, 50, 250, 250], outline="black", width=3)

    out = regularize_technical_drawing(_png_bytes(img), snap_lines=True, vectorize=False)
    ink = _ink_mask(out)
    # A circle isn't a canonical-angle straight line — it must survive
    # roughly intact (not collapsed to a single bar, not erased). Compare
    # ink pixel count within a generous tolerance of the original stroke.
    original_ink = _ink_mask(_png_bytes(img))
    ratio = ink.sum() / max(1, original_ink.sum())
    assert 0.5 < ratio < 2.0, f"circle ink changed too much (ratio={ratio:.2f})"


def test_regularize_technical_drawing_keeps_title_block_text_readable_with_line_snapping_on():
    """Dense small text in the ГОСТ title block corner (bottom-right) reads
    geometrically like short axis-aligned "lines" — regression test for a
    real corruption found live: without the title-block exclusion, this
    became a solid black blob instead of staying separate character-like
    marks."""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([50, 50, 550, 350], outline="black", width=2)
    # Simulate dense small text strokes in the bottom-right corner (title
    # block position per drawing_preprocessor.py's own convention).
    rng = np.random.default_rng(1)
    for _ in range(60):
        x = int(rng.integers(430, 590))
        y = int(rng.integers(330, 395))
        d.line([(x, y), (x + 3, y + 2)], fill="black", width=1)

    out = regularize_technical_drawing(_png_bytes(img), snap_lines=True, vectorize=False)
    ink = _ink_mask(out)
    title_block = ink[330:395, 430:590]
    # A single corrupted blob would fill almost the entire region; scattered
    # short strokes leave most of it background (white).
    fill_ratio = title_block.sum() / (255.0 * title_block.size)
    assert fill_ratio < 0.5, f"title block area looks like a solid blob (fill={fill_ratio:.2f})"


def test_snap_canonical_lines_does_not_bridge_unrelated_far_apart_segments():
    """Regression test for a real corruption found live: two distinct
    horizontal lines at the same height but far apart (with unrelated
    content between them) were merged into one giant bar spanning the gap,
    because the original clustering matched purely on angle+offset with no
    contiguity check along the line's own axis."""
    import cv2

    w, h = 500, 100
    ink = np.zeros((h, w), dtype=np.uint8)
    # Two short, unrelated horizontal strokes at the same y, far apart.
    cv2.line(ink, (10, 50), (60, 50), color=255, thickness=2)
    cv2.line(ink, (440, 50), (490, 50), color=255, thickness=2)

    out = _snap_canonical_lines(ink, w, h, text_boxes=[])

    # The middle of the sheet (far from both real strokes) must stay empty —
    # a bridged "line" would fill it.
    middle_band = out[45:55, 150:350]
    assert middle_band.sum() == 0, "unrelated far-apart segments were bridged into one line"


def test_in_any_box_respects_padding():
    boxes = [(10, 10, 20, 20)]
    assert _in_any_box(15, 15, boxes) is True
    assert _in_any_box(11, 11, boxes) is True  # inside
    assert _in_any_box(100, 100, boxes) is False
