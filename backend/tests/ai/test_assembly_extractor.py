"""Tests for app.ai.assembly_extractor's deterministic CV stages.

Uses example-drawings/gearbox_assembly.png (a real, if synthetic, ГОСТ
assembly drawing: bordered spec table + 8 numbered position balloons) as a
live fixture — no VLM call involved, these stages are pure OpenCV/pytesseract.
Regression cases for three bugs found live-testing Ф4.1 against this file
for the first time: bordered-table detection, ring-border OCR failure, and
table-region false-positive balloons.
"""

import pathlib

import pytest

pytest.importorskip("cv2")

from app.ai.assembly_extractor import _detect_balloons, _detect_bom_table, _ocr_circle


def _gearbox_bytes() -> bytes:
    path = pathlib.Path(__file__).resolve().parents[3] / "example-drawings" / "gearbox_assembly.png"
    return path.read_bytes()


def test_detect_bom_table_finds_a_fully_bordered_table():
    """The spec table is drawn with a full outer border (grid lines all the
    way round, the ГОСТ 2.106 convention) — RETR_EXTERNAL collapses that into
    ONE blob spanning every row+column rather than separate thin per-row
    contours, so the aspect>4 "individual row" heuristic alone never fires.
    The fallback (largest wider-than-tall contour in the ROI) must catch it.
    """
    crop, bbox = _detect_bom_table(_gearbox_bytes())
    assert bbox is not None
    x, y, w, h = bbox
    # Known table position in the 1400x900 source image (upper-right).
    assert x > 800
    assert y < 50
    assert w > 400
    assert h > 150
    assert crop is not None and len(crop) > 0


def test_ocr_circle_ignores_ring_border():
    """A balloon's ring stroke, included whole in the OCR crop, makes
    tesseract fail outright (empty string, even with a digit whitelist) even
    though the digit itself is perfectly legible — verified against balloon
    "2" at (401, 187) in the fixture, which sits close enough to the small
    gear's circular outline that the ring dominates the un-cropped read.
    """
    import io

    import numpy as np
    from PIL import Image

    img_np = np.array(Image.open(io.BytesIO(_gearbox_bytes())).convert("RGB"))
    assert _ocr_circle(img_np, 401, 187, 15) == 2
    assert _ocr_circle(img_np, 79, 349, 13) == 3
    assert _ocr_circle(img_np, 721, 361, 12) == 4
    assert _ocr_circle(img_np, 61, 439, 13) == 8


def test_detect_balloons_excludes_table_region():
    """Table borders/digits are themselves detected as HoughCircles
    candidates and, once OCR-readable, would otherwise register as
    spurious balloons (observed: two different in-table circles both
    misread as item "1") — a real balloon is never drawn inside the spec
    table itself, so anything centered in the table bbox is dropped.
    """
    image_bytes = _gearbox_bytes()
    _, table_bbox = _detect_bom_table(image_bytes)
    assert table_bbox is not None

    balloons = _detect_balloons(image_bytes, exclude_bbox=table_bbox)
    ex, ey, ew, eh = table_bbox
    for balloon in balloons:
        inside_table = ex <= balloon.x <= ex + ew and ey <= balloon.y <= ey + eh
        assert not inside_table, (
            f"balloon {balloon.item_no} at ({balloon.x},{balloon.y}) is inside the table"
        )

    # The four balloons in open space (2, 3, 4, 8) are reliably found; 1, 5,
    # 6, 7 sit on/inside the drawing's own large gear circles and are a
    # known, documented remaining gap (see memory), not asserted here.
    found_item_nos = {b.item_no for b in balloons}
    assert {2, 3, 4, 8} <= found_item_nos


def test_detect_balloons_without_exclude_bbox_still_works():
    """exclude_bbox is optional — callers that don't have a table bbox yet
    (or none was found) still get balloon detection, unfiltered."""
    balloons = _detect_balloons(_gearbox_bytes())
    assert any(b.item_no == 3 for b in balloons)
