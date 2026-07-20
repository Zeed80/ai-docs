#!/usr/bin/env python3
"""Project exact STEP topology into orthographic 2D primitive observations.

This script runs inside the FreeCAD/OpenCascade container.  It intentionally
does not use TechDraw: the headless FreeCAD 0.19 package crashes while loading
that workbench.  Full circles parallel to the view remain circles; all other
curves are sampled into honest line segments.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import Any

VIEWS = {
    "front": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "top": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "side": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
}


def _dot(point: Any, axis: tuple[int, int, int]) -> float:
    return point.x * axis[0] + point.y * axis[1] + point.z * axis[2]


def _point_2d(
    point: Any,
    horizontal: tuple[int, int, int],
    vertical: tuple[int, int, int],
) -> list[float]:
    return [round(_dot(point, horizontal), 6), round(_dot(point, vertical), 6)]


def _segment_key(p1: list[float], p2: list[float]) -> tuple[int, ...]:
    left = (round(p1[0], 4), round(p1[1], 4))
    right = (round(p2[0], 4), round(p2[1], 4))
    ordered = sorted((left, right))
    return tuple(round(value * 10_000) for point in ordered for value in point)


def _view_primitives(edges: list[Any], view_axes) -> list[dict[str, Any]]:
    horizontal, vertical, direction = view_axes
    primitives: list[dict[str, Any]] = []
    seen_segments: set[tuple[int, ...]] = set()
    seen_circles: set[tuple[int, ...]] = set()
    for edge in edges:
        curve_name = type(edge.Curve).__name__
        if curve_name == "Circle" and edge.isClosed():
            axis = edge.Curve.Axis
            alignment = abs(_dot(axis, direction))
            if alignment >= 0.999:
                center = _point_2d(edge.Curve.Center, horizontal, vertical)
                radius = round(float(edge.Curve.Radius), 6)
                key = (
                    round(center[0] * 10_000),
                    round(center[1] * 10_000),
                    round(radius * 10_000),
                )
                if key not in seen_circles and radius > 1e-6:
                    seen_circles.add(key)
                    primitives.append(
                        {"type": "circle", "center": center, "radius": radius}
                    )
                continue

        if curve_name == "Line":
            points = [vertex.Point for vertex in edge.Vertexes]
        else:
            length = max(float(edge.Length), 1.0)
            point_count = max(8, min(64, math.ceil(length / 2.0)))
            points = edge.discretize(Number=point_count)
        projected = [_point_2d(point, horizontal, vertical) for point in points]
        for p1, p2 in zip(projected, projected[1:]):
            if math.dist(p1, p2) <= 1e-6:
                continue
            key = _segment_key(p1, p2)
            if key in seen_segments:
                continue
            seen_segments.add(key)
            primitives.append({"type": "segment", "p1": p1, "p2": p2})
    return primitives


def project_file(path: pathlib.Path) -> dict[str, Any]:
    import FreeCAD
    import Import

    document = FreeCAD.newDocument(f"step_{path.stem[:40]}")
    try:
        Import.insert(str(path), document.Name)
        document.recompute()
        shapes = [
            obj.Shape
            for obj in document.Objects
            if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull()
        ]
        edges = [edge for shape in shapes for edge in shape.Edges]
        if not edges:
            raise ValueError("STEP contains no topological edges")
        return {
            "source": str(path),
            "views": {
                name: _view_primitives(edges, axes)
                for name, axes in VIEWS.items()
            },
        }
    finally:
        FreeCAD.closeDocument(document.Name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=pathlib.Path)
    parser.add_argument("--file", type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if (args.source is None) == (args.file is None):
        parser.error("provide exactly one of --source or --file")
    args.out.mkdir(parents=True, exist_ok=True)
    failures = []
    projected = 0
    paths = [args.file] if args.file else sorted(args.source.glob("*.step"))
    for path in paths:
        try:
            payload = project_file(path)
            (args.out / f"{path.stem}.json").write_text(json.dumps(payload))
            projected += 1
        except Exception as exc:  # noqa: BLE001
            failures.append({"source": str(path), "error": f"{type(exc).__name__}: {exc}"})
    summary = {"projected": projected, "failed": len(failures), "failures": failures}
    if args.source:
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return 0 if projected else 1


if __name__ == "__main__":
    sys.exit(main())
