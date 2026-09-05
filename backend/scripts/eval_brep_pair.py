#!/usr/bin/env python3
"""Compare a reconstructed STEP B-Rep with independent STEP ground truth.

Run this script in the CAD-kernel image.  The evaluator deliberately uses
OpenCascade solids rather than rendered views: validity, topology, mass
properties, boolean overlap and bidirectional surface distance are separate
release signals.  A good-looking projection cannot pass this gate by itself.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import pathlib
import sys
from typing import Any


def _relative_error(actual: float, expected: float) -> float:
    denominator = max(abs(expected), 1e-12)
    return abs(actual - expected) / denominator


def _count_score(actual: int, expected: int) -> float:
    return max(0.0, 1.0 - _relative_error(float(actual), float(expected)))


def _combine_shapes(part_module: Any, shapes: list[Any]) -> Any:
    if not shapes:
        raise ValueError("STEP contains no shapes")
    return shapes[0] if len(shapes) == 1 else part_module.Compound(shapes)


def _determinant3(matrix: tuple[tuple[int, int, int], ...]) -> int:
    return (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )


def _axis_orientations() -> list[tuple[tuple[int, int, int], ...]]:
    """The 24 proper signed axis permutations; reflections are excluded."""

    orientations = []
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            rows = tuple(
                tuple(signs[row] if column == permutation[row] else 0 for column in range(3))
                for row in range(3)
            )
            if _determinant3(rows) == 1:
                orientations.append(rows)
    identity = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    return [identity, *sorted(item for item in orientations if item != identity)]


def _orientation_label(matrix: tuple[tuple[int, int, int], ...]) -> str:
    return "/".join(",".join(str(value) for value in row) for row in matrix)


def _promotion_decision(metrics: dict[str, Any]) -> tuple[bool, list[str]]:
    checks = {
        "reference_brep_invalid": metrics["reference_valid"],
        "candidate_brep_invalid": metrics["candidate_valid"],
        "solid_count_mismatch": metrics["solid_count_match"],
        "bounding_box_mismatch": metrics["bounding_box_max_relative_error"] <= 0.01,
        "volume_mismatch": metrics["volume_relative_error"] <= 0.01,
        "surface_mismatch": metrics["surface_max_distance_normalized"] <= 0.005,
        "overlap_mismatch": metrics["volume_iou"] >= 0.98,
        "topology_mismatch": metrics["topology_exact"],
    }
    failures = [code for code, passed in checks.items() if not passed]
    return not failures, failures


def _load_shape(path: pathlib.Path):
    import FreeCAD
    import Import
    import Part

    document = FreeCAD.newDocument(f"brep_eval_{path.stem[:32]}")
    try:
        Import.insert(str(path), document.Name)
        document.recompute()
        shapes = [
            obj.Shape.copy()
            for obj in document.Objects
            if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull()
        ]
        # FreeCAD 1.1's Python ``Part.makeCompound`` helper segfaults for a
        # valid imported STEP in the production CAD image.  Avoid wrapping a
        # single shape and use the native constructor for real assemblies.
        return _combine_shapes(Part, shapes)
    finally:
        FreeCAD.closeDocument(document.Name)


def _descriptor(shape: Any) -> dict[str, Any]:
    bounds = shape.BoundBox
    return {
        "valid": bool(shape.isValid()),
        "solid_count": len(shape.Solids),
        "shell_count": len(shape.Shells),
        "face_count": len(shape.Faces),
        "edge_count": len(shape.Edges),
        "vertex_count": len(shape.Vertexes),
        "volume": float(shape.Volume),
        "surface_area": float(shape.Area),
        "bbox": [float(bounds.XLength), float(bounds.YLength), float(bounds.ZLength)],
        "center": [float(bounds.Center.x), float(bounds.Center.y), float(bounds.Center.z)],
        "diagonal": math.sqrt(bounds.XLength**2 + bounds.YLength**2 + bounds.ZLength**2),
    }


def _center_on(candidate: Any, reference_descriptor: dict, candidate_descriptor: dict):
    import FreeCAD

    aligned = candidate.copy()
    delta = [
        reference_descriptor["center"][axis] - candidate_descriptor["center"][axis]
        for axis in range(3)
    ]
    aligned.translate(FreeCAD.Vector(*delta))
    return aligned


def _orient_and_center(candidate: Any, orientation: tuple, reference_descriptor: dict):
    import FreeCAD

    transform = FreeCAD.Matrix()
    for row in range(3):
        for column in range(3):
            setattr(transform, f"A{row + 1}{column + 1}", orientation[row][column])
    # ``transformGeometry`` rebuilds analytic surfaces and changed volume in a
    # live FreeCAD 1.1 STEP test.  A signed axis permutation is rigid, so keep
    # the exact B-Rep topology and location transform instead.
    oriented = candidate.copy()
    oriented.transformShape(transform, False, False)
    return _center_on(oriented, reference_descriptor, _descriptor(oriented))


def _volume_iou(reference: Any, candidate: Any, reference_volume: float) -> float:
    intersection_volume = 0.0
    try:
        intersection_volume = float(reference.common(candidate).Volume)
    except Exception:  # noqa: BLE001 - a failed boolean is a failed overlap signal
        pass
    union_volume = reference_volume + float(candidate.Volume) - intersection_volume
    raw_iou = intersection_volume / union_volume if union_volume > 1e-12 else 0.0
    return min(1.0, max(0.0, raw_iou))


def _sample_surface(shape: Any, deflection: float, limit: int = 512) -> list[Any]:
    vertices, _ = shape.tessellate(max(deflection, 1e-5))
    if len(vertices) <= limit:
        return list(vertices)
    step = len(vertices) / limit
    return [vertices[min(len(vertices) - 1, int(index * step))] for index in range(limit)]


def _directed_surface_distances(source: Any, target: Any, deflection: float) -> list[float]:
    import Part

    distances = []
    for point in _sample_surface(source, deflection):
        distance, _, _ = target.distToShape(Part.Vertex(point))
        distances.append(float(distance))
    return distances


def evaluate_pair(reference_path: pathlib.Path, candidate_path: pathlib.Path) -> dict[str, Any]:
    reference = _load_shape(reference_path)
    candidate = _load_shape(candidate_path)
    reference_descriptor = _descriptor(reference)
    candidate_descriptor = _descriptor(candidate)
    orientation_trials = []
    for orientation in _axis_orientations():
        trial = _orient_and_center(candidate, orientation, reference_descriptor)
        trial_descriptor = _descriptor(trial)
        bbox_errors = [
            _relative_error(trial_descriptor["bbox"][axis], reference_descriptor["bbox"][axis])
            for axis in range(3)
        ]
        orientation_trials.append(
            {
                "orientation": orientation,
                "shape": trial,
                "descriptor": trial_descriptor,
                "bbox_max_relative_error": max(bbox_errors),
                "volume_iou": _volume_iou(reference, trial, reference_descriptor["volume"]),
            }
        )
    best_trial = max(
        orientation_trials,
        key=lambda item: (
            item["volume_iou"],
            -item["bbox_max_relative_error"],
            item["orientation"] == ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ),
    )
    aligned_candidate = best_trial["shape"]
    aligned_descriptor = _descriptor(aligned_candidate)

    bbox_errors = [
        _relative_error(aligned_descriptor["bbox"][axis], reference_descriptor["bbox"][axis])
        for axis in range(3)
    ]
    volume_iou = best_trial["volume_iou"]

    diagonal = max(reference_descriptor["diagonal"], 1e-9)
    deflection = diagonal / 200
    distances = _directed_surface_distances(
        reference, aligned_candidate, deflection
    ) + _directed_surface_distances(aligned_candidate, reference, deflection)
    surface_max = max(distances, default=math.inf)
    surface_mean = sum(distances) / len(distances) if distances else math.inf
    topology_fields = ("solid_count", "shell_count", "face_count", "edge_count", "vertex_count")
    topology_exact = all(
        reference_descriptor[field] == aligned_descriptor[field] for field in topology_fields
    )
    metrics = {
        "reference_valid": reference_descriptor["valid"],
        "candidate_valid": aligned_descriptor["valid"],
        "solid_count_match": (
            reference_descriptor["solid_count"] == aligned_descriptor["solid_count"]
        ),
        "topology_exact": topology_exact,
        "topology_score": sum(
            _count_score(aligned_descriptor[field], reference_descriptor[field])
            for field in topology_fields
        )
        / len(topology_fields),
        "bounding_box_relative_errors": bbox_errors,
        "bounding_box_max_relative_error": max(bbox_errors),
        "volume_relative_error": _relative_error(
            aligned_descriptor["volume"], reference_descriptor["volume"]
        ),
        "surface_area_relative_error": _relative_error(
            aligned_descriptor["surface_area"], reference_descriptor["surface_area"]
        ),
        "volume_iou": volume_iou,
        "surface_mean_distance": surface_mean,
        "surface_max_distance": surface_max,
        "surface_mean_distance_normalized": surface_mean / diagonal,
        "surface_max_distance_normalized": surface_max / diagonal,
    }
    promotion_eligible, failures = _promotion_decision(metrics)
    return {
        "schema_version": 1,
        "reference": str(reference_path),
        "candidate": str(candidate_path),
        "alignment": "24_proper_axis_orientations_then_bbox_center_translation",
        "selected_orientation": _orientation_label(best_trial["orientation"]),
        "orientation_trials": [
            {
                "orientation": _orientation_label(item["orientation"]),
                "bbox_max_relative_error": item["bbox_max_relative_error"],
                "volume_iou": item["volume_iou"],
            }
            for item in orientation_trials
        ],
        "reference_descriptor": reference_descriptor,
        "candidate_descriptor": candidate_descriptor,
        "metrics": metrics,
        "promotion_eligible": promotion_eligible,
        "failures": failures,
        "limitations": [
            "Only the 24 proper axis orientations are normalized; arbitrary rotations are not.",
            "Surface distance uses deterministic tessellation sampling, "
            "not analytic Hausdorff distance.",
            "PMI and feature intent require separate semantic evaluators.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=pathlib.Path)
    parser.add_argument("candidate", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    report = evaluate_pair(args.reference, args.candidate)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered)
    return 0 if report["promotion_eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
