from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from project_ifc_views import (
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
    class AlternatingTree:
        calls = 0

        def select_ray(self, origin, direction, length):
            self.calls += 1
            distance = 10.0 if self.calls == 2 else 20.0
            return [SimpleNamespace(position=(origin[0], origin[1], origin[2] - distance))]

    parts = _visible_edge_parts(
        AlternatingTree(), (0, 0, 0), (3, 0, 0), (0, 0, 1), 20, 1e-6
    )
    assert [part[2] for part in parts] == ["visible", "hidden", "visible"]
    assert parts[0][:2] == ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
