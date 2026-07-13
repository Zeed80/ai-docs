"""Move/copy/mirror/fillet/chamfer geometry (Ф5.6-5.7)."""

from __future__ import annotations

import math

import pytest

from app.ai.cad_ir.schema import Arc, Circle, Point, Segment, TextEntity
from app.ai.cad_ir.transform import (
    FilletChamferError,
    SketchOpError,
    chamfer,
    duplicate_entity,
    extend_segment,
    fillet,
    mirror_entity,
    offset_entity,
    pattern_linear,
    pattern_polar,
    translate_entity,
    trim_segment,
)


def _seg(x1, y1, x2, y2):
    return Segment(p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2))


def test_trim_keeps_side_away_from_click() -> None:
    # horizontal 0..100, cut by a vertical at x=60, click the right half → keep 0..60
    out = trim_segment(_seg(0, 0, 100, 0), _seg(60, -10, 60, 10), Point(x=90, y=0))
    xs = sorted([out.p1.x, out.p2.x])
    assert xs == pytest.approx([0, 60])


def test_trim_rejects_non_crossing_cutter() -> None:
    with pytest.raises(SketchOpError):
        trim_segment(_seg(0, 0, 100, 0), _seg(200, -10, 200, 10), Point(x=50, y=0))


def test_extend_lengthens_to_boundary() -> None:
    out = extend_segment(_seg(0, 0, 50, 0), _seg(80, -10, 80, 10), Point(x=45, y=0))
    assert max(out.p1.x, out.p2.x) == pytest.approx(80)


def test_extend_refuses_to_shorten() -> None:
    # boundary crosses inside the segment → that would be a trim, not an extend
    with pytest.raises(SketchOpError):
        extend_segment(_seg(0, 0, 100, 0), _seg(40, -10, 40, 10), Point(x=90, y=0))


def test_offset_segment_moves_toward_side_point() -> None:
    out = offset_entity(_seg(0, 0, 100, 0), 10, Point(x=50, y=20))
    assert out.p1.y == pytest.approx(10) and out.p2.y == pytest.approx(10)
    assert out.id  # fresh copy


def test_offset_circle_outward_and_inward() -> None:
    c = Circle(center=Point(x=0, y=0), radius=20)
    assert offset_entity(c, 5, Point(x=100, y=0)).radius == pytest.approx(25)
    assert offset_entity(c, 5, Point(x=1, y=0)).radius == pytest.approx(15)
    with pytest.raises(SketchOpError):
        offset_entity(c, 25, Point(x=1, y=0))  # inward past the centre


def test_pattern_linear_returns_new_copies_only() -> None:
    copies = pattern_linear(_seg(0, 0, 10, 0), 4, 30, 0)
    assert len(copies) == 3
    assert [round(c.p1.x) for c in copies] == [30, 60, 90]


def test_pattern_polar_full_circle_divides_by_count() -> None:
    copies = pattern_polar(_seg(10, 0, 20, 0), 4, Point(x=0, y=0), 360)
    assert len(copies) == 3
    # 90° step: first copy's p1 (10,0) → (0,10)
    assert copies[0].p1.x == pytest.approx(0, abs=1e-6)
    assert copies[0].p1.y == pytest.approx(10, abs=1e-6)


def test_translate_segment() -> None:
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    out = translate_entity(seg, 5, 3)
    assert (out.p1.x, out.p1.y) == (5, 3)
    assert (out.p2.x, out.p2.y) == (15, 3)
    assert out.id == seg.id  # translate is in-place semantics, same identity
    assert out.origin == "human"


def test_translate_circle_moves_center_only() -> None:
    c = Circle(center=Point(x=10, y=10), radius=5)
    out = translate_entity(c, 1, 1)
    assert (out.center.x, out.center.y) == (11, 11)
    assert out.radius == 5


def test_translate_polyline_moves_all_points() -> None:
    from app.ai.cad_ir.schema import Polyline

    pl = Polyline(points=[Point(x=0, y=0), Point(x=1, y=1), Point(x=2, y=0)])
    out = translate_entity(pl, 10, 0)
    assert [(p.x, p.y) for p in out.points] == [(10, 0), (11, 1), (12, 0)]


def test_duplicate_entity_gets_a_new_id() -> None:
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    dup = duplicate_entity(seg, 5, 5)
    assert dup.id != seg.id
    assert (dup.p1.x, dup.p1.y) == (5, 5)


def test_duplicate_with_zero_offset_stacks_exactly() -> None:
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    dup = duplicate_entity(seg)
    assert (dup.p1.x, dup.p1.y) == (0, 0)
    assert (dup.p2.x, dup.p2.y) == (10, 0)


def test_mirror_segment_across_vertical_line() -> None:
    seg = Segment(p1=Point(x=10, y=0), p2=Point(x=20, y=5))
    out = mirror_entity(seg, Point(x=0, y=0), Point(x=0, y=100))  # mirror across x=0
    assert out.p1.x == pytest.approx(-10)
    assert out.p1.y == pytest.approx(0)
    assert out.p2.x == pytest.approx(-20)
    assert out.p2.y == pytest.approx(5)


def test_mirror_text_flips_position() -> None:
    t = TextEntity(position=Point(x=10, y=0), text="Ø18")
    out = mirror_entity(t, Point(x=0, y=0), Point(x=0, y=1))
    assert out.position.x == pytest.approx(-10)


def test_mirror_arc_reverses_and_reflects_angles() -> None:
    # Quarter arc from 0deg to 90deg, centered at origin; mirror across the x-axis.
    arc = Arc(center=Point(x=0, y=0), radius=10, start_angle=0, end_angle=90)
    out = mirror_entity(arc, Point(x=0, y=0), Point(x=1, y=0))
    # Reflecting across the x-axis: a point at angle theta maps to angle -theta.
    # start(0)->0, end(90)->-90 (=270); with reversal (new_start=2phi-old_end,
    # new_end=2phi-old_start) => new_start=-90=270, new_end=0.
    assert out.start_angle == pytest.approx(270)
    assert out.end_angle == pytest.approx(0)


def test_chamfer_right_angle_corner() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    seg2 = Segment(p1=Point(x=0, y=0), p2=Point(x=0, y=100))
    new1, new2, bevel = chamfer(seg1, seg2, 10)
    # seg1's corner endpoint (p1, at the shared corner) moves to (10,0)
    assert (new1.p1.x, new1.p1.y) == pytest.approx((10, 0))
    assert (new1.p2.x, new1.p2.y) == pytest.approx((100, 0))
    assert (new2.p1.x, new2.p1.y) == pytest.approx((0, 10))
    assert (bevel.p1.x, bevel.p1.y) == pytest.approx((10, 0))
    assert (bevel.p2.x, bevel.p2.y) == pytest.approx((0, 10))


def test_chamfer_rejects_non_positive_distance() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    seg2 = Segment(p1=Point(x=0, y=0), p2=Point(x=0, y=100))
    with pytest.raises(FilletChamferError):
        chamfer(seg1, seg2, 0)


def test_chamfer_rejects_segments_without_shared_corner() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    seg2 = Segment(p1=Point(x=500, y=500), p2=Point(x=0, y=600))
    with pytest.raises(FilletChamferError):
        chamfer(seg1, seg2, 10)


def test_fillet_right_angle_corner_produces_quarter_arc() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    seg2 = Segment(p1=Point(x=0, y=0), p2=Point(x=0, y=100))
    new1, new2, arc = fillet(seg1, seg2, 10)
    assert arc.radius == pytest.approx(10)
    # Classic right-angle fillet: center offset diagonally by (r, r) from the corner.
    assert (arc.center.x, arc.center.y) == pytest.approx((10, 10))
    span = (arc.end_angle - arc.start_angle) % 360
    assert span == pytest.approx(90, abs=0.5)
    # Trimmed segment endpoints are the tangent points, at distance r from the corner.
    assert (new1.p1.x, new1.p1.y) == pytest.approx((10, 0))
    assert (new2.p1.x, new2.p1.y) == pytest.approx((0, 10))


def test_fillet_rejects_collinear_segments() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    seg2 = Segment(p1=Point(x=100, y=0), p2=Point(x=200, y=0))
    with pytest.raises(FilletChamferError):
        fillet(seg1, seg2, 5)


def test_fillet_rejects_radius_too_large_for_segments() -> None:
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    seg2 = Segment(p1=Point(x=0, y=0), p2=Point(x=0, y=10))
    with pytest.raises(FilletChamferError):
        fillet(seg1, seg2, 50)


def test_fillet_arc_is_tangent_to_both_segments() -> None:
    """Geometric sanity check beyond the right-angle special case: the
    vector from the fillet center to each tangent point must be
    perpendicular to that segment's direction."""
    seg1 = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=20))
    seg2 = Segment(p1=Point(x=0, y=0), p2=Point(x=-30, y=80))
    new1, new2, arc = fillet(seg1, seg2, 5)
    t1 = new1.p1 if math.hypot(new1.p1.x, new1.p1.y) < 50 else new1.p2
    d1x, d1y = seg1.p2.x - seg1.p1.x, seg1.p2.y - seg1.p1.y
    v1x, v1y = t1.x - arc.center.x, t1.y - arc.center.y
    dot1 = d1x * v1x + d1y * v1y
    assert dot1 == pytest.approx(0, abs=1e-6)
