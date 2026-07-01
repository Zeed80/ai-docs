"""Tests for the OCR-anchored text-preservation hybrid (diffusion edit/cleanup).

Uses small synthetic PIL-drawn images (not the external real-drawing test set
in test-drawings/, which is gitignored and network-fetched — see
test-drawings/SOURCES.md) so these run offline and deterministically in CI.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("pytesseract")
pytest.importorskip("cv2")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.ai.text_preserve import (  # noqa: E402
    _looks_like_text,
    composite_text_regions,
    detect_text_regions,
    text_fidelity_score,
)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _drawing_with_text(text: str = "45h6", size=(400, 300)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    # A couple of geometry lines (simulating a drawing) + one text label.
    d.rectangle([50, 50, 350, 250], outline="black", width=3)
    d.line([50, 150, 350, 150], fill="black", width=1)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    d.text((150, 20), text, fill="black", font=font)
    return img


def test_detect_text_regions_finds_the_label():
    img = _drawing_with_text("45h6")
    regions = detect_text_regions(_png_bytes(img))
    assert regions, "expected at least one detected text region"
    # The label was drawn near (150, 20) — some region should overlap that area.
    assert any(r.x < 250 and r.y < 60 for r in regions)


def test_detect_text_regions_empty_for_blank_image():
    img = Image.new("RGB", (200, 200), "white")
    regions = detect_text_regions(_png_bytes(img))
    assert regions == []


def test_looks_like_text_accepts_real_text_crop():
    img = _drawing_with_text("h6")
    crop = img.crop((140, 15, 260, 55))
    assert _looks_like_text(crop) is True


def test_looks_like_text_rejects_solid_dark_crop():
    crop = Image.new("RGB", (60, 60), (10, 10, 10))
    assert _looks_like_text(crop) is False


def test_looks_like_text_rejects_saturated_colored_crop():
    crop = Image.new("RGB", (60, 60), (220, 20, 20))  # solid red
    assert _looks_like_text(crop) is False


def test_composite_text_regions_restores_label_closer_to_source():
    source = _drawing_with_text("45h6")
    source_bytes = _png_bytes(source)
    regions = detect_text_regions(source_bytes)
    assert regions

    # Simulate a "diffusion pass" that garbled the label (drew something else
    # in roughly the same place) but kept the rest of the geometry.
    garbled = source.copy()
    d = ImageDraw.Draw(garbled)
    d.rectangle([140, 10, 300, 60], fill="white")
    d.text((150, 20), "??g@", fill="black")
    garbled_bytes = _png_bytes(garbled)

    fixed_bytes = composite_text_regions(garbled_bytes, source_bytes, regions, *source.size)

    score_before = text_fidelity_score(source_bytes, garbled_bytes, regions=regions)
    score_after = text_fidelity_score(source_bytes, fixed_bytes, regions=regions)
    assert score_after["mean_abs_diff"] < score_before["mean_abs_diff"]


def test_composite_text_regions_is_resolution_independent():
    source = _drawing_with_text("30h7")
    source_bytes = _png_bytes(source)
    regions = detect_text_regions(source_bytes)
    assert regions

    # "Diffusion output" at a different resolution than the source.
    upscaled = source.resize((source.width * 2, source.height * 2))
    upscaled_bytes = _png_bytes(upscaled)

    fixed_bytes = composite_text_regions(upscaled_bytes, source_bytes, regions, *source.size)
    out = Image.open(io.BytesIO(fixed_bytes))
    assert out.size == upscaled.size  # output resolution preserved, not forced back to source size


def test_text_fidelity_score_identical_images_near_zero():
    source = _drawing_with_text("40h7")
    source_bytes = _png_bytes(source)
    score = text_fidelity_score(source_bytes, source_bytes)
    assert score["mean_abs_diff"] < 1.0


def test_text_fidelity_score_none_when_no_regions():
    blank = Image.new("RGB", (100, 100), "white")
    b = _png_bytes(blank)
    score = text_fidelity_score(b, b)
    assert score["mean_abs_diff"] is None
    assert score["region_count"] == 0
