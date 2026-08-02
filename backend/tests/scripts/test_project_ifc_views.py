from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from project_ifc_views import _edge_indices, _round_point


def test_edge_indices_prefers_explicit_topology():
    geometry = SimpleNamespace(edges=(0, 1, 1, 2), faces=(0, 2, 3))
    assert _edge_indices(geometry) == {(0, 1), (1, 2)}


def test_edge_indices_falls_back_to_triangle_topology():
    geometry = SimpleNamespace(edges=(), faces=(0, 1, 2, 2, 1, 3))
    assert _edge_indices(geometry) == {(0, 1), (0, 2), (1, 2), (1, 3), (2, 3)}


def test_round_point_selects_projection_axes():
    assert _round_point([1.1234567, 2.0, 3.5], 0, (0, 2)) == [1.123457, 3.5]
