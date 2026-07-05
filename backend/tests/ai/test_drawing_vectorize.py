"""Tests for the vector-reconstruction pass (drawing_vectorize.py). Small
synthetic ink masks — offline, deterministic. Each test targets one of the
guarantees the module's docstring makes, plus regression shapes for every
corruption mode that killed the older _snap_canonical_lines approach.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.drawing_vectorize import (  # noqa: E402
    _skeletonize,
    _verify_coverage,
    _width_classes,
    redraw_ink,
)


def _blank(h: int = 300, w: int = 400) -> np.ndarray:
    return np.zeros((h, w), dtype=np.uint8)


def _ink_of(canvas: np.ndarray) -> np.ndarray:
    """Boolean ink mask of a redraw_ink grayscale result."""
    return canvas < 128


# ── Straightening ────────────────────────────────────────────────────────────


def test_wavy_near_horizontal_line_comes_out_straight_and_thin():
    ink = _blank()
    # A hand-wobbly "horizontal" line: ±3px sine wobble over 360px.
    xs = np.arange(20, 380)
    ys = (150 + 3.0 * np.sin(xs / 25.0)).round().astype(int)
    for x, y in zip(xs, ys):
        cv2.circle(ink, (int(x), int(y)), 2, 255, -1)

    out = redraw_ink(ink)
    assert out is not None, "clean synthetic line must not be declined"
    ink_out = _ink_of(out)
    rows = np.nonzero(ink_out.any(axis=1))[0]
    band = rows.max() - rows.min() + 1
    # Original band: 6px wobble + 5px stroke ≈ 11; straightened: stroke only.
    assert band <= 7, f"line not straightened (vertical band {band}px)"


def test_line_at_odd_angle_is_not_forced_to_canonical():
    ink = _blank(400, 400)
    # 30° is a legitimate ЕСКД angle for e.g. auxiliary views — far outside
    # the 4° snap tolerance; it must stay at its own angle, just straight.
    cv2.line(ink, (50, 50), (350, 223), 255, 3)  # atan(173/300) ≈ 30°

    out = redraw_ink(ink)
    assert out is not None
    ys, xs = np.nonzero(_ink_of(out))
    slope = np.polyfit(xs, ys, 1)[0]
    assert 0.5 < slope < 0.65, f"30° line was distorted (slope {slope:.2f})"


# ── Curves ───────────────────────────────────────────────────────────────────


def test_circle_survives_as_a_circle():
    ink = _blank(300, 300)
    cv2.circle(ink, (150, 150), 100, 255, 3)

    out = redraw_ink(ink)
    assert out is not None
    ys, xs = np.nonzero(_ink_of(out))
    r = np.hypot(xs - 150, ys - 150)
    # Every redrawn pixel sits on the original radius (within stroke width).
    assert r.min() > 92 and r.max() < 108, "circle geometry was corrupted"
    # And it's still a full ring, not an arc fragment: ink in all quadrants.
    for qy, qx in ((slice(0, 150), slice(0, 150)), (slice(0, 150), slice(150, 300)),
                   (slice(150, 300), slice(0, 150)), (slice(150, 300), slice(150, 300))):
        assert _ink_of(out)[qy, qx].any(), "circle lost a quadrant"


def test_free_curve_is_preserved_not_flattened():
    ink = _blank(300, 400)
    # An S-curve no line/circle fit should claim.
    xs = np.arange(30, 370)
    ys = (150 + 60 * np.sin((xs - 30) / 55.0)).round().astype(int)
    for x, y in zip(xs, ys):
        cv2.circle(ink, (int(x), int(y)), 2, 255, -1)

    out = redraw_ink(ink)
    assert out is not None
    ink_out = _ink_of(out)
    rows = np.nonzero(ink_out.any(axis=1))[0]
    # A flattened curve would collapse the ~120px vertical extent.
    assert rows.max() - rows.min() > 100, "curve was flattened"


# ── Filled shapes (dimension arrowheads etc.) ────────────────────────────────


def test_filled_arrowhead_stays_solid():
    ink = _blank(200, 400)
    cv2.line(ink, (40, 100), (360, 100), 255, 2)
    arrow = np.array([[40, 100], [70, 92], [70, 108]], dtype=np.int32)
    cv2.fillPoly(ink, [arrow], 255)

    out = redraw_ink(ink)
    assert out is not None
    head = _ink_of(out)[92:108, 40:70]
    # A skeleton redraw of the triangle would leave a thin spur (~15% fill);
    # kept-as-raster keeps it solid (~50% of the bounding box).
    fill = head.mean()
    assert fill > 0.3, f"arrowhead was thinned away (fill {fill:.2f})"


# ── Exclusion boxes (text regions) ───────────────────────────────────────────


def test_exclusion_box_content_is_kept_verbatim():
    ink = _blank(300, 400)
    cv2.line(ink, (20, 50), (380, 50), 255, 2)
    # Dense text-like clutter inside the exclusion box.
    rng = np.random.default_rng(3)
    for _ in range(60):
        x, y = int(rng.integers(210, 300)), int(rng.integers(210, 260))
        cv2.line(ink, (x, y), (x + 4, y + 2), 255, 1)

    out = redraw_ink(ink, exclusion_boxes=[(200, 200, 310, 270)])
    assert out is not None
    box_out = _ink_of(out)[200:270, 200:310]
    box_in = ink[200:270, 200:310] > 0
    assert (box_out == box_in).all(), "exclusion box content was modified"


# ── Regression shapes from _snap_canonical_lines' live failures ─────────────


def test_two_parallel_lines_a_few_px_apart_stay_two_lines():
    """The failure that kept the old pass off by default: near-duplicate
    fragments in the same offset neighborhood each redrawn, compounding into
    one thicker bar."""
    ink = _blank(200, 400)
    cv2.line(ink, (20, 96), (380, 96), 255, 2)
    cv2.line(ink, (20, 104), (380, 104), 255, 2)

    out = redraw_ink(ink)
    assert out is not None
    col = _ink_of(out)[:, 200]
    runs = np.diff(np.nonzero(col)[0])
    # Two separate strokes → a gap in the ink column between them.
    assert (runs > 1).any(), "parallel lines were merged into one bar"


def test_far_apart_collinear_segments_are_not_bridged():
    ink = _blank(100, 500)
    cv2.line(ink, (10, 50), (60, 50), 255, 2)
    cv2.line(ink, (440, 50), (490, 50), 255, 2)

    out = redraw_ink(ink)
    assert out is not None
    middle = _ink_of(out)[45:55, 150:350]
    assert middle.sum() == 0, "unrelated far-apart segments were bridged"


# ── Safety valves ────────────────────────────────────────────────────────────


def test_dense_non_line_image_is_declined():
    rng = np.random.default_rng(0)
    ink = (rng.random((200, 200)) < 0.5).astype(np.uint8) * 255
    assert redraw_ink(ink) is None


def test_verify_coverage_rejects_displaced_redraw():
    ink = _blank(200, 400) > 0
    ink[100, 20:380] = True
    displaced = _blank(200, 400) > 0
    displaced[150, 20:380] = True  # 50px off — far beyond the dilation
    assert _verify_coverage(ink, displaced) is False


def test_verify_coverage_accepts_faithful_redraw():
    ink = _blank(200, 400)
    cv2.line(ink, (20, 100), (380, 103), 255, 3)
    redrawn = _blank(200, 400)
    cv2.line(redrawn, (20, 101), (380, 101), 255, 2)  # straightened, ≤3px off
    assert _verify_coverage(ink > 0, redrawn > 0) is True


# ── Building blocks ──────────────────────────────────────────────────────────


def test_skeletonize_reduces_thick_line_to_thin_path():
    ink = _blank(100, 200)
    cv2.line(ink, (20, 50), (180, 50), 255, 7)
    skel = _skeletonize(ink > 0)
    cols = skel[:, 100]
    assert 1 <= cols.sum() <= 2, "skeleton is not thin"
    assert skel[:, 30:170].any(axis=0).all(), "skeleton lost continuity"


def test_width_classes_split_thick_and_thin():
    thin_px, thick_px, split = _width_classes([2.0, 2.0, 5.0, 5.0], [500, 500, 500, 500])
    assert split is not None
    assert thin_px < thick_px


def test_width_classes_single_weight_sheet():
    thin_px, thick_px, split = _width_classes([3.0, 3.2, 2.9], [300, 300, 300])
    assert split is None
    assert thin_px == thick_px
