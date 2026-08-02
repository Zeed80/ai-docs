#!/usr/bin/env python3
"""Project IFC product geometry into semantic plan/front/side edge observations."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import Counter, defaultdict
from typing import Any

VIEWS = {
    "plan": {"axes": (0, 1), "direction": (0.0, 0.0, 1.0)},
    "front": {"axes": (0, 2), "direction": (0.0, 1.0, 0.0)},
    "side": {"axes": (1, 2), "direction": (1.0, 0.0, 0.0)},
}

SECTION_HEIGHT_M = 1.2


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


def _vertex(vertices: list[float], index: int) -> tuple[float, float, float]:
    base = index * 3
    return tuple(float(vertices[base + offset]) for offset in range(3))  # type: ignore[return-value]


def _triangle_normal(
    vertices: list[float], triangle: tuple[int, int, int]
) -> tuple[float, float, float] | None:
    p0, p1, p2 = (_vertex(vertices, index) for index in triangle)
    left = tuple(p1[index] - p0[index] for index in range(3))
    right = tuple(p2[index] - p0[index] for index in range(3))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    length = math.sqrt(sum(value * value for value in cross))
    if length <= 1e-12:
        return None
    return tuple(value / length for value in cross)  # type: ignore[return-value]


def _drawing_edges(
    vertices: list[float],
    faces: list[int],
    direction: tuple[float, float, float],
    *,
    sharp_angle_deg: float = 30.0,
) -> dict[tuple[int, int], str]:
    """Drop coplanar mesh diagonals and retain boundaries/features/silhouettes."""

    edge_normals: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for index in range(0, len(faces) - 2, 3):
        triangle = tuple(int(value) for value in faces[index:index + 3])
        normal = _triangle_normal(vertices, triangle)
        if normal is None:
            continue
        for left, right in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            edge_normals[tuple(sorted((left, right)))].append(normal)
    selected = {}
    sharp_cos = math.cos(math.radians(sharp_angle_deg))
    for edge, normals in edge_normals.items():
        if len(normals) == 1:
            selected[edge] = "boundary"
            continue
        first, second = normals[0], normals[1]
        facing = (
            sum(first[index] * direction[index] for index in range(3)),
            sum(second[index] * direction[index] for index in range(3)),
        )
        if facing[0] * facing[1] < -1e-9:
            selected[edge] = "silhouette"
            continue
        normal_dot = sum(first[index] * second[index] for index in range(3))
        if normal_dot < sharp_cos:
            selected[edge] = "feature"
    return selected


def _lerp3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
    fraction: float,
) -> tuple[float, float, float]:
    return tuple(
        left[index] + (right[index] - left[index]) * fraction
        for index in range(3)
    )  # type: ignore[return-value]


def _section_segments(
    vertices: list[float],
    faces: list[int],
    elevation: float,
    *,
    tolerance: float = 1e-7,
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Intersect a tessellated solid with a horizontal drawing cut plane."""

    segments = []
    seen: set[tuple[float, ...]] = set()
    for index in range(0, len(faces) - 2, 3):
        triangle = [_vertex(vertices, int(value)) for value in faces[index:index + 3]]
        intersections: list[tuple[float, float, float]] = []
        triangle_edges = (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        )
        for start, end in triangle_edges:
            start_delta, end_delta = start[2] - elevation, end[2] - elevation
            if abs(start_delta) <= tolerance and abs(end_delta) <= tolerance:
                continue  # coplanar faces do not define a section boundary
            if start_delta * end_delta > 0:
                continue
            denominator = end[2] - start[2]
            if abs(denominator) <= tolerance:
                continue
            fraction = (elevation - start[2]) / denominator
            if -tolerance <= fraction <= 1 + tolerance:
                point = _lerp3(start, end, min(1.0, max(0.0, fraction)))
                if not any(
                    sum((point[axis] - other[axis]) ** 2 for axis in range(3)) <= tolerance ** 2
                    for other in intersections
                ):
                    intersections.append(point)
        if len(intersections) != 2:
            continue
        ordered = sorted(intersections)
        key = tuple(round(value, 7) for point in ordered for value in point)
        if key in seen:
            continue
        seen.add(key)
        segments.append((ordered[0], ordered[1]))
    # Tessellation splits a straight wall cut at every triangle diagonal.
    # Merge only touching collinear spans; real corners remain separate.
    changed = True
    while changed:
        changed = False
        for left_index, (left_start, left_end) in enumerate(segments):
            for right_index in range(left_index + 1, len(segments)):
                right_start, right_end = segments[right_index]
                shared = None
                for left_point in (left_start, left_end):
                    for right_point in (right_start, right_end):
                        if sum(
                            (left_point[axis] - right_point[axis]) ** 2
                            for axis in range(3)
                        ) <= tolerance ** 2:
                            shared = (left_point, right_point)
                            break
                    if shared:
                        break
                if not shared:
                    continue
                outer_left = left_end if shared[0] == left_start else left_start
                outer_right = right_end if shared[1] == right_start else right_start
                first = tuple(shared[0][axis] - outer_left[axis] for axis in range(3))
                second = tuple(outer_right[axis] - shared[0][axis] for axis in range(3))
                cross = (
                    first[1] * second[2] - first[2] * second[1],
                    first[2] * second[0] - first[0] * second[2],
                    first[0] * second[1] - first[1] * second[0],
                )
                if math.sqrt(sum(value * value for value in cross)) > tolerance:
                    continue
                merged_segment = tuple(sorted((outer_left, outer_right)))
                segments[left_index] = merged_segment  # type: ignore[assignment]
                segments.pop(right_index)
                changed = True
                break
            if changed:
                break
    return segments


def _ray_visible(
    tree: Any,
    point: tuple[float, float, float],
    direction: tuple[float, float, float],
    ray_offset: float,
    tolerance: float,
) -> bool:
    """Return whether ``point`` is the first surface seen by an orthographic ray."""

    origin = tuple(point[index] + direction[index] * ray_offset for index in range(3))
    ray_direction = tuple(-value for value in direction)
    hits = tree.select_ray(origin, ray_direction, ray_offset * 2)
    if not hits:
        # Exact boundary rays may miss both adjacent faces. Keeping the source
        # edge is safer than inventing an occluder in that numerical case.
        return True
    nearest = min(
        math.sqrt(sum((float(hit.position[index]) - origin[index]) ** 2 for index in range(3)))
        for hit in hits
    )
    return nearest >= ray_offset - tolerance


def _visible_edge_parts(
    tree: Any,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    direction: tuple[float, float, float],
    ray_offset: float,
    tolerance: float,
    *,
    samples: int = 3,
) -> list[tuple[tuple[float, float, float], tuple[float, float, float], str]]:
    """Split an edge into independently tested visible/hidden spans."""

    parts = []
    for index in range(samples):
        left_fraction = index / samples
        right_fraction = (index + 1) / samples
        midpoint = _lerp3(start, end, (left_fraction + right_fraction) / 2)
        visibility = "visible" if _ray_visible(
            tree, midpoint, direction, ray_offset, tolerance
        ) else "hidden"
        parts.append((
            _lerp3(start, end, left_fraction),
            _lerp3(start, end, right_fraction),
            visibility,
        ))
    merged = []
    for start_part, end_part, visibility in parts:
        if merged and merged[-1][2] == visibility:
            merged[-1] = (merged[-1][0], end_part, visibility)
        else:
            merged.append((start_part, end_part, visibility))
    return merged


def project_ifc(path: pathlib.Path) -> dict[str, Any]:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element
    import ifcopenshell.util.placement
    import ifcopenshell.util.unit

    model = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    # Geometry output is SI by default; placement utilities expose project
    # coordinates, so storey elevations must be normalized to metres.
    unit_scale = float(ifcopenshell.util.unit.calculate_unit_scale(model) or 1.0)
    views: dict[str, list[dict[str, Any]]] = {name: [] for name in VIEWS}
    views["plan_section"] = []
    elements = []
    failures = []
    meshes = []
    tree = ifcopenshell.geom.tree()
    for product in model.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            continue
        guid = str(getattr(product, "GlobalId", ""))
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = list(shape.geometry.verts)
            faces = [int(value) for value in shape.geometry.faces]
            if not vertices or not faces:
                raise ValueError("empty tessellation")
        except Exception as exc:  # noqa: BLE001
            failures.append({
                "guid": guid,
                "ifc_class": product.is_a(),
                "error": f"{type(exc).__name__}:{exc}",
            })
            continue
        container = ifcopenshell.util.element.get_container(product)
        storey = ifcopenshell.util.element.get_container(product, ifc_class="IfcBuildingStorey")
        storey_elevation_m = (
            float(ifcopenshell.util.placement.get_storey_elevation(storey)) * unit_scale
            if storey is not None else None
        )
        elements.append({
            "guid": guid,
            "ifc_class": product.is_a(),
            "name": str(getattr(product, "Name", "") or ""),
            "container_guid": str(getattr(container, "GlobalId", "") or ""),
            "storey_guid": str(getattr(storey, "GlobalId", "") or ""),
            "storey_elevation_m": storey_elevation_m,
            "triangle_count": len(faces) // 3,
        })
        tree.add_element(shape)
        meshes.append((product, vertices, faces, container, storey, storey_elevation_m))

    all_vertices = [
        _vertex(vertices, index)
        for _, vertices, _, _, _, _ in meshes
        for index in range(len(vertices) // 3)
    ]
    if all_vertices:
        bounds = [
            (min(point[axis] for point in all_vertices), max(point[axis] for point in all_vertices))
            for axis in range(3)
        ]
        diagonal = math.sqrt(sum((maximum - minimum) ** 2 for minimum, maximum in bounds))
    else:
        diagonal = 1.0
    ray_offset = max(1.0, diagonal * 2)
    visibility_tolerance = max(1e-5, diagonal * 1e-6)
    for product, vertices, faces, container, storey, storey_elevation in meshes:
        guid = str(getattr(product, "GlobalId", ""))
        for view_name, view in VIEWS.items():
            axes = view["axes"]
            edges = _drawing_edges(vertices, faces, view["direction"])
            seen: set[tuple[float, ...]] = set()
            for (left, right), edge_kind in edges.items():
                start, end = _vertex(vertices, left), _vertex(vertices, right)
                for visible_start, visible_end, visibility in _visible_edge_parts(
                    tree, start, end, view["direction"], ray_offset, visibility_tolerance
                ):
                    p1 = [round(visible_start[axis], 6) for axis in axes]
                    p2 = [round(visible_end[axis], 6) for axis in axes]
                    if p1 == p2:
                        continue
                    ordered = sorted((p1, p2))
                    key = tuple(value for point in ordered for value in point) + (visibility,)
                    if key in seen:
                        continue
                    seen.add(key)
                    views[view_name].append({
                        "type": "segment",
                        "p1": p1,
                        "p2": p2,
                        "element_guid": guid,
                        "ifc_class": product.is_a(),
                        "edge_kind": edge_kind,
                        "visibility": visibility,
                    })

        if storey is not None and storey_elevation is not None:
            section_elevation = storey_elevation + SECTION_HEIGHT_M
            for start, end in _section_segments(vertices, faces, section_elevation):
                views["plan_section"].append({
                    "type": "segment",
                    "p1": [round(start[0], 6), round(start[1], 6)],
                    "p2": [round(end[0], 6), round(end[1], 6)],
                    "element_guid": guid,
                    "ifc_class": product.is_a(),
                    "edge_kind": "section",
                    "visibility": "visible",
                    "section_plane": round(section_elevation, 6),
                    "storey_guid": str(getattr(storey, "GlobalId", "") or ""),
                })
    return {
        "schema_version": 2,
        "source": str(path),
        "ifc_schema": model.schema,
        "elements": elements,
        "class_counts": dict(sorted(Counter(item["ifc_class"] for item in elements).items())),
        "views": views,
        "projection": {
            "hidden_line_method": "ifcopenshell_geometry_tree_ray_cast",
            "visibility_samples_per_edge": 3,
            "section_height_m": SECTION_HEIGHT_M,
            "project_unit_scale_to_m": unit_scale,
        },
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
