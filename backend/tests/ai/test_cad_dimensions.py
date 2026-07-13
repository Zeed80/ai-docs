"""Dimension reconstruction (B2): OCR value + thin line → DimensionEntity."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import Point, Segment, SourceRegion, TextEntity
from app.ai.cad_recognize.dimensions import _parse_label, reconstruct_dimensions


def _thin(x1, y1, x2, y2):
    return Segment(
        p1=Point(x=x1, y=y1),
        p2=Point(x=x2, y=y2),
        line_class="thin",
        width_class="thin",
    )


def _label(text, cx, cy, h=12.0):
    return TextEntity(
        position=Point(x=cx, y=cy + h / 2),
        text=text,
        height=h,
        source_region=SourceRegion(x0=cx - 15, y0=cy - h / 2, x1=cx + 15, y1=cy + h / 2),
    )


# ── label parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,kind,value,tol",
    [
        ("40", "linear", 40.0, None),
        ("Ø40", "diameter", 40.0, None),
        ("⌀25H7", "diameter", 25.0, "H7"),
        ("R8", "radial", 8.0, None),
        ("12,5", "linear", 12.5, None),
        ("100 -0.05", "linear", 100.0, "-0.05"),
    ],
)
def test_parse_valid_labels(text, kind, value, tol):
    parsed = _parse_label(text)
    assert parsed is not None
    assert parsed[0] == kind
    assert parsed[1] == pytest.approx(value)
    assert parsed[2] == tol


@pytest.mark.parametrize("text", ["", "А", "Вид сверху", "не число", "0"])
def test_parse_rejects_non_dimensions(text):
    assert _parse_label(text) is None


# ── reconstruction ───────────────────────────────────────────────────────────


def test_label_pairs_with_nearest_thin_line():
    line = _thin(100, 200, 300, 200)  # 200px, scale 0.5 → 100mm
    text = _label("100", 200, 185)  # just above the line midpoint
    entities, texts, count = reconstruct_dimensions(
        [line], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 1
    dims = [e for e in entities if e.type == "dimension"]
    assert len(dims) == 1
    d = dims[0]
    assert d.kind == "linear"
    assert d.value_mm == pytest.approx(100.0)
    assert d.assurance == "inferred"
    assert d.confidence >= 0.8  # measured 100mm matches value → high conf
    # the consumed line and the label are gone
    assert not any(e.type == "segment" for e in entities)
    assert texts == []


def test_value_mismatch_flags_low_confidence():
    line = _thin(100, 200, 300, 200)  # 200px → 100mm at scale 0.5
    text = _label("250", 200, 185)  # claims 250mm, measures 100mm
    entities, _texts, count = reconstruct_dimensions(
        [line], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 1
    d = next(e for e in entities if e.type == "dimension")
    assert d.confidence < 0.5


def test_far_label_does_not_pair():
    line = _thin(100, 200, 300, 200)
    text = _label("100", 200, 20)  # far above, out of reach
    entities, texts, count = reconstruct_dimensions(
        [line], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 0
    assert any(e.type == "segment" for e in entities)
    assert len(texts) == 1


def test_long_contour_lines_are_never_consumed():
    # A long main-width segment (structural contour) can never be eaten as a
    # dimension line, even with a number right next to it. 500px >
    # _SHORT_LINE_FRACTION * min(sheet) = 0.3 * 600 = 180.
    contour = Segment(
        p1=Point(x=50, y=200),
        p2=Point(x=550, y=200),
        line_class="contour",
        width_class="main",
    )
    text = _label("100", 300, 185)
    entities, texts, count = reconstruct_dimensions(
        [contour], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 0
    assert any(e.type == "segment" for e in entities)
    assert len(texts) == 1


def test_short_main_line_pairs_when_label_sits_on_it():
    # The neural backend marks every segment "main"; a genuinely short one
    # with the label right on it IS a dimension line.
    line = Segment(
        p1=Point(x=100, y=200),
        p2=Point(x=180, y=200),  # 80px, well under the short cap
        line_class="contour",
        width_class="main",
    )
    text = _label("40", 140, 192, h=10)  # perp 8px < 1.3*10
    entities, _texts, count = reconstruct_dimensions(
        [line], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 1
    assert any(e.type == "dimension" for e in entities)


def test_diameter_label_builds_diameter_dimension():
    line = _thin(100, 200, 180, 200)  # 80px → 40mm
    text = _label("Ø40", 140, 185)
    entities, _texts, count = reconstruct_dimensions(
        [line], [text], scale=0.5, sheet_w=800, sheet_h=600
    )
    assert count == 1
    d = next(e for e in entities if e.type == "dimension")
    assert d.kind == "diameter"


def test_no_scale_still_pairs_without_mismatch_flag():
    line = _thin(100, 200, 300, 200)
    text = _label("100", 200, 185)
    entities, _texts, count = reconstruct_dimensions(
        [line], [text], scale=None, sheet_w=800, sheet_h=600
    )
    assert count == 1
    d = next(e for e in entities if e.type == "dimension")
    # no scale → can't measure → no mismatch penalty
    assert d.confidence >= 0.8
