from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_brep_pair import (
    _axis_orientations,
    _combine_shapes,
    _count_score,
    _determinant3,
    _promotion_decision,
    _relative_error,
)


def _passing_metrics():
    return {
        "reference_valid": True,
        "candidate_valid": True,
        "solid_count_match": True,
        "bounding_box_max_relative_error": 0.001,
        "volume_relative_error": 0.002,
        "surface_max_distance_normalized": 0.001,
        "volume_iou": 0.995,
        "topology_exact": True,
    }


def test_relative_error_and_count_score_are_scale_safe():
    assert _relative_error(99, 100) == 0.01
    assert _count_score(9, 10) == 0.9
    assert _count_score(15, 10) == 0.5


def test_promotion_requires_every_independent_brep_gate():
    eligible, failures = _promotion_decision(_passing_metrics())
    assert eligible is True
    assert failures == []


def test_valid_brep_with_wrong_overlap_cannot_be_promoted():
    metrics = _passing_metrics()
    metrics["volume_iou"] = 0.4
    eligible, failures = _promotion_decision(metrics)
    assert eligible is False
    assert failures == ["overlap_mismatch"]


def test_good_envelope_cannot_hide_wrong_topology():
    metrics = _passing_metrics()
    metrics["topology_exact"] = False
    eligible, failures = _promotion_decision(metrics)
    assert eligible is False
    assert failures == ["topology_mismatch"]


def test_single_shape_is_not_wrapped_in_crashing_compound_helper():
    shape = object()

    class FakePart:
        @staticmethod
        def Compound(shapes):
            return ("compound", shapes)

    assert _combine_shapes(FakePart, [shape]) is shape
    assert _combine_shapes(FakePart, [shape, shape]) == ("compound", [shape, shape])


def test_axis_orientations_are_24_unique_proper_rotations():
    orientations = _axis_orientations()
    assert len(orientations) == 24
    assert len(set(orientations)) == 24
    assert orientations[0] == ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    assert {_determinant3(matrix) for matrix in orientations} == {1}
    for matrix in orientations:
        assert all(sum(abs(value) for value in row) == 1 for row in matrix)
        assert all(sum(abs(matrix[row][column]) for row in range(3)) == 1 for column in range(3))
