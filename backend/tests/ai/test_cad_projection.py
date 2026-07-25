"""Stage 2: views derived from the solid, in ГОСТ 2.305 projection alignment."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import Circle, Segment
from app.ai.cad_projection import (
    VIEW_GAP_MM,
    place_views,
    verify_views_against_solid,
)


def _views() -> dict:
    """A stepped shaft Ø50×100 with a Ø16 bore, as the projector returns it."""
    return {
        "front": {
            "bounds_mm": {"u_min": 0.0, "u_max": 100.0, "v_min": -25.0, "v_max": 25.0},
            "visible": [
                {"type": "line", "points": [[0.0, 25.0], [100.0, 25.0]]},
                {"type": "line", "points": [[0.0, -25.0], [100.0, -25.0]]},
            ],
            "hidden": [{"type": "line", "points": [[0.0, 8.0], [90.0, 8.0]]}],
        },
        "side": {
            "bounds_mm": {"u_min": -25.0, "u_max": 25.0, "v_min": -25.0, "v_max": 25.0},
            "visible": [{"type": "circle", "center": [0.0, 0.0], "radius": 25.0}],
            "hidden": [{"type": "circle", "center": [0.0, 0.0], "radius": 8.0}],
        },
    }


def test_side_view_sits_on_the_front_view_axis():
    """The identity that makes these projections rather than two drawings."""
    _entities, placements = place_views(_views(), px_per_mm=4.0)
    assert placements["side"]["offset_v"] == pytest.approx(placements["front"]["offset_v"])


def test_side_view_is_placed_to_the_right_with_the_declared_gap():
    _entities, placements = place_views(_views(), px_per_mm=4.0)
    # front spans 0..100 in view coords, mapped from offset_u
    front_right = placements["front"]["offset_u"] + 100.0
    side_left = placements["side"]["offset_u"] - 25.0
    assert side_left == pytest.approx(front_right + VIEW_GAP_MM)


def test_top_view_goes_below_sharing_the_horizontal_position():
    views = _views()
    views["top"] = {
        "bounds_mm": {"u_min": 0.0, "u_max": 100.0, "v_min": -25.0, "v_max": 25.0},
        "visible": [],
        "hidden": [],
    }
    _entities, placements = place_views(views, px_per_mm=4.0)
    assert placements["top"]["offset_u"] == pytest.approx(placements["front"]["offset_u"])
    assert placements["top"]["offset_v"] > placements["front"]["offset_v"]


def test_visible_and_hidden_get_the_gost_line_classes():
    entities, _ = place_views(_views(), px_per_mm=4.0)
    contour = [e for e in entities if e.line_class == "contour"]
    hidden = [e for e in entities if e.line_class == "hidden"]
    assert len(contour) == 3 and len(hidden) == 2
    assert all(e.width_class == "main" for e in contour)
    assert all(e.width_class == "thin" for e in hidden)


def test_view_is_not_mirrored_by_the_canvas_flip():
    """The projector is y-up, the canvas y-down; a missed flip mirrors the part."""
    entities, placements = place_views(_views(), px_per_mm=4.0)
    top_edge = next(
        e for e in entities
        if isinstance(e, Segment) and e.line_class == "contour"
        and e.p1.y == pytest.approx(e.p2.y) and e.p1.y < 100
    )
    bottom_edge = next(
        e for e in entities
        if isinstance(e, Segment) and e.line_class == "contour"
        and e.p1.y == pytest.approx(e.p2.y) and e.p1.y > 100
    )
    # v=+25 must land ABOVE v=-25 on a y-down canvas.
    assert top_edge.p1.y < bottom_edge.p1.y


def test_circles_keep_their_radius_in_sheet_pixels():
    entities, _ = place_views(_views(), px_per_mm=4.0)
    circles = [e for e in entities if isinstance(e, Circle)]
    assert sorted(round(c.radius, 3) for c in circles) == [32.0, 100.0]


def test_a_sheet_without_a_front_view_produces_nothing():
    assert place_views({"side": _views()["side"]}, px_per_mm=4.0) == ([], {})


# --- verification -----------------------------------------------------------


def _report(length: float = 100.0, diameter: float = 50.0) -> dict:
    return {"bounds_mm": {"x": diameter, "y": diameter, "z": length}}


def test_verification_passes_when_views_measure_the_solid():
    result = verify_views_against_solid(_views(), _report())
    assert result["ok"]
    assert result["front_length_mm"] == 100.0
    assert result["side_width_mm"] == 50.0


def test_verification_catches_a_swapped_view_frame():
    """A wrong axis mapping draws a plausible picture of the wrong part."""
    views = _views()
    views["front"]["bounds_mm"] = {
        "u_min": -25.0, "u_max": 25.0, "v_min": 0.0, "v_max": 100.0,
    }
    result = verify_views_against_solid(views, _report())
    assert not result["ok"]
    assert result["front_matches_solid"] is False
