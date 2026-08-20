from __future__ import annotations

from types import SimpleNamespace

# Ф5.1 moved the projection logic out of scripts/project_ifc_views.py (now a
# thin CLI shell around app.ai.ifc_reader.project_ifc, see its docstring) and
# into app.ai.ifc_reader so it's importable from a service, not just the
# offline script — this test module's import was never updated to follow.
from app.ai.ifc_reader import (
    _depth_visible,
    _drawing_edges,
    _edge_indices,
    _section_segments,
    _visible_edge_parts,
    _round_point,
)


def test_edge_indices_prefers_explicit_topology():
    geometry = SimpleNamespace(edges=(0, 1, 1, 2), faces=(0, 2, 3))
    assert _edge_indices(geometry) == {(0, 1), (1, 2)}


def test_edge_indices_falls_back_to_triangle_topology():
    geometry = SimpleNamespace(edges=(), faces=(0, 1, 2, 2, 1, 3))
    assert _edge_indices(geometry) == {(0, 1), (0, 2), (1, 2), (1, 3), (2, 3)}


def test_round_point_selects_projection_axes():
    assert _round_point([1.1234567, 2.0, 3.5], 0, (0, 2)) == [1.123457, 3.5]


def test_drawing_edges_remove_coplanar_triangle_diagonal():
    vertices = [
        0, 0, 0,
        1, 0, 0,
        1, 1, 0,
        0, 1, 0,
    ]
    edges = _drawing_edges(vertices, [0, 1, 2, 0, 2, 3], (0, 0, 1))
    assert set(edges) == {(0, 1), (1, 2), (2, 3), (0, 3)}
    assert set(edges.values()) == {"boundary"}


def test_drawing_edges_keep_sharp_feature():
    vertices = [
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    ]
    edges = _drawing_edges(vertices, [0, 1, 2, 0, 3, 1], (0, 1, 0))
    assert edges[(0, 1)] in {"feature", "silhouette"}


def test_section_segments_cut_vertical_faces_without_coplanar_diagonals():
    vertices = [
        0, 0, 0,
        1, 0, 0,
        1, 0, 2,
        0, 0, 2,
    ]
    segments = _section_segments(vertices, [0, 1, 2, 0, 2, 3], 1.2)
    assert segments == [((0.0, 0.0, 1.2), (1.0, 0.0, 1.2))]


def test_visible_edge_parts_keep_visibility_per_span():
    parts = _visible_edge_parts(
        (0, 0, 0),
        (3, 0, 0),
        lambda point, start, end: not 1.0 < point[0] < 2.0,
    )
    assert [part[2] for part in parts] == ["visible", "hidden", "visible"]
    assert parts[0][:2] == ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))


def test_depth_visibility_rejects_edge_behind_both_probe_sides():
    import numpy as np

    depth = np.full((16, 16), -np.inf, dtype=np.float32)
    depth[6:11, 6:11] = 2.0
    buffer = {
        "depth": depth,
        "axes": (0, 1),
        "direction": (0, 0, 1),
        "u_min": 0.0,
        "v_min": 0.0,
        "u_scale": 1.0,
        "v_scale": 1.0,
        "resolution": 16,
    }
    assert not _depth_visible(buffer, (7, 7, 1), (5, 7, 1), (9, 7, 1), 1e-6)
    assert _depth_visible(buffer, (7, 7, 3), (5, 7, 3), (9, 7, 3), 1e-6)
