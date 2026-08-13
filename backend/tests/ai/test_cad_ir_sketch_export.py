"""Tests for app.ai.cad_ir.sketch_export (Ф4 нового CAD-редактора).

The graph-walk is the one genuinely new nontrivial geometry algorithm in
the whole new-editor plan — covered thoroughly here since it has no
drawing corpus to catch mistakes the way the mechanical readers do.

The output shape ([x, y] LISTS, not {x, y} objects) is dictated entirely by
the kernel's own _sketch_point (infra/cad-kernel/server.py) — a live check
on a real generation caught the first version of this module emitting
objects instead, which passed every one of THESE tests since they only
checked self-consistency, not the true kernel contract. test_output_shape_
matches_the_kernels_sketch_point_contract below exists specifically so that
mistake can't silently recur.
"""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import Arc, Circle, Point, Segment
from app.ai.cad_ir.sketch_export import cad_ir_profile_to_sketch_segments


def _line(p1: tuple[float, float], p2: tuple[float, float]) -> Segment:
    return Segment(p1=Point(x=p1[0], y=p1[1]), p2=Point(x=p2[0], y=p2[1]))


def test_rectangle_walks_in_order_and_closes():
    entities = [
        _line((0, 0), (10, 0)),
        _line((10, 0), (10, 10)),
        _line((10, 10), (0, 10)),
        _line((0, 10), (0, 0)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert profile == [
        {"kind": "line", "to": [10, 0]},
        {"kind": "line", "to": [10, 10]},
        {"kind": "line", "to": [0, 10]},
        {"kind": "line", "to": [0, 0]},
    ]


def test_walk_starts_from_whichever_entity_is_first_in_the_list():
    # Same rectangle, but the loop must start from entities[0] regardless
    # of which physical edge that happens to be. The kernel always walks
    # from an implicit (0, 0), so entities[0]'s own start point (10, 10)
    # becomes the rebasing origin -- every emitted point is relative to it.
    entities = [
        _line((10, 10), (0, 10)),
        _line((0, 0), (10, 0)),
        _line((10, 0), (10, 10)),
        _line((0, 10), (0, 0)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert profile[0] == {"kind": "line", "to": [-10, 0]}
    assert len(profile) == 4
    # Closes back onto the rebased origin (0, 0) -- entities[0]'s own start
    # point, which is always where the kernel's implicit walk begins.
    assert tuple(profile[-1]["to"]) == (0, 0)


def test_rectangle_with_a_notch_is_one_closed_loop():
    # An L-shaped outline (rectangle with a rectangular notch bitten out of
    # one corner) — still topologically a single closed loop, no arcs
    # needed, exactly the shape this module's own docstring/plan uses as
    # the acceptance test for Ф4.
    entities = [
        _line((0, 0), (10, 0)),
        _line((10, 0), (10, 6)),
        _line((10, 6), (6, 6)),
        _line((6, 6), (6, 10)),
        _line((6, 10), (0, 10)),
        _line((0, 10), (0, 0)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert len(profile) == 6
    assert all(seg["kind"] == "line" for seg in profile)
    assert profile[-1]["to"] == [0, 0]


def test_arc_walked_forward_reports_counter_clockwise():
    # Semicircle (arc) + its diameter (line) — a "D" shape. Arc listed
    # first, so it is walked in its own natural start_angle->end_angle
    # direction. Its own start point (10, 0) is the rebasing origin.
    entities = [
        Arc(center=Point(x=5, y=0), radius=5, start_angle=0, end_angle=180),
        _line((10, 0), (0, 0)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert profile[0]["kind"] == "arc"
    assert profile[0]["to"][0] == pytest.approx(-10)
    assert profile[0]["to"][1] == pytest.approx(0)
    assert profile[0]["center"] == [-5, 0]
    assert profile[0]["clockwise"] is False
    assert profile[1] == {"kind": "line", "to": [0, 0]}


def test_arc_walked_backward_reports_clockwise():
    # Same "D" shape, but the line is listed first — the walk reaches the
    # arc from ITS end_angle point, traversing it against its own stored
    # direction. The line's own start point (10, 0) is the rebasing origin.
    entities = [
        _line((10, 0), (0, 0)),
        Arc(center=Point(x=5, y=0), radius=5, start_angle=0, end_angle=180),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert profile[0] == {"kind": "line", "to": [-10, 0]}
    assert profile[1]["kind"] == "arc"
    assert profile[1]["to"] == [0, 0]
    assert profile[1]["center"] == [-5, 0]
    assert profile[1]["clockwise"] is True


def test_profile_drawn_away_from_origin_is_rebased_to_local_zero():
    # A live-caught bug: the kernel's _sketch_wire ALWAYS walks from an
    # implicit (0, 0) (every call site hardcodes it), but a sketch drawn on
    # a canvas naturally sits at whatever absolute coordinates the user
    # clicked -- e.g. an L-shape drawn entirely between x=[19.9, 40] never
    # touching (0, 0) at all. The exported profile must be translated so
    # entities[0]'s own start point becomes the walk's local origin.
    entities = [
        _line((20, 20), (40, 20)),
        _line((40, 20), (40, 30)),
        _line((40, 30), (20, 30)),
        _line((20, 30), (20, 20)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    assert profile == [
        {"kind": "line", "to": [20, 0]},
        {"kind": "line", "to": [20, 10]},
        {"kind": "line", "to": [0, 10]},
        {"kind": "line", "to": [0, 0]},
    ]


def test_output_shape_matches_the_kernels_sketch_point_contract():
    # infra/cad-kernel/server.py's _sketch_point rejects anything that
    # isn't `isinstance(raw, list) and len(raw) == 2` — an {"x":.., "y":..}
    # object 422s with "must be [x, y]". This test exists so a future
    # change can't silently reintroduce that exact live-caught bug.
    entities = [
        _line((0, 0), (10, 0)),
        _line((10, 0), (10, 10)),
        _line((10, 10), (0, 10)),
        _line((0, 10), (0, 0)),
    ]
    profile = cad_ir_profile_to_sketch_segments(entities)
    for segment in profile:
        assert isinstance(segment["to"], list)
        assert len(segment["to"]) == 2
        if "center" in segment:
            assert isinstance(segment["center"], list)
            assert len(segment["center"]) == 2


def test_open_chain_is_rejected():
    entities = [
        _line((0, 0), (10, 0)),
        _line((10, 0), (10, 10)),
        # Missing the closing edges -- (10, 10) and (0, 0) each touch only
        # one entity.
    ]
    with pytest.raises(ValueError, match="не замкнут"):
        cad_ir_profile_to_sketch_segments(entities)


def test_branching_vertex_is_rejected():
    # Three lines meeting at the origin -- degree 3, not a simple loop.
    entities = [
        _line((0, 0), (10, 0)),
        _line((0, 0), (0, 10)),
        _line((0, 0), (-10, 0)),
        _line((10, 0), (0, 10)),
        _line((0, 10), (-10, 0)),
    ]
    with pytest.raises(ValueError, match="разветвлён"):
        cad_ir_profile_to_sketch_segments(entities)


def test_disjoint_extra_entity_is_rejected():
    entities = [
        *[
            _line((0, 0), (10, 0)),
            _line((10, 0), (10, 10)),
            _line((10, 10), (0, 10)),
            _line((0, 10), (0, 0)),
        ],
        _line((100, 100), (110, 100)),  # dangling, not connected to anything
    ]
    with pytest.raises(ValueError):
        cad_ir_profile_to_sketch_segments(entities)


def test_standalone_circle_exports_as_two_exact_semicircular_arcs():
    profile = cad_ir_profile_to_sketch_segments([
        Circle(center=Point(x=25, y=-10), radius=5),
    ])
    assert profile == [
        {
            "kind": "arc",
            "to": [-10.0, 0.0],
            "center": [-5, 0.0],
            "clockwise": False,
        },
        {
            "kind": "arc",
            "to": [0.0, 0.0],
            "center": [-5, 0.0],
            "clockwise": False,
        },
    ]


def test_circle_mixed_with_other_contours_is_rejected():
    entities = [
        Circle(center=Point(x=0, y=0), radius=5),
        _line((0, 0), (10, 0)),
    ]
    with pytest.raises(ValueError, match="одну окружность"):
        cad_ir_profile_to_sketch_segments(entities)


def test_empty_sketch_is_rejected():
    with pytest.raises(ValueError, match="пуст"):
        cad_ir_profile_to_sketch_segments([])
