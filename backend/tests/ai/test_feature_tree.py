"""2D IR -> 3D feature-tree hypotheses (Ф10)."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.feature_tree import (
    Feature3D,
    FeatureTreeCandidate,
    compile_to_step,
    generate_feature_tree_candidates,
)
from app.ai.cad_ir.schema import CadIR, Circle, Point, Segment, SourceInfo, TextEntity


def _rect_ir(entities=None, scale=1.0) -> CadIR:
    base = [
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="contour", width_class="main"),
        Segment(p1=Point(x=100, y=0), p2=Point(x=100, y=60), line_class="contour", width_class="main"),
        Segment(p1=Point(x=100, y=60), p2=Point(x=0, y=60), line_class="contour", width_class="main"),
        Segment(p1=Point(x=0, y=60), p2=Point(x=0, y=0), line_class="contour", width_class="main"),
    ]
    return CadIR(
        source=SourceInfo(image_width=200, image_height=200), scale=scale,
        entities=base + (entities or []),
    )


def test_no_geometry_returns_no_candidates() -> None:
    ir = CadIR(source=SourceInfo(image_width=200, image_height=200), scale=1.0, entities=[])
    assert generate_feature_tree_candidates(ir) == []


def test_returns_multiple_ranked_candidates() -> None:
    candidates = generate_feature_tree_candidates(_rect_ir())
    assert len(candidates) >= 2
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)  # best first


def test_guess_candidates_flag_missing_side_view() -> None:
    candidates = generate_feature_tree_candidates(_rect_ir())
    guesses = [c for c in candidates if c.score < 0.5]
    assert guesses
    assert all(
        any("боков" in m or "разрез" in m for m in c.missing_data) for c in guesses
    )


def test_stated_depth_wins_and_has_no_missing_side_view_note() -> None:
    ir = _rect_ir([TextEntity(position=Point(x=50, y=30), text="глубина 25", height=5)])
    candidates = generate_feature_tree_candidates(ir)
    best = candidates[0]
    assert best.score > 0.5
    extrude = next(f for f in best.features if f.kind == "extrude")
    assert extrude.params["depth_mm"] == pytest.approx(25)
    assert extrude.confidence == pytest.approx(0.9)


def test_gost_tolerance_class_is_not_mistaken_for_a_stated_depth() -> None:
    """Regression: "40h7"/"Ø30h6"/"15H7" are the single most common shaft/
    hole fit callout on a real drawing (ГОСТ 25347), not a depth. A bare "h"
    pattern previously matched the tolerance letter and fabricated a
    high-confidence but bogus depth candidate."""
    for text in ("40h7", "Ø30h6", "15H7", "d=20h6"):
        ir = _rect_ir([TextEntity(position=Point(x=50, y=30), text=text, height=5)])
        candidates = generate_feature_tree_candidates(ir)
        assert all(c.score <= 0.5 for c in candidates), f"{text!r} produced a false high-confidence candidate"


def test_hole_feature_from_circle_with_correct_diameter_mm() -> None:
    ir = _rect_ir([Circle(center=Point(x=50, y=30), radius=10, line_class="contour", width_class="main")], scale=0.5)
    candidates = generate_feature_tree_candidates(ir)
    holes = [f for f in candidates[0].features if f.kind == "hole"]
    assert len(holes) == 1
    assert holes[0].params["diameter_mm"] == pytest.approx(10)  # 2*10px*0.5mm/px


def test_hole_through_flag_from_nearby_text() -> None:
    ir = _rect_ir([
        Circle(center=Point(x=50, y=30), radius=5, line_class="contour", width_class="main"),
        TextEntity(position=Point(x=52, y=32), text="сквозное отверстие", height=5),
    ])
    candidates = generate_feature_tree_candidates(ir)
    hole = next(f for f in candidates[0].features if f.kind == "hole")
    assert hole.params["through"] is True
    assert hole.confidence == pytest.approx(0.8)


def test_hole_without_through_marker_is_flagged_as_missing_data() -> None:
    ir = _rect_ir([Circle(center=Point(x=50, y=30), radius=5, line_class="contour", width_class="main")])
    candidates = generate_feature_tree_candidates(ir)
    best = candidates[0]
    hole = next(f for f in best.features if f.kind == "hole")
    assert hole.params["through"] is None
    assert any("сквозное" in m or "глух" in m for m in best.missing_data)


def test_compile_to_step_degrades_gracefully_without_cadquery() -> None:
    """cadquery/OCP isn't installed in this environment (deliberately —
    heavy native dep reserved for a dedicated cad-kernel container, per the
    module docstring) — compile_to_step must return None, never raise or
    fake output."""
    candidate = FeatureTreeCandidate(
        features=[Feature3D(kind="extrude", params={"width_mm": 10, "height_mm": 10, "depth_mm": 5})],
        score=0.9, label="test",
    )
    assert compile_to_step(candidate) is None
