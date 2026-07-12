"""Topology consolidation (B3): collinear fragment merge + arc re-fit.

Both recognizers over-fragment (B0 diagnosis 2026-07-12): the patch-based
neural model splits one stroke at every 64px patch border, CV at every
junction. These tests pin the consolidation contract: fragments of one
stroke become one segment, dashed patterns and genuine corners survive,
polygonal circle approximations become true circles/arcs, and nothing else
is touched.
"""

from __future__ import annotations

import math

import pytest

from app.ai.cad_ir.schema import Circle, Point, Segment, TextEntity
from app.ai.cad_recognize.topology import consolidate_entities


def _seg(x1, y1, x2, y2, **kw):
    return Segment(p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2), **kw)


def test_collinear_run_merges_to_one_segment():
    fragments = [_seg(i * 60, 100, i * 60 + 58, 100) for i in range(10)]
    out, stats = consolidate_entities(fragments)
    assert stats["segments_in"] == 10
    assert len(out) == 1
    seg = out[0]
    assert seg.type == "segment"
    assert math.hypot(seg.p2.x - seg.p1.x, seg.p2.y - seg.p1.y) == pytest.approx(598, abs=3)
    assert abs(seg.p1.y - 100) < 1.5 and abs(seg.p2.y - 100) < 1.5


def test_overlapping_duplicates_collapse():
    out, _ = consolidate_entities([
        _seg(0, 50, 100, 50),
        _seg(40, 50.5, 140, 50.5),
        _seg(90, 50, 200, 50),
    ])
    assert len(out) == 1
    seg = out[0]
    assert seg.p1.x == pytest.approx(0, abs=2)
    assert seg.p2.x == pytest.approx(200, abs=2)


def test_dashed_line_gaps_survive():
    # ЕСКД dash gaps (>= 10px here) must NOT be welded into one solid stroke.
    dashes = [_seg(i * 30, 20, i * 30 + 15, 20) for i in range(5)]
    out, _ = consolidate_entities(dashes)
    assert len(out) == 5


def test_perpendicular_segments_do_not_merge():
    out, _ = consolidate_entities([
        _seg(0, 0, 100, 0),
        _seg(100, 0, 100, 100),
    ])
    assert len(out) == 2


def test_parallel_but_offset_segments_do_not_merge():
    out, _ = consolidate_entities([
        _seg(0, 0, 100, 0),
        _seg(0, 8, 100, 8),  # a table border 8px away — separate stroke
    ])
    assert len(out) == 2


def test_polygonal_circle_chain_becomes_circle():
    cx, cy, r, n = 200.0, 200.0, 80.0, 24
    ring = []
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = 2 * math.pi * (i + 1) / n
        ring.append(_seg(
            cx + r * math.cos(a0), cy + r * math.sin(a0),
            cx + r * math.cos(a1), cy + r * math.sin(a1),
        ))
    out, stats = consolidate_entities(ring)
    assert stats["arcs_fitted"] == 1
    circles = [e for e in out if e.type == "circle"]
    assert len(circles) == 1
    assert circles[0].center.x == pytest.approx(cx, abs=2)
    assert circles[0].center.y == pytest.approx(cy, abs=2)
    assert circles[0].radius == pytest.approx(r, abs=2)
    assert not any(e.type == "segment" for e in out)


def test_arc_chain_becomes_arc():
    cx, cy, r, n = 300.0, 300.0, 100.0, 12
    pieces = []
    for i in range(n):  # a 120-degree sweep
        a0 = math.radians(10 * i)
        a1 = math.radians(10 * (i + 1))
        pieces.append(_seg(
            cx + r * math.cos(a0), cy + r * math.sin(a0),
            cx + r * math.cos(a1), cy + r * math.sin(a1),
        ))
    out, stats = consolidate_entities(pieces)
    assert stats["arcs_fitted"] == 1
    arcs = [e for e in out if e.type == "arc"]
    assert len(arcs) == 1
    assert arcs[0].radius == pytest.approx(r, abs=2)


def test_rectangle_is_not_refit_into_circle():
    # Rectangle corners sit exactly on the circumcircle — edge midpoints
    # do not. The chain must stay segments.
    rect = [
        _seg(0, 0, 200, 0), _seg(200, 0, 200, 120),
        _seg(200, 120, 0, 120), _seg(0, 120, 0, 0),
    ]
    out, stats = consolidate_entities(rect)
    assert stats["arcs_fitted"] == 0
    assert sorted(e.type for e in out) == ["segment"] * 4


def test_non_segment_entities_pass_through():
    circle = Circle(center=Point(x=10, y=10), radius=5)
    text = TextEntity(position=Point(x=0, y=0), text="M8")
    out, _ = consolidate_entities([circle, text, _seg(0, 0, 50, 0), _seg(52, 0, 100, 0)])
    types = sorted(e.type for e in out)
    assert types == ["circle", "segment", "text"]


def test_merged_segment_inherits_anchor_metadata():
    out, _ = consolidate_entities([
        _seg(0, 0, 200, 0, line_class="axis", width_class="thin", confidence=0.9, origin="neural"),
        _seg(202, 0, 240, 0, line_class="contour", width_class="main", confidence=0.5, origin="cv"),
    ])
    assert len(out) == 1
    seg = out[0]
    # anchor = the longest member
    assert seg.line_class == "axis"
    assert seg.origin == "neural"
    assert 0.5 <= seg.confidence <= 1.0


def _arc(cx, cy, r, a0, a1, **kw):
    from app.ai.cad_ir.schema import Arc
    return Arc(center=Point(x=cx, y=cy), radius=r, start_angle=a0, end_angle=a1, **kw)


def test_cocircular_arc_fragments_merge_to_circle():
    fragments = [
        _arc(100, 100, 50, 0, 95),
        _arc(100.5, 100, 50.2, 93, 185),
        _arc(100, 100.5, 49.9, 184, 272),
        _arc(100, 100, 50, 270, 358),
    ]
    out, stats = consolidate_entities(fragments)
    assert stats["arcs_merged"] == 1
    assert [e.type for e in out] == ["circle"]
    assert out[0].radius == pytest.approx(50, abs=1)


def test_cocircular_arcs_with_true_gap_stay_arcs():
    # Two arcs on one circle separated by ~90-degree gaps (a shaft with
    # keyway breaks) must merge runs but NOT close into a full circle.
    out, _ = consolidate_entities([
        _arc(100, 100, 40, 0, 80),
        _arc(100, 100, 40, 81, 170),
        _arc(100, 100, 40, 260, 350),
    ])
    types = sorted(e.type for e in out)
    assert types == ["arc", "arc"]
    spans = sorted(round(abs(e.end_angle - e.start_angle)) for e in out)
    assert spans == [90, 170]


def test_concentric_arcs_different_radius_untouched():
    out, _ = consolidate_entities([
        _arc(100, 100, 30, 0, 120),
        _arc(100, 100, 60, 0, 120),
    ])
    assert sorted(e.radius for e in out) == [30, 60]


def test_single_segment_untouched():
    seg = _seg(0, 0, 100, 0)
    out, stats = consolidate_entities([seg])
    assert out == [seg]
    assert stats == {"consolidated": False}
