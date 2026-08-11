"""IFC file readers: raw hidden-line projections, and the canonical ConstructionModel.

Фаза 5.1 of the CAD geometry roadmap: promotes what was an offline script
(scripts/project_ifc_views.py) into an importable module usable from a
service (FastAPI endpoint / Celery task), without duplicating the geometry
logic. ``project_ifc`` is relocated here verbatim; ``ifc_to_construction_model``
is new -- it reads an ARBITRARY external IFC file (not necessarily one this
project produced) into the same ``ConstructionModel`` shape that
``construction_as_graph`` already consumes, so the existing
POST /revisions/{id}/construction-model-graph machinery needs no changes.
"""

from __future__ import annotations

import math
import pathlib
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.ai.construction_emg import ConstructionModel

VIEWS = {
    "plan": {"axes": (0, 1), "direction": (0.0, 0.0, 1.0)},
    "front": {"axes": (0, 2), "direction": (0.0, 1.0, 0.0)},
    "side": {"axes": (1, 2), "direction": (1.0, 0.0, 0.0)},
}

SECTION_HEIGHT_M = 1.2

# ConstructionModel only models these five element kinds (see construction_emg.py).
# IfcDoor/IfcWindow are read as "opening" -- the schema has no door/window kind of
# its own, and a filled opening is functionally an opening for this graph's purposes.
_KIND_BY_IFC_CLASS = {
    "IfcWall": "wall",
    "IfcWallStandardCase": "wall",
    "IfcSlab": "slab",
    "IfcColumn": "column",
    "IfcSpace": "space",
    "IfcOpeningElement": "opening",
    "IfcDoor": "opening",
    "IfcWindow": "opening",
}


# ── Raw hidden-line projection (relocated from scripts/project_ifc_views.py) ───


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


def _visible_edge_parts(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    visible_at: Any,
    *,
    samples: int = 3,
) -> list[tuple[tuple[float, float, float], tuple[float, float, float], str]]:
    """Split an edge into independently tested visible/hidden spans."""

    parts = []
    for index in range(samples):
        left_fraction = index / samples
        right_fraction = (index + 1) / samples
        midpoint = _lerp3(start, end, (left_fraction + right_fraction) / 2)
        visibility = "visible" if visible_at(midpoint, start, end) else "hidden"
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


def _build_depth_buffer(
    meshes: list[tuple],
    axes: tuple[int, int],
    direction: tuple[float, float, float],
    bounds: list[tuple[float, float]],
    *,
    resolution: int = 1024,
) -> dict[str, Any]:
    """Rasterize scene triangles into an orthographic maximum-depth buffer."""

    import numpy as np

    u_min, u_max = bounds[axes[0]]
    v_min, v_max = bounds[axes[1]]
    u_scale = (resolution - 3) / max(u_max - u_min, 1e-12)
    v_scale = (resolution - 3) / max(v_max - v_min, 1e-12)
    depth = np.full((resolution, resolution), -np.inf, dtype=np.float32)
    for _, vertices, faces, _, _, _ in meshes:
        for index in range(0, len(faces) - 2, 3):
            triangle = [_vertex(vertices, int(value)) for value in faces[index:index + 3]]
            pixel = [
                (
                    1 + (point[axes[0]] - u_min) * u_scale,
                    1 + (point[axes[1]] - v_min) * v_scale,
                )
                for point in triangle
            ]
            x0 = max(0, int(math.floor(min(point[0] for point in pixel))))
            x1 = min(resolution - 1, int(math.ceil(max(point[0] for point in pixel))))
            y0 = max(0, int(math.floor(min(point[1] for point in pixel))))
            y1 = min(resolution - 1, int(math.ceil(max(point[1] for point in pixel))))
            if x1 < x0 or y1 < y0:
                continue
            px0, px1, px2 = pixel
            denominator = (
                (px1[1] - px2[1]) * (px0[0] - px2[0])
                + (px2[0] - px1[0]) * (px0[1] - px2[1])
            )
            if abs(denominator) <= 1e-9:
                continue
            grid_y, grid_x = np.mgrid[y0:y1 + 1, x0:x1 + 1]
            weight0 = (
                (px1[1] - px2[1]) * (grid_x - px2[0])
                + (px2[0] - px1[0]) * (grid_y - px2[1])
            ) / denominator
            weight1 = (
                (px2[1] - px0[1]) * (grid_x - px2[0])
                + (px0[0] - px2[0]) * (grid_y - px2[1])
            ) / denominator
            weight2 = 1.0 - weight0 - weight1
            inside = (weight0 >= -1e-5) & (weight1 >= -1e-5) & (weight2 >= -1e-5)
            if not inside.any():
                continue
            triangle_depth = [
                sum(point[axis] * direction[axis] for axis in range(3))
                for point in triangle
            ]
            values = (
                weight0 * triangle_depth[0]
                + weight1 * triangle_depth[1]
                + weight2 * triangle_depth[2]
            )
            target = depth[y0:y1 + 1, x0:x1 + 1]
            target[inside] = np.maximum(target[inside], values[inside])
    return {
        "depth": depth,
        "axes": axes,
        "direction": direction,
        "u_min": u_min,
        "v_min": v_min,
        "u_scale": u_scale,
        "v_scale": v_scale,
        "resolution": resolution,
    }


def _depth_visible(
    buffer: dict[str, Any],
    point: tuple[float, float, float],
    edge_start: tuple[float, float, float],
    edge_end: tuple[float, float, float],
    tolerance: float,
) -> bool:
    """Test both raster sides of an edge against the scene depth buffer."""

    import numpy as np

    axes = buffer["axes"]
    x = 1 + (point[axes[0]] - buffer["u_min"]) * buffer["u_scale"]
    y = 1 + (point[axes[1]] - buffer["v_min"]) * buffer["v_scale"]
    edge_x = (edge_end[axes[0]] - edge_start[axes[0]]) * buffer["u_scale"]
    edge_y = (edge_end[axes[1]] - edge_start[axes[1]]) * buffer["v_scale"]
    length = math.hypot(edge_x, edge_y)
    if length <= 1e-9:
        return False
    perpendicular = (-edge_y / length * 2.0, edge_x / length * 2.0)
    point_depth = sum(
        point[axis] * buffer["direction"][axis] for axis in range(3)
    )
    for sign in (-1, 1):
        px = int(round(x + sign * perpendicular[0]))
        py = int(round(y + sign * perpendicular[1]))
        if not (0 <= px < buffer["resolution"] and 0 <= py < buffer["resolution"]):
            return True
        observed = buffer["depth"][py, px]
        if not np.isfinite(observed) or float(observed) <= point_depth + tolerance:
            return True
    return False


def project_ifc(path: pathlib.Path) -> dict[str, Any]:
    """Project every IFC product into hidden-line plan/front/side + a plan section.

    Relocated verbatim from scripts/project_ifc_views.py (Ф5.1) -- used for
    viewer/drawing purposes, distinct from ifc_to_construction_model below.
    """
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
    visibility_tolerance = max(1e-5, diagonal * 1e-6)
    depth_buffers = {
        view_name: _build_depth_buffer(
            meshes, view["axes"], view["direction"], bounds
        )
        for view_name, view in VIEWS.items()
    }
    for product, vertices, faces, container, storey, storey_elevation in meshes:
        guid = str(getattr(product, "GlobalId", ""))
        for view_name, view in VIEWS.items():
            axes = view["axes"]
            edges = _drawing_edges(vertices, faces, view["direction"])
            seen: set[tuple[float, ...]] = set()
            for (left, right), edge_kind in edges.items():
                start, end = _vertex(vertices, left), _vertex(vertices, right)
                visibility_test = lambda point, edge_start, edge_end: _depth_visible(
                    depth_buffers[view_name],
                    point,
                    edge_start,
                    edge_end,
                    visibility_tolerance,
                )
                for visible_start, visible_end, visibility in _visible_edge_parts(
                    start,
                    end,
                    visibility_test,
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
            "hidden_line_method": "triangle_depth_buffer",
            "visibility_samples_per_edge": 3,
            "depth_buffer_resolution": 1024,
            "section_height_m": SECTION_HEIGHT_M,
            "project_unit_scale_to_m": unit_scale,
        },
        "geometry_failures": failures,
    }


# ── ConstructionModel reader (new, Ф5.1) ────────────────────────────────────


def ifc_to_construction_model(
    path: pathlib.Path,
    *,
    site_name: str | None = None,
    building_name: str | None = None,
) -> tuple[ConstructionModel | None, dict[str, Any]]:
    """Read an arbitrary IFC file into the project's canonical ConstructionModel.

    Fail-closed by construction:
    - Only IfcWall(StandardCase)/IfcSlab/IfcColumn/IfcSpace/IfcOpeningElement/
      IfcDoor/IfcWindow are mapped (the five ConstructionModel element kinds --
      IfcDoor/IfcWindow become "opening", the schema has no door/window kind).
      Every other IFC class is silently not modeled (not an error -- this
      graph domain doesn't cover e.g. IfcBeam/IfcRoof/IfcFurnishingElement yet).
    - An element whose geometry fails to tessellate, whose storey can't be
      resolved, or (for door/window/opening) whose host wall/slab can't be
      resolved via IfcRelVoidsElement/IfcRelFillsElement, is excluded and
      reported in ``report["skipped"]`` -- never guessed.
    - The box geometry is the element's axis-aligned bounding box in world
      coordinates (from the tessellated shape), not a placement heuristic --
      correct regardless of which authoring tool produced the file. A real
      building's ConstructionModel.validate_hierarchy may still reject the
      result (rotated walls whose openings don't corner-contain in an
      axis-aligned box); that surfaces as ``report["blocked"]`` with the
      validation error, never a crash.

    Returns (model, report). ``model`` is None when the file yields zero
    storeys, zero elements, or fails ConstructionModel's own validation --
    check ``report["blocked"]``/``report["skipped"]`` for why.
    """
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element
    import ifcopenshell.util.placement
    import ifcopenshell.util.unit
    from pydantic import ValidationError

    from app.ai.construction_emg import (
        ColumnElement,
        ConstructionBox,
        ConstructionModel,
        ConstructionStorey,
        OpeningElement,
        SlabElement,
        SpaceElement,
        WallElement,
    )

    model_file = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    unit_scale_m = float(ifcopenshell.util.unit.calculate_unit_scale(model_file) or 1.0)

    def storey_elevation_to_mm(value: float) -> float:
        # get_storey_elevation returns a raw placement-space value in the
        # project's declared length unit -- convert to metres first, then mm.
        return value * unit_scale_m * 1000.0

    def geom_to_mm(value: float) -> float:
        # ifcopenshell.geom tessellation output is always SI (metres),
        # regardless of the project's declared unit -- see project_ifc()'s
        # own comment above. Only ever multiply by 1000, never unit_scale_m.
        return value * 1000.0

    skipped: list[dict[str, Any]] = []

    storeys: list[ConstructionStorey] = []
    storey_ids: dict[int, str] = {}
    for storey in model_file.by_type("IfcBuildingStorey"):
        sid = str(storey.GlobalId)
        elevation = ifcopenshell.util.placement.get_storey_elevation(storey) or 0.0
        storeys.append(ConstructionStorey(
            id=sid, name=str(storey.Name or sid), elevation_mm=storey_elevation_to_mm(elevation),
        ))
        storey_ids[storey.id()] = sid

    def resolve_storey(product: Any) -> Any:
        # get_container's ifc_class filter only walks ContainedInStructure --
        # it does not reliably resolve an element (e.g. IfcSpace) aggregated
        # directly under a storey via IfcRelAggregates/Decomposes. Fall back
        # to walking the decomposition parent chain by hand.
        storey = ifcopenshell.util.element.get_container(product, ifc_class="IfcBuildingStorey")
        if storey is not None:
            return storey
        node = ifcopenshell.util.element.get_parent(product)
        seen: set[int] = set()
        while node is not None and node.id() not in seen:
            seen.add(node.id())
            if node.is_a("IfcBuildingStorey"):
                return node
            node = ifcopenshell.util.element.get_parent(node)
        return None

    # Opening -> its host wall/slab (IfcRelVoidsElement).
    opening_host: dict[int, Any] = {}
    for rel in model_file.by_type("IfcRelVoidsElement"):
        opening_host[rel.RelatedOpeningElement.id()] = rel.RelatingBuildingElement
    # Door/window -> the opening it fills (IfcRelFillsElement).
    fills_opening: dict[int, Any] = {}
    for rel in model_file.by_type("IfcRelFillsElement"):
        fills_opening[rel.RelatedBuildingElement.id()] = rel.RelatingOpeningElement

    elements: list[Any] = []
    seen_guids: set[str] = set()
    for product in model_file.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            continue
        kind = _KIND_BY_IFC_CLASS.get(product.is_a())
        if kind is None:
            continue  # not modeled by this graph domain -- not a skip, just out of scope

        guid = str(product.GlobalId)
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = list(shape.geometry.verts)
            if not vertices:
                raise ValueError("empty tessellation")
        except Exception as exc:  # noqa: BLE001
            skipped.append({
                "guid": guid, "ifc_class": product.is_a(),
                "reason": "geometry_failed", "detail": f"{type(exc).__name__}:{exc}",
            })
            continue

        xs, ys, zs = vertices[0::3], vertices[1::3], vertices[2::3]
        box = ConstructionBox(
            x_mm=geom_to_mm(min(xs)), y_mm=geom_to_mm(min(ys)), z_mm=geom_to_mm(min(zs)),
            width_mm=max(geom_to_mm(max(xs) - min(xs)), 1e-6),
            depth_mm=max(geom_to_mm(max(ys) - min(ys)), 1e-6),
            height_mm=max(geom_to_mm(max(zs) - min(zs)), 1e-6),
        )

        storey = resolve_storey(product)
        storey_id = storey_ids.get(storey.id()) if storey is not None else None
        if storey_id is None:
            skipped.append({"guid": guid, "ifc_class": product.is_a(), "reason": "no_storey"})
            continue

        host_id: str | None = None
        if kind == "opening":
            if product.is_a() == "IfcOpeningElement":
                host = opening_host.get(product.id())
            else:  # IfcDoor / IfcWindow
                opening = fills_opening.get(product.id())
                host = opening_host.get(opening.id()) if opening is not None else None
            if host is None:
                skipped.append({
                    "guid": guid, "ifc_class": product.is_a(), "reason": "no_host",
                    "detail": "no host wall/slab via IfcRelVoidsElement/IfcRelFillsElement",
                })
                continue
            host_id = str(host.GlobalId)

        if guid in seen_guids:
            skipped.append({"guid": guid, "ifc_class": product.is_a(), "reason": "duplicate_guid"})
            continue
        seen_guids.add(guid)

        name = str(product.Name or guid)
        common = {"id": guid, "name": name, "storey_id": storey_id, "box": box}
        if kind == "wall":
            elements.append(WallElement(kind="wall", **common))
        elif kind == "slab":
            elements.append(SlabElement(kind="slab", **common))
        elif kind == "column":
            elements.append(ColumnElement(kind="column", **common))
        elif kind == "space":
            elements.append(SpaceElement(kind="space", **common))
        elif kind == "opening":
            elements.append(OpeningElement(kind="opening", host_id=host_id, **common))

    sites = model_file.by_type("IfcSite")
    buildings = model_file.by_type("IfcBuilding")
    resolved_site_name = site_name or (str(sites[0].Name) if sites and sites[0].Name else "Site")
    resolved_building_name = building_name or (
        str(buildings[0].Name) if buildings and buildings[0].Name else "Building"
    )

    report: dict[str, Any] = {
        "ifc_schema": model_file.schema,
        "mapped_elements": len(elements),
        "storeys": len(storeys),
        "skipped": skipped,
    }

    if not storeys or not elements:
        report["blocked"] = True
        report["blocked_reason"] = "no_storeys" if not storeys else "no_mapped_elements"
        return None, report

    try:
        model = ConstructionModel(
            site_name=resolved_site_name,
            building_name=resolved_building_name,
            storeys=storeys,
            elements=elements,
        )
    except ValidationError as exc:
        report["blocked"] = True
        report["blocked_reason"] = "construction_model_validation_failed"
        report["validation_error"] = str(exc)
        return None, report

    return model, report
