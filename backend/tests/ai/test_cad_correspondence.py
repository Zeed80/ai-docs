"""Correspondence graph between orthographic views (D1)."""

from __future__ import annotations

from app.ai.cad_ir.correspondence import (
    ViewCircle,
    ViewGeometry,
    build_correspondence_graph,
)


def _kinds(graph):
    return sorted(c.kind for c in graph.correspondences)


def test_single_view_has_no_correspondences():
    graph = build_correspondence_graph([ViewGeometry(label="A", projection="front")])
    assert graph.correspondences == []
    assert graph.issues == []


def test_consistent_scales_are_a_correspondence():
    views = [
        ViewGeometry(label="Спереди", projection="front", scale=0.5),
        ViewGeometry(label="Сверху", projection="top", scale=0.51),
    ]
    graph = build_correspondence_graph(views)
    assert "scale" in _kinds(graph)
    assert graph.issues == []


def test_divergent_scales_are_flagged():
    views = [
        ViewGeometry(label="Спереди", projection="front", scale=0.5),
        ViewGeometry(label="Сверху", projection="top", scale=1.0),
    ]
    graph = build_correspondence_graph(views)
    assert "scale" not in _kinds(graph)
    assert any("расход" in i for i in graph.issues)


def test_front_top_axis_alignment():
    views = [
        ViewGeometry(label="Спереди", projection="front", bbox=(0, 0, 100, 60)),
        ViewGeometry(label="Сверху", projection="top", bbox=(2, 80, 98, 140)),
    ]
    graph = build_correspondence_graph(views)
    assert "axis_alignment" in _kinds(graph)


def test_misaligned_orthographic_views_flagged():
    views = [
        ViewGeometry(label="Спереди", projection="front", bbox=(0, 0, 100, 60)),
        ViewGeometry(label="Сверху", projection="top", bbox=(200, 80, 300, 140)),
    ]
    graph = build_correspondence_graph(views)
    assert "axis_alignment" not in _kinds(graph)
    assert any("выровнен" in i for i in graph.issues)


def test_diameter_matches_circle_in_orthogonal_view():
    # Front view labels Ø40; top view shows a circle of radius 20px at
    # scale 1.0 mm/px → Ø40. They correspond.
    front = ViewGeometry(label="Спереди", projection="front", scale=1.0, diameters_mm=[40.0])
    top = ViewGeometry(label="Сверху", projection="top", scale=1.0,
                       circles=[ViewCircle(cx=50, cy=50, r=20)])
    graph = build_correspondence_graph([front, top])
    assert "diameter" in _kinds(graph)


def test_diameter_mismatch_no_correspondence():
    front = ViewGeometry(label="Спереди", projection="front", scale=1.0, diameters_mm=[40.0])
    top = ViewGeometry(label="Сверху", projection="top", scale=1.0,
                       circles=[ViewCircle(cx=50, cy=50, r=5)])  # Ø10, not Ø40
    graph = build_correspondence_graph([front, top])
    assert "diameter" not in _kinds(graph)


def test_hidden_contour_matches_visible_circle():
    side = ViewGeometry(label="Сбоку", projection="side", has_hidden=True)
    front = ViewGeometry(label="Спереди", projection="front",
                         circles=[ViewCircle(cx=10, cy=10, r=5)])
    graph = build_correspondence_graph([side, front])
    assert "hidden_visible" in _kinds(graph)


def test_confirmed_view_pairs_collects_confirming_edges():
    front = ViewGeometry(label="Спереди", projection="front", scale=1.0,
                         diameters_mm=[40.0], bbox=(0, 0, 100, 60))
    top = ViewGeometry(label="Сверху", projection="top", scale=1.0,
                       circles=[ViewCircle(cx=50, cy=50, r=20)], bbox=(0, 80, 100, 140))
    graph = build_correspondence_graph([front, top])
    assert tuple(sorted(("Спереди", "Сверху"))) in graph.confirmed_view_pairs
    # scale edges do not count as a confirming pair
    scale_only = build_correspondence_graph([
        ViewGeometry(label="X", projection="front", scale=0.5),
        ViewGeometry(label="Y", projection="isometric", scale=0.5),
    ])
    assert scale_only.confirmed_view_pairs == set()
