#!/usr/bin/env python3
"""Project IFC product geometry into semantic plan/front/side edge observations."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any

VIEWS = {
    "plan": (0, 1),
    "front": (0, 2),
    "side": (1, 2),
}


def _round_point(vertices: list[float], index: int, axes: tuple[int, int]) -> list[float]:
    base = index * 3
    return [round(float(vertices[base + axis]), 6) for axis in axes]


def _edge_indices(geometry: Any) -> set[tuple[int, int]]:
    raw_edges = list(getattr(geometry, "edges", ()) or ())
    edges = {
        tuple(sorted((int(raw_edges[index]), int(raw_edges[index + 1]))))
        for index in range(0, len(raw_edges) - 1, 2)
    }
    if edges:
        return edges
    faces = list(getattr(geometry, "faces", ()) or ())
    for index in range(0, len(faces) - 2, 3):
        triangle = [int(value) for value in faces[index:index + 3]]
        edges.update({
            tuple(sorted((triangle[0], triangle[1]))),
            tuple(sorted((triangle[1], triangle[2]))),
            tuple(sorted((triangle[2], triangle[0]))),
        })
    return edges


def project_ifc(path: pathlib.Path) -> dict[str, Any]:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element

    model = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    views: dict[str, list[dict[str, Any]]] = {name: [] for name in VIEWS}
    elements = []
    failures = []
    for product in model.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            continue
        guid = str(getattr(product, "GlobalId", ""))
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = list(shape.geometry.verts)
            edges = _edge_indices(shape.geometry)
            if not vertices or not edges:
                raise ValueError("empty tessellation")
        except Exception as exc:  # noqa: BLE001
            failures.append({
                "guid": guid,
                "ifc_class": product.is_a(),
                "error": f"{type(exc).__name__}:{exc}",
            })
            continue
        container = ifcopenshell.util.element.get_container(product)
        elements.append({
            "guid": guid,
            "ifc_class": product.is_a(),
            "name": str(getattr(product, "Name", "") or ""),
            "container_guid": str(getattr(container, "GlobalId", "") or ""),
            "edge_count": len(edges),
        })
        for view_name, axes in VIEWS.items():
            seen: set[tuple[float, ...]] = set()
            for left, right in edges:
                p1 = _round_point(vertices, left, axes)
                p2 = _round_point(vertices, right, axes)
                if p1 == p2:
                    continue
                ordered = sorted((p1, p2))
                key = tuple(value for point in ordered for value in point)
                if key in seen:
                    continue
                seen.add(key)
                views[view_name].append({
                    "type": "segment",
                    "p1": p1,
                    "p2": p2,
                    "element_guid": guid,
                    "ifc_class": product.is_a(),
                })
    return {
        "schema_version": 1,
        "source": str(path),
        "ifc_schema": model.schema,
        "elements": elements,
        "class_counts": dict(sorted(Counter(item["ifc_class"] for item in elements).items())),
        "views": views,
        "geometry_failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    payload = project_ifc(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False))
    print(json.dumps({
        "schema": payload["ifc_schema"],
        "elements": len(payload["elements"]),
        "geometry_failures": len(payload["geometry_failures"]),
        "view_segments": {key: len(value) for key, value in payload["views"].items()},
    }, ensure_ascii=False))
    return 0 if payload["elements"] else 1


if __name__ == "__main__":
    sys.exit(main())
