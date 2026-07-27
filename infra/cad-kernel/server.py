"""Headless FreeCAD/OpenCascade compiler for confirmed Engineering IR trees."""

from __future__ import annotations

import hashlib
import io
import json
import math
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal

import FreeCAD as App
import Mesh
import Part
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Feature(BaseModel):
    # Strict boundary is deliberate (sandboxed kernel); the traceability
    # fields the backend attaches (param_provenance — D2) are accepted but
    # unused here: the kernel builds geometry from ``params`` only.
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "extrude", "hole", "boss", "pocket", "fillet", "chamfer",
        # D3: revolve/loft are alternative BASE features (a shaft profile spun
        # about Z; circular sections lofted along Z); shell hollows the final
        # solid; thread is cosmetic per ЕСКД (reported, not modeled).
        "revolve", "loft", "shell", "thread",
        # Turned-part cuts a pocket cannot express: a pocket is cut from the top
        # face straight down -Z, while an annular groove goes all the way round
        # a cylindrical surface and a keyway is milled into that surface from
        # the side. Both are on every real shaft drawing.
        "groove", "keyway",
    ]
    source_entity_ids: list[str] = Field(default_factory=list, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    param_provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    features: list[Feature] = Field(min_length=1, max_length=500)
    score: float = Field(ge=0, le=1)
    label: str = Field(min_length=1, max_length=500)
    missing_data: list[str] = Field(default_factory=list, max_length=500)
    correspondences: list[str] = Field(default_factory=list, max_length=500)


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: Candidate
    confirm_assumptions: bool = False
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_is_bounded(cls, value: dict) -> dict:
        if len(value) > 30 or any(len(str(key)) > 100 or len(str(item)) > 1000 for key, item in value.items()):
            raise ValueError("metadata is too large")
        return value


app = FastAPI(title="Engineering CAD Kernel", version="1.0.0")


def _number(params: dict[str, Any], name: str, *, maximum: float = 100_000) -> float:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(422, f"Feature parameter {name!r} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise HTTPException(422, f"Feature parameter {name!r} is outside supported bounds")
    return result


def _coordinate(params: dict[str, Any], name: str, *, limit: float = 100_000) -> float:
    """A feature CENTRE, which may legitimately be zero or negative.

    ``_number`` rejects anything <= 0 because it reads sizes; a hole on the axis
    of a flange sits at exactly (0, 0), and holes on a bolt circle sit at
    negative coordinates for half the pattern.
    """
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(422, f"Feature parameter {name!r} must be numeric")
    result = float(value)
    if not math.isfinite(result) or abs(result) > limit:
        raise HTTPException(422, f"Feature parameter {name!r} is outside supported bounds")
    return result


def _check_footprint(
    kind: str, x: float, y: float, radius: float,
    *, axis_centred: bool, outer_radius: float, width: float, height: float,
) -> None:
    """Does this cut/boss stay inside the material it is applied to?"""
    if axis_centred:
        if math.hypot(x, y) + radius > outer_radius + 1e-6:
            raise HTTPException(
                422,
                f"{kind} at ({x:g}, {y:g}) reaches past the turned outer radius "
                f"{outer_radius:g} mm",
            )
        return
    if x - radius < -1e-6 or y - radius < -1e-6 or x + radius > width + 1e-6 or y + radius > height + 1e-6:
        raise HTTPException(422, f"{kind} lies outside the base footprint")


def _top_z_at(shape: Part.Shape, x: float, y: float) -> float:
    probe = Part.makeLine(
        App.Vector(x, y, shape.BoundBox.ZMin - 1.0),
        App.Vector(x, y, shape.BoundBox.ZMax + 1.0),
    )
    section = shape.section(probe)
    levels = [vertex.Point.z for vertex in section.Vertexes]
    if not levels:
        raise HTTPException(422, "Hole center has no supporting material")
    return max(levels)


def _cut_feature(
    shape: Part.Shape, tool: Part.Shape, label: str, warnings: list[str]
) -> Part.Shape:
    """Subtract one feature, and never let it cost the whole part.

    OpenCascade's boolean is order-sensitive in ways nothing here controls:
    measured on the reference spindle, cutting three cross-holes as Ø14, Ø10,
    Ø9 produces an invalid solid, while the SAME three cut in the reverse order
    produce a valid one. Each cut on its own is fine. There is no reordering
    rule that is right in general, and a part that fails to build is worth far
    less to the person waiting for it than a part missing one oil way and
    saying so.

    So: cut, then check. If the result is unusable, try the standard cleanup,
    and failing that keep the previous solid and report the feature as not
    built. removeSplitter alone does not rescue this case (measured), which is
    why the fallback is a rollback rather than a repair.
    """
    try:
        candidate = shape.cut(tool)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{label} not built: OpenCascade rejected the cut ({str(exc)[:100]})")
        return shape
    if not candidate.isNull() and candidate.isValid() and candidate.Volume > 0:
        return candidate
    try:
        cleaned = candidate.removeSplitter()
        if not cleaned.isNull() and cleaned.isValid() and cleaned.Volume > 0:
            return cleaned
    except Exception:  # noqa: BLE001 — cleanup is best effort
        pass
    warnings.append(
        f"{label} not built: OpenCascade returned invalid geometry for this cut"
    )
    return shape


def _bottom_z_at(shape: Part.Shape, x: float, y: float) -> float:
    """The LOWEST material level under a point — the mirror of _top_z_at.

    A hole drilled from the right-hand face needs the face it starts at, and
    that is the bottom of the material, not the top.
    """
    probe = Part.makeLine(
        App.Vector(x, y, shape.BoundBox.ZMin - 1.0),
        App.Vector(x, y, shape.BoundBox.ZMax + 1.0),
    )
    section = shape.section(probe)
    levels = [vertex.Point.z for vertex in section.Vertexes]
    if not levels:
        raise HTTPException(422, "Hole center has no supporting material")
    return min(levels)


def _radial_hole_tool(
    shape: Part.Shape,
    feature: "Feature",
    radius: float,
    through: Any,
    warnings: list[str],
    request: "CompileRequest",
) -> Part.Shape:
    """A hole drilled ACROSS the axis, at an angular position round the shaft."""
    z = _coordinate(feature.params, "axial_position_mm")
    angle = (
        _coordinate(feature.params, "angle_deg")
        if "angle_deg" in feature.params
        else 0.0
    )
    bounds = shape.BoundBox
    if z < bounds.ZMin - 1e-6 or z > bounds.ZMax + 1e-6:
        raise HTTPException(422, "Cross hole lies outside the part along its axis")
    surface_radius = _radius_at(shape, z, inner=False)
    if surface_radius <= 0.0:
        raise HTTPException(422, "Cross hole has no supporting material at that position")
    if radius >= surface_radius:
        raise HTTPException(422, "Cross hole is wider than the material it passes through")

    if through is False:
        depth = _number(feature.params, "depth_mm", maximum=2.0 * surface_radius)
        start_x = surface_radius + 1.0
        length = depth + 1.0
        origin = App.Vector(start_x, 0.0, z)
        direction = App.Vector(-1.0, 0.0, 0.0)
    else:
        if through is None and not request.confirm_assumptions:
            raise HTTPException(409, "Unknown cross-hole depth requires explicit confirmation")
        if through is None:
            warnings.append(
                f"Cross hole {2 * radius:g}mm compiled as through because depth is unknown"
            )
        length = 2.0 * (surface_radius + 1.0)
        origin = App.Vector(-(surface_radius + 1.0), 0.0, z)
        direction = App.Vector(1.0, 0.0, 0.0)

    tool = Part.makeCylinder(radius, length, origin, direction)
    if abs(angle) > 1e-9:
        tool.rotate(App.Vector(0.0, 0.0, 0.0), App.Vector(0.0, 0.0, 1.0), angle)
    return tool


def _edge_key(edge: Part.Edge) -> str:
    bounds = edge.BoundBox
    payload = {
        "curve": edge.Curve.__class__.__name__,
        "length": round(edge.Length, 6),
        "bounds": [round(value, 6) for value in (
            bounds.XMin, bounds.YMin, bounds.ZMin,
            bounds.XMax, bounds.YMax, bounds.ZMax,
        )],
        "vertices": sorted([
            [round(vertex.Point.x, 6), round(vertex.Point.y, 6), round(vertex.Point.z, 6)]
            for vertex in edge.Vertexes
        ]),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"edge-{digest}"


def _edge_descriptors(shape: Part.Shape) -> list[dict[str, Any]]:
    return [
        {
            "key": _edge_key(edge),
            "index": index,
            "curve": edge.Curve.__class__.__name__,
            "length_mm": edge.Length,
            "vertices": [
                {"x": vertex.Point.x, "y": vertex.Point.y, "z": vertex.Point.z}
                for vertex in edge.Vertexes
            ],
        }
        for index, edge in enumerate(shape.Edges, start=1)
    ]


def _find_edge(shape: Part.Shape, key: str) -> Part.Edge:
    matches = [edge for edge in shape.Edges if _edge_key(edge) == key]
    if len(matches) != 1:
        raise HTTPException(422, "Selected edge no longer exists after preceding operations")
    return matches[0]


def _resolve_edge(shape: Part.Shape, params: dict[str, Any]) -> Part.Edge:
    """The edge an operation applies to: by key, or by what it IS.

    An edge_key can only be known AFTER a solid exists, which is fine for a
    human picking in the editor and impossible for a drawing being redrawn: the
    reader knows "1x45° on the shoulder at Ø80", not a hash. So an operation may
    instead describe the edge — a circle, at this axial position, of this
    diameter — and it is resolved against the shape as it stands at that moment,
    with every preceding cut already applied.

    Ambiguity is an error rather than a guess, and the error LISTS the
    candidates: picking the wrong shoulder silently produces a plausible part
    that is not the one on the sheet.
    """
    key = params.get("edge_key")
    if isinstance(key, str) and key:
        return _find_edge(shape, key)
    selector = params.get("edge_selector")
    if not isinstance(selector, dict):
        raise HTTPException(422, "Edge operation requires edge_key or edge_selector")

    tolerance = selector.get("tolerance_mm")
    tolerance = float(tolerance) if isinstance(tolerance, (int, float)) else 0.25
    wanted_curve = selector.get("curve")
    at_z = selector.get("at_z_mm")
    diameter = selector.get("diameter_mm")

    candidates: list[tuple[float, float, Part.Edge]] = []
    for edge in shape.Edges:
        curve = edge.Curve.__class__.__name__
        if wanted_curve and curve != wanted_curve:
            continue
        centre = edge.BoundBox.Center
        # A circular edge on a turned part lies in a plane of constant z, and
        # its diameter is what the drawing calls the step.
        edge_z = centre.z
        edge_diameter = 2.0 * getattr(edge.Curve, "Radius", 0.0) if curve == "Circle" else 0.0
        if at_z is not None and abs(edge_z - float(at_z)) > tolerance:
            continue
        if diameter is not None and abs(edge_diameter - float(diameter)) > tolerance:
            continue
        candidates.append((edge_z, edge_diameter, edge))

    if not candidates:
        raise HTTPException(
            422,
            "No edge matches the selector "
            f"{ {k: v for k, v in selector.items() if k != 'tolerance_mm'} }",
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    index = selector.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        if not 0 <= index < len(candidates):
            raise HTTPException(
                422, f"Edge selector index {index} out of range (0..{len(candidates) - 1})"
            )
        return candidates[index][2]
    if len(candidates) > 1:
        described = ", ".join(
            f"(z={z:.3f}, Ø{d:.3f})" for z, d, _edge in candidates[:8]
        )
        raise HTTPException(
            422,
            f"Edge selector matches {len(candidates)} edges: {described}. "
            "Add index or narrow at_z_mm/diameter_mm.",
        )
    return candidates[0][2]


def _groove_tool(shape: Part.Shape, feature: "Feature") -> Part.Shape:
    """The material an annular groove removes (ГОСТ 8820 and friends).

    A groove goes all the way round the turned surface, so the tool is a ring:
    the cylinder that reaches the surface minus the one that stays. Depth is
    measured inward from the LOCAL surface, which is what the sheet dimensions
    — a groove on a Ø80 step and the same groove on a Ø102 step are the same
    groove, at different radii.
    """
    position = _coordinate(feature.params, "axial_position_mm")
    width = _number(feature.params, "width_mm")
    internal = bool(feature.params.get("internal"))
    z0 = position - width / 2.0
    bounds = shape.BoundBox
    if z0 < bounds.ZMin - 1e-6 or z0 + width > bounds.ZMax + 1e-6:
        raise HTTPException(422, "Groove lies outside the part along its axis")

    surface_radius = _radius_at(shape, position, inner=internal)
    if surface_radius <= 0.0:
        raise HTTPException(422, "Groove has no supporting material at that position")
    root = feature.params.get("root_diameter_mm")
    if isinstance(root, (int, float)) and not isinstance(root, bool):
        root_radius = float(root) / 2.0
    else:
        depth = _number(feature.params, "depth_mm")
        root_radius = (
            surface_radius + depth if internal else surface_radius - depth
        )
    if not internal and root_radius <= 0.0:
        raise HTTPException(422, "Groove is deeper than the material it cuts into")

    origin = App.Vector(0.0, 0.0, z0)
    if internal:
        # An internal groove widens the bore: cut out to the root radius.
        return Part.makeCylinder(root_radius, width, origin).cut(
            Part.makeCylinder(surface_radius, width, origin)
        )
    outer = Part.makeCylinder(surface_radius + 1.0, width, origin)
    return outer.cut(Part.makeCylinder(root_radius, width, origin))


def _keyway_tool(shape: Part.Shape, feature: "Feature") -> Part.Shape:
    """The slot a keyway cutter leaves in a shaft (ГОСТ 23360).

    Built lying along the axis at angle 0 and then rotated into place, because
    that is how the sheet states it: a start, a length, a width, a depth from
    the surface, and an angular position. A closed keyway ends in two radii, so
    the tool is a rectangle with a cylinder at each end.
    """
    start = _coordinate(feature.params, "axial_start_mm")
    length = _number(feature.params, "length_mm")
    width = _number(feature.params, "width_mm")
    depth = _number(feature.params, "depth_mm")
    angle = _coordinate(feature.params, "angle_deg") if "angle_deg" in feature.params else 0.0
    end_type = feature.params.get("end_type") or "closed"

    bounds = shape.BoundBox
    if start < bounds.ZMin - 1e-6 or start + length > bounds.ZMax + 1e-6:
        raise HTTPException(422, "Keyway runs past the end of the part")
    surface_radius = _radius_at(shape, start + length / 2.0, inner=False)
    if surface_radius <= 0.0:
        raise HTTPException(422, "Keyway has no supporting material at that position")
    if depth >= surface_radius:
        raise HTTPException(422, "Keyway is deeper than the shaft radius")

    # The cutter is built in x: it spans from the surface inward by `depth`,
    # plus 1 mm of overshoot above the surface so the cut is clean.
    x0 = surface_radius - depth
    height = depth + 1.0
    if end_type == "closed":
        body_length = max(length - width, 0.0)
        tool = Part.makeBox(
            height, width, body_length,
            App.Vector(x0, -width / 2.0, start + width / 2.0),
        )
        for centre_z in (start + width / 2.0, start + length - width / 2.0):
            cap = Part.makeCylinder(
                width / 2.0, height,
                App.Vector(x0, 0.0, centre_z),
                App.Vector(1.0, 0.0, 0.0),
            )
            tool = tool.fuse(cap)
    else:
        # An open keyway runs out through the end face; no end radii.
        tool = Part.makeBox(
            height, width, length, App.Vector(x0, -width / 2.0, start)
        )
    if abs(angle) > 1e-9:
        tool.rotate(App.Vector(0.0, 0.0, 0.0), App.Vector(0.0, 0.0, 1.0), angle)
    return tool


def _radius_at(shape: Part.Shape, z: float, *, inner: bool) -> float:
    """The material radius of a turned part at one axial position.

    Measured off the solid rather than taken from the request: by the time a
    groove is cut, earlier features have already changed the shape, and a depth
    applied to the wrong radius cuts either air or the axis.
    """
    plane = Part.makePlane(
        max(shape.BoundBox.XLength, shape.BoundBox.YLength) * 4.0 + 10.0,
        max(shape.BoundBox.XLength, shape.BoundBox.YLength) * 4.0 + 10.0,
        App.Vector(
            -(max(shape.BoundBox.XLength, shape.BoundBox.YLength) * 2.0 + 5.0),
            -(max(shape.BoundBox.XLength, shape.BoundBox.YLength) * 2.0 + 5.0),
            z,
        ),
    )
    section = shape.section(plane)
    radii = [
        math.hypot(vertex.Point.x, vertex.Point.y) for vertex in section.Vertexes
    ]
    if not radii:
        return 0.0
    return min(radii) if inner else max(radii)


def _revolve_points(raw: Any, label: str) -> list[App.Vector]:
    """Validate and convert an (r, z) profile polyline into kernel vectors."""
    if not isinstance(raw, list) or len(raw) < 2 or len(raw) > 200:
        raise HTTPException(422, f"{label} must be a list of 2..200 points of {{r, z}}")
    points: list[App.Vector] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(422, f"{label} point must be an object {{r, z}}")
        r, z = item.get("r"), item.get("z")
        for name, value in (("r", r), ("z", z)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise HTTPException(422, f"{label} point {name!r} must be a finite number")
        if float(r) < 0 or float(r) > 100_000 or abs(float(z)) > 100_000:
            raise HTTPException(422, f"{label} point is outside supported bounds")
        points.append(App.Vector(float(r), 0.0, float(z)))
    if max(p.x for p in points) <= 0:
        raise HTTPException(422, f"{label} never leaves the axis — nothing to spin")
    return points


def _revolve_solid(points: list[App.Vector], label: str) -> Part.Shape:
    # close the loop onto the axis (r=0) at both ends unless already there
    closed = list(points)
    if closed[0].x > 1e-9:
        closed.insert(0, App.Vector(0.0, 0.0, closed[0].z))
    if closed[-1].x > 1e-9:
        closed.append(App.Vector(0.0, 0.0, closed[-1].z))
    closed.append(closed[0])
    try:
        face = Part.Face(Part.makePolygon(closed))
        return face.revolve(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 360)
    except Exception as exc:
        raise HTTPException(422, f"OpenCascade rejected the {label}: {exc}") from exc


def _revolve_base(feature: Feature) -> Part.Shape:
    """D3: a lathe part — the (r, z) profile polyline spun 360° about Z.
    The profile is auto-closed onto the axis, so a simple stepped-shaft
    outline (what a front view of a spindle gives you) is enough.

    Optional ``bore_points`` spin a SECOND coaxial profile and cut it out, so a
    hollow lathe part (a spindle, a sleeve, a stepped bore) is one feature
    rather than a stack of holes. Holes are refused on a revolve base — they
    are positioned against an extrude footprint a lathe part does not have —
    so without this a bored part could not be built at all.
    """
    points = _revolve_points(feature.params.get("profile_points"), "revolve profile_points")
    solid = _revolve_solid(points, "revolve profile")

    raw_bore = feature.params.get("bore_points")
    if raw_bore is None:
        return solid
    bore_points = _revolve_points(raw_bore, "revolve bore_points")
    # The bore must stay inside the material at every z it spans, or the "part"
    # is a contradiction the sheet cannot have meant.
    outer_max = max(p.x for p in points)
    if max(p.x for p in bore_points) >= outer_max - 1e-9:
        raise HTTPException(
            422, "bore radius reaches or exceeds the outer radius — not a hollow part"
        )
    bore = _revolve_solid(bore_points, "revolve bore profile")
    try:
        result = solid.cut(bore)
    except Exception as exc:
        raise HTTPException(422, f"OpenCascade rejected the bore cut: {exc}") from exc
    if result.isNull() or not result.isValid() or result.Volume <= 0:
        raise HTTPException(422, "OpenCascade produced invalid geometry after the bore cut")
    return result


def _loft_base(feature: Feature) -> Part.Shape:
    """D3: circular sections at increasing Z lofted into one solid — the
    adapter/cone/transition class of parts."""
    raw = feature.params.get("sections")
    if not isinstance(raw, list) or len(raw) < 2 or len(raw) > 50:
        raise HTTPException(422, "loft requires sections: 2..50 items of {z, diameter_mm}")
    wires = []
    last_z = None
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(422, "loft section must be an object {z, diameter_mm}")
        z = item.get("z")
        if isinstance(z, bool) or not isinstance(z, (int, float)) or not math.isfinite(float(z)) or abs(float(z)) > 100_000:
            raise HTTPException(422, "loft section 'z' must be a finite number")
        diameter = _number(item, "diameter_mm")
        if last_z is not None and float(z) <= last_z:
            raise HTTPException(422, "loft sections must have strictly increasing z")
        last_z = float(z)
        circle = Part.Circle(App.Vector(0, 0, float(z)), App.Vector(0, 0, 1), diameter / 2)
        wires.append(Part.Wire(circle.toShape()))
    try:
        solid = Part.makeLoft(wires, True)
    except Exception as exc:
        raise HTTPException(422, f"OpenCascade rejected the loft sections: {exc}") from exc
    return solid


def _apply_shell(shape: Part.Shape, feature: Feature) -> Part.Shape:
    """D3: hollow the solid leaving walls of the given thickness, opening the
    top face (the face whose centre sits highest)."""
    thickness = _number(feature.params, "thickness_mm", maximum=10_000)
    if not shape.Faces:
        raise HTTPException(422, "shell requires a solid with faces")
    top = max(shape.Faces, key=lambda f: f.CenterOfMass.z)
    try:
        result = shape.makeThickness([top], -thickness, 1e-3)
    except Exception as exc:
        raise HTTPException(422, f"OpenCascade rejected shell: {exc}") from exc
    if result.isNull() or not result.isValid() or result.Volume <= 0:
        raise HTTPException(422, "OpenCascade produced invalid geometry after shell")
    return result


def _build_shape(request: CompileRequest) -> tuple[Part.Shape, list[str]]:
    bases = [
        feature for feature in request.candidate.features
        if feature.kind in ("extrude", "revolve", "loft")
    ]
    if len(bases) != 1:
        raise HTTPException(422, "Exactly one base feature (extrude, revolve or loft) is required")
    if request.candidate.missing_data and not request.confirm_assumptions:
        raise HTTPException(409, "Explicit confirmation of feature-tree assumptions is required")

    base = bases[0]
    warnings: list[str] = []
    # An extrude base is a box anchored at the origin, so cut/add features are
    # addressed from its corner. A turned base is symmetric about the axis, so
    # ITS features are addressed from the axis (x=y=0 is the centre) and bounded
    # by the material radius — the convention a flange drawing already uses when
    # it gives hole coordinates from the centre.
    axis_centred = base.kind in ("revolve", "loft")
    outer_radius = 0.0
    if base.kind == "revolve":
        shape = _revolve_base(base)
    elif base.kind == "loft":
        shape = _loft_base(base)
    if axis_centred:
        width = height = depth = max(
            shape.BoundBox.XLength, shape.BoundBox.YLength, shape.BoundBox.ZLength
        )
        outer_radius = max(shape.BoundBox.XLength, shape.BoundBox.YLength) / 2.0
    else:
        width = _number(base.params, "width_mm")
        height = _number(base.params, "height_mm")
        depth = _number(base.params, "depth_mm")
        shape = Part.makeBox(width, height, depth)

    for feature in request.candidate.features:
        if feature.kind not in ("boss", "pocket"):
            continue
        profile = feature.params.get("profile")
        x = _coordinate(feature.params, "center_x_mm")
        y = _coordinate(feature.params, "center_y_mm")
        operation_depth = _number(feature.params, "depth_mm")
        # For an extrude box the top face is at z == depth; for a turned base it
        # is wherever the solid actually ends, and using the box convention
        # there would cut in mid-air above the part.
        top_z = shape.BoundBox.ZMax if axis_centred else depth
        material_depth = (
            shape.BoundBox.ZMax - shape.BoundBox.ZMin if axis_centred else depth
        )
        if feature.kind == "pocket" and operation_depth > material_depth + 1e-6:
            raise HTTPException(422, "Pocket depth exceeds base depth")
        z = top_z if feature.kind == "boss" else top_z - operation_depth
        solid_height = operation_depth if feature.kind == "boss" else operation_depth + 1.0
        if profile == "circle":
            diameter = _number(feature.params, "diameter_mm", maximum=min(width, height) * 2)
            radius = diameter / 2
            _check_footprint(
                feature.kind, x, y, radius,
                axis_centred=axis_centred, outer_radius=outer_radius,
                width=width, height=height,
            )
            tool = Part.makeCylinder(radius, solid_height, App.Vector(x, y, z))
        elif profile == "rectangle":
            profile_width = _number(feature.params, "width_mm", maximum=width)
            profile_height = _number(feature.params, "height_mm", maximum=height)
            x0 = x - profile_width / 2
            y0 = y - profile_height / 2
            if x0 < -1e-6 or y0 < -1e-6 or x0 + profile_width > width + 1e-6 or y0 + profile_height > height + 1e-6:
                raise HTTPException(422, f"{feature.kind} lies outside the base footprint")
            tool = Part.makeBox(profile_width, profile_height, solid_height, App.Vector(x0, y0, z))
        else:
            raise HTTPException(422, f"Unsupported {feature.kind} profile")
        shape = shape.fuse(tool) if feature.kind == "boss" else shape.cut(tool)

    for feature in request.candidate.features:
        if feature.kind not in ("groove", "keyway"):
            continue
        tool = (
            _groove_tool(shape, feature)
            if feature.kind == "groove"
            else _keyway_tool(shape, feature)
        )
        position = feature.params.get("axial_position_mm", feature.params.get("axial_start_mm"))
        shape = _cut_feature(shape, tool, f"{feature.kind} @ {position}", warnings)

    for feature in request.candidate.features:
        if feature.kind != "hole":
            continue
        diameter = _number(feature.params, "diameter_mm", maximum=min(width, height) * 2)
        x = _coordinate(feature.params, "center_x_mm")
        y = _coordinate(feature.params, "center_y_mm")
        radius = diameter / 2
        _check_footprint(
            "Hole", x, y, radius,
            axis_centred=axis_centred, outer_radius=outer_radius,
            width=width, height=height,
        )
        through = feature.params.get("through")
        # A cross-drilling runs ACROSS the axis, not along it — the oil ways and
        # dowel holes on every shaft drawing. Same feature, different direction,
        # so it is a parameter rather than a second kind.
        if (feature.params.get("axis") or "z") == "radial":
            shape = _cut_feature(
                shape,
                _radial_hole_tool(shape, feature, radius, through, warnings, request),
                f"cross hole Ø{diameter:g} @ {feature.params.get('axial_position_mm')}",
                warnings,
            )
            continue
        if through is True:
            cutter = Part.makeCylinder(
                radius,
                shape.BoundBox.ZMax - shape.BoundBox.ZMin + 2.0,
                App.Vector(x, y, shape.BoundBox.ZMin - 1.0),
            )
        elif through is False:
            # Which face the hole is drilled from. A bore that starts at the
            # right-hand face was previously impossible to state, so it came out
            # of the wrong end of the part with every dimension still correct.
            from_face = feature.params.get("from_face") or "zmax"
            if from_face == "zmin":
                bottom_z = _bottom_z_at(shape, x, y)
                available_depth = shape.BoundBox.ZMax - bottom_z
                hole_depth = _number(feature.params, "depth_mm", maximum=available_depth)
                if hole_depth >= available_depth - 1e-6:
                    raise HTTPException(
                        422, "Blind hole depth must be smaller than local material depth"
                    )
                cutter = Part.makeCylinder(
                    radius, hole_depth + 1.0, App.Vector(x, y, bottom_z - 1.0)
                )
            else:
                top_z = _top_z_at(shape, x, y)
                available_depth = top_z - shape.BoundBox.ZMin
                hole_depth = _number(feature.params, "depth_mm", maximum=available_depth)
                if hole_depth >= available_depth - 1e-6:
                    raise HTTPException(422, "Blind hole depth must be smaller than local material depth")
                cutter = Part.makeCylinder(
                    radius,
                    hole_depth + 1.0,
                    App.Vector(x, y, top_z - hole_depth),
                )
        else:
            if not request.confirm_assumptions:
                raise HTTPException(409, "Unknown hole depth requires explicit confirmation")
            warnings.append(f"Hole {diameter:g}mm compiled as through because depth is unknown")
            cutter = Part.makeCylinder(
                radius,
                shape.BoundBox.ZMax - shape.BoundBox.ZMin + 2.0,
                App.Vector(x, y, shape.BoundBox.ZMin - 1.0),
            )
        shape = _cut_feature(shape, cutter, f"hole Ø{diameter:g}", warnings)

    # Edge operations come AFTER every cut: a chamfer belongs to the edge that
    # exists once the part is fully cut, and asking for one before the hole is
    # drilled means the mouth of that hole can never be chamfered. It also fails
    # loudly rather than silently landing on some other edge.
    for feature in request.candidate.features:
        if feature.kind not in ("fillet", "chamfer"):
            continue
        edge = _resolve_edge(shape, feature.params)
        size = _number(feature.params, "size_mm", maximum=max(width, height, depth))
        # A refused edge operation must not cost the part. OpenCascade declines
        # these routinely — an edge too short for the radius, a fillet across a
        # groove it cannot blend — and losing a whole shaft over a 1x45° corner
        # is the wrong trade. It declines in TWO ways, measured on this build:
        # it raises (StdFail_NotDone), or it returns a shape that is simply not
        # valid. Both mean the same thing, so both keep the previous solid and
        # report the feature as not built.
        previous = shape
        try:
            candidate = (
                shape.makeFillet(size, [edge])
                if feature.kind == "fillet"
                else shape.makeChamfer(size, [edge])
            )
        except Exception as exc:
            warnings.append(
                f"{feature.kind} not built: OpenCascade rejected it ({str(exc)[:120]})"
            )
            continue
        if candidate.isNull() or not candidate.isValid():
            shape = previous
            warnings.append(
                f"{feature.kind} not built: OpenCascade returned invalid geometry "
                f"for size {size:g} mm"
            )
            continue
        shape = candidate

    # D3: shell hollows the finished solid (after all add/cut operations).
    shells = [f for f in request.candidate.features if f.kind == "shell"]
    if len(shells) > 1:
        raise HTTPException(422, "At most one shell feature is supported")
    for feature in shells:
        shape = _apply_shell(shape, feature)

    shape = shape.removeSplitter()
    if shape.isNull() or not shape.isValid() or shape.Volume <= 0:
        raise HTTPException(422, "OpenCascade produced an invalid or empty solid")
    return shape, warnings


def _cosmetic_threads(request: CompileRequest) -> list[dict[str, Any]]:
    """D3: threads are cosmetic per ЕСКД (ГОСТ 2.311 draws them conventionally;
    modeling helical geometry adds nothing downstream). Validated and carried
    into the report so the drawing/technology layers can consume them."""
    threads: list[dict[str, Any]] = []
    for feature in request.candidate.features:
        if feature.kind != "thread":
            continue
        spec = feature.params.get("spec")
        if not isinstance(spec, str) or not (2 <= len(spec) <= 40):
            raise HTTPException(422, "thread requires a 'spec' designation (e.g. М12x1.75)")
        diameter = _number(feature.params, "diameter_mm", maximum=10_000)
        entry: dict[str, Any] = {"spec": spec, "diameter_mm": diameter}
        pitch = feature.params.get("pitch_mm")
        if pitch is not None:
            entry["pitch_mm"] = _number(feature.params, "pitch_mm", maximum=100)
        threads.append(entry)
    return threads


class InterferenceComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=160)
    # Primitive occupancy solid in the component's local frame (origin at its
    # own corner/base): box(width/height/depth) or cylinder(diameter/height,
    # axis +Z from the origin).
    shape: dict[str, Any]
    # translate [x, y, z] mm + rotation about Z (deg) into assembly space.
    transform: dict[str, Any] = Field(default_factory=dict)


class InterferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    components: list[InterferenceComponent] = Field(min_length=2, max_length=50)
    # Below this common volume a touch is contact, not interference.
    tolerance_mm3: float = Field(default=1e-3, gt=0)


def _component_solid(component: InterferenceComponent) -> Part.Shape:
    shape = component.shape if isinstance(component.shape, dict) else {}
    kind = shape.get("kind")
    if kind == "box":
        solid = Part.makeBox(
            _number(shape, "width_mm"), _number(shape, "height_mm"), _number(shape, "depth_mm")
        )
    elif kind == "cylinder":
        solid = Part.makeCylinder(_number(shape, "diameter_mm") / 2, _number(shape, "height_mm"))
    else:
        raise HTTPException(422, f"Component {component.key!r}: shape.kind must be box or cylinder")
    transform = component.transform or {}
    rotate = transform.get("rotate_z_deg", 0)
    if isinstance(rotate, bool) or not isinstance(rotate, (int, float)) or not math.isfinite(float(rotate)):
        raise HTTPException(422, f"Component {component.key!r}: rotate_z_deg must be a finite number")
    if abs(float(rotate)) > 1e-9:
        solid.rotate(App.Vector(0, 0, 0), App.Vector(0, 0, 1), float(rotate))
    translate = transform.get("translate", [0, 0, 0])
    if (
        not isinstance(translate, list) or len(translate) != 3
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in translate)
    ):
        raise HTTPException(422, f"Component {component.key!r}: translate must be [x, y, z]")
    solid.translate(App.Vector(*(float(v) for v in translate)))
    return solid


@app.post("/interference")
def check_interference(request: InterferenceRequest) -> dict[str, Any]:
    """E5: EXACT B-Rep interference — pairwise boolean common() volume between
    positioned component solids, not an axis-aligned bounding-box guess. A
    rotated part that clears its neighbour no longer reads as a collision, and
    a genuine overlap reports how much material intersects."""
    solids = [(component.key, _component_solid(component)) for component in request.components]
    if len({key for key, _ in solids}) != len(solids):
        raise HTTPException(422, "Component keys must be unique")
    collisions: list[dict[str, Any]] = []
    for index, (first_key, first) in enumerate(solids):
        for second_key, second in solids[index + 1:]:
            # cheap reject before the boolean op
            if not first.BoundBox.intersect(second.BoundBox):
                continue
            try:
                common = first.common(second)
                volume = float(common.Volume)
            except Exception as exc:
                raise HTTPException(422, f"OpenCascade rejected {first_key}∩{second_key}: {exc}") from exc
            if volume > request.tolerance_mm3:
                collisions.append(
                    {"first": first_key, "second": second_key, "volume_mm3": volume}
                )
    return {"collisions": collisions, "checked_pairs": len(solids) * (len(solids) - 1) // 2}


# --- Orthographic projection (ГОСТ 2.305) -----------------------------------
#
# Views are DERIVED from the solid, never drawn independently: once the model
# is right, projection alignment is arithmetic rather than something a drafter
# (human or model) has to keep consistent by hand.
#
# Sheet mapping. The solid is built with its axis of revolution along +Z, but a
# shaft lies horizontally on a drawing, so each view names which model axes
# become sheet (u, v):
#   front — look along -Y: u = +Z (axis runs left→right), v = +X
#   side  — look along -Z (down the axis): u = +X, v = +Y  → concentric circles
#   top   — look along -X: u = +Z, v = +Y
# ``Drawing.project`` returns geometry ALREADY FLATTENED into the XY plane
# (z == 0), not in model coordinates, so each view only needs a 2D mapping from
# that plane to sheet (u, v). Verified against a stepped shaft: projecting a
# Z-axis part along -Y puts the length on -x and the radius on y.
_VIEW_FRAMES: dict[str, tuple[tuple[float, float, float], float, float]] = {
    # name: (direction, u = u_sign * x, v = v_sign * y)
    "front": ((0.0, -1.0, 0.0), -1.0, 1.0),
    "side": ((0.0, 0.0, -1.0), 1.0, 1.0),
    "top": ((-1.0, 0.0, 0.0), -1.0, 1.0),
}


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: Candidate
    views: list[Literal["front", "side", "top"]] = Field(min_length=1, max_length=3)
    confirm_assumptions: bool = False
    # Curves the projector cannot express exactly (a projected ellipse, a spline
    # silhouette) are sampled; straight and circular edges stay exact.
    curve_samples: int = Field(default=24, ge=4, le=200)


def _collinear(points: list[tuple[float, float]], tolerance: float = 1e-6) -> bool:
    """Do these sampled points all lie on one straight line?

    A revolved end face projects edge-on as a BSpline that is geometrically a
    straight segment. Exporting it as a 25-point polyline would put a fake
    curve on a technical drawing, so collinear samples collapse back to a line.
    """
    if len(points) < 3:
        return True
    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    span = math.hypot(dx, dy)
    if span < tolerance:
        return False
    return all(
        abs(dx * (y - y0) - dy * (x - x0)) / span <= max(tolerance, span * 1e-6)
        for x, y in points[1:-1]
    )


def _project_edge(
    edge: Part.Edge, u_sign: float, v_sign: float, samples: int
) -> dict[str, Any] | None:
    """One projected edge as an EXACT primitive where the geometry allows it."""

    def uv(point: App.Vector) -> tuple[float, float]:
        return (round(u_sign * point.x, 6), round(v_sign * point.y, 6))

    curve = edge.Curve.__class__.__name__
    points = [uv(vertex.Point) for vertex in edge.Vertexes]
    if curve == "Line" and len(points) == 2:
        if points[0] == points[1]:
            return None
        return {"type": "line", "points": points}
    if curve == "Circle":
        circle = edge.Curve
        centre = uv(circle.Center)
        closed = len(edge.Vertexes) <= 1 or points[0] == points[-1]
        if closed:
            return {"type": "circle", "center": centre, "radius": round(circle.Radius, 6)}
        return {
            "type": "arc",
            "center": centre,
            "radius": round(circle.Radius, 6),
            "points": points,
        }
    # Anything else (spline silhouette, edge-on circle) is sampled along the edge.
    try:
        first, last = edge.FirstParameter, edge.LastParameter
        sampled = [
            uv(edge.valueAt(first + (last - first) * index / samples))
            for index in range(samples + 1)
        ]
    except Exception:  # noqa: BLE001 — an unsamplable edge is simply skipped
        return None
    deduped = [sampled[0]]
    for point in sampled[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    if len(deduped) < 2:
        return None
    if _collinear(deduped):
        return {"type": "line", "points": [deduped[0], deduped[-1]]}
    return {"type": "polyline", "points": deduped}


@app.post("/project")
def project_views(request: ProjectRequest) -> dict[str, Any]:
    """Orthographic views of the compiled solid, as exact 2D primitives.

    Returns visible and hidden geometry separately so the caller can honour
    ГОСТ 2.303 line types, and the bounding box of each view so it can place
    them in projection alignment without re-measuring the model.

    Built on TechDraw views rather than the legacy ``Drawing`` module, which
    FreeCAD 1.x removed outright — and whose replacement, ``TechDraw.projectEx``,
    segfaults when called directly on a shape. The view objects are the path
    that actually works headless, and they are what ``/drawing`` already uses,
    so both endpoints now share one projector instead of two.

    Coordinates are the view's own, centred on the shape. Callers place views
    from ``bounds_mm`` and compare extents, both of which are unchanged by that.
    """
    shape, warnings = _build_shape(
        CompileRequest(
            candidate=request.candidate,
            confirm_assumptions=request.confirm_assumptions,
            metadata={},
        )
    )
    document = App.newDocument("project")
    try:
        body = document.addObject("Part::Feature", "Body")
        body.Shape = shape
        page = document.addObject("TechDraw::DrawPage", "Page")
        page.Template = document.addObject("TechDraw::DrawSVGTemplate", "Template")

        views: dict[str, Any] = {}
        for name in request.views:
            direction = _VIEW_FRAMES[name][0]
            view = document.addObject("TechDraw::DrawViewPart", f"View_{name}")
            page.addView(view)
            view.Source = [body]
            view.Direction = App.Vector(*direction)
            if name == "front":
                view.XDirection = App.Vector(0.0, 0.0, 1.0)
            if "ScaleType" in view.PropertiesList:
                view.ScaleType = "Custom"
            view.Scale = 1.0
            if hasattr(view, "HardHidden"):
                view.HardHidden = True
            document.recompute()
            entities = _techdraw_edges(view, request.curve_samples)
            points: list[tuple[float, float]] = []
            for group in entities.values():
                for item in group:
                    if item.get("points"):
                        points.extend(item["points"])
                    elif item.get("type") == "circle":
                        cu, cv = item["center"]
                        radius = item["radius"]
                        points.extend([
                            (cu - radius, cv - radius), (cu + radius, cv + radius),
                        ])
            bounds = None
            if points:
                bounds = {
                    "u_min": min(p[0] for p in points), "u_max": max(p[0] for p in points),
                    "v_min": min(p[1] for p in points), "v_max": max(p[1] for p in points),
                }
            views[name] = {
                "bounds_mm": bounds,
                "visible": entities["visible"],
                "hidden": entities["hidden"],
            }
        return {"views": views, "warnings": warnings, "kernel": "FreeCAD/TechDraw"}
    finally:
        App.closeDocument(document.Name)


class SheetViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["front", "side", "top", "section"] = "front"
    label: str | None = None
    # Where the cutting plane sits along the view's own depth axis, in model mm.
    # Only meaningful for a section; None puts it through the model centre.
    section_origin_mm: float | None = None
    # A STEPPED or BROKEN section (ГОСТ 2.305): the polyline the cut follows,
    # in model millimetres. A real sheet is full of these — a straight plane
    # cannot pass through a keyway and a cross-hole that sit at different
    # angles, which is exactly why Г-Г, Б-Б and В-В exist on the spindle. Empty
    # means an ordinary single-plane section.
    section_path_mm: list[tuple[float, float, float]] = Field(
        default_factory=list, max_length=64
    )
    # Offset only — ступенчатый разрез, parallel planes joined by steps.
    #
    # TechDraw also offers "Aligned" (ломаный) and "NoParallel", and both
    # SEGFAULT this build: a stepped path through a flange's bolt circle killed
    # the kernel process outright, twice, and a segfault cannot be caught in
    # process — with a single uvicorn worker it takes the service down and
    # drops whatever else was in flight. Offset is measured working on the same
    # input (18 edges against the plain section's 16, so the stepped cut really
    # does catch more), so that is what the API exposes. Do not widen this
    # enum without re-testing the others against a real part.
    section_strategy: Literal["Offset"] = "Offset"
    # The letter pair a section is labelled with (Г-Г → "Г").
    section_symbol: str | None = Field(default=None, max_length=8)


class SheetDimensionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_index: int = Field(ge=0, le=5)
    edge_index: int = Field(ge=0, le=5000)
    # A diameter on a longitudinal section is measured BETWEEN the two
    # generatrices (ГОСТ 2.307 draws it straight through the axis), and one
    # edge cannot express that: the upper contour line on its own is a length,
    # not a diameter. A second reference makes the pair one dimension.
    second_edge_index: int | None = Field(default=None, ge=0, le=5000)
    kind: Literal["DistanceX", "DistanceY", "Distance", "Diameter", "Radius"] = "Distance"
    label: str | None = None


class DrawingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: Candidate
    views: list[SheetViewRequest] = Field(min_length=1, max_length=6)
    scale: float = Field(default=1.0, gt=0.0, le=100.0)
    confirm_assumptions: bool = False
    hidden_lines: bool = True
    curve_samples: int = Field(default=32, ge=4, le=200)
    dimensions: list[SheetDimensionRequest] = Field(default_factory=list, max_length=200)


def _cut_face_outlines(
    shape: Part.Shape, depth_mm: float, scale: float, samples: int,
    path: list[tuple[float, float, float]] | None = None,
) -> list[list[tuple[float, float]]]:
    """Closed outlines of the material a section plane cuts through.

    ГОСТ 2.306 hatches the cut material, and only the material — the bore of a
    hollow shaft must stay white. Taking the section view's outer boundary
    would fill the bore in, so the region is computed from the solid itself:
    intersect it with the cutting plane and take the resulting faces, which by
    construction are exactly the material and exactly not the holes.

    Coordinates come back in the same view frame TechDraw uses for its edges
    (u along the model's Z, v along X, both scaled), so the hatch lands on the
    geometry rather than beside it. The caller verifies that against the view's
    own bounds and drops the hatch instead of misplacing it.
    """
    box = shape.BoundBox
    # TechDraw centres a view on the shape, so the hatch must be centred the
    # same way — in raw model coordinates the outline lands beside the view and
    # the alignment check throws it away.
    centre_u, centre_v = box.Center.z, box.Center.x
    reach = max(box.XLength, box.YLength, box.ZLength) * 2.0 + 10.0
    if path:
        # A stepped or broken section does not cut along a PLANE, so the cut
        # surface is the sheet swept by the section path along the viewing
        # direction. Intersecting the solid with that shell gives the same
        # thing a plane gives for a straight cut: the material, and not the
        # holes.
        try:
            tool = Part.makePolygon([App.Vector(*point) for point in path])
            tool = tool.extrude(App.Vector(0.0, reach * 2.0, 0.0))
            tool.translate(App.Vector(0.0, -reach, 0.0))
        except Exception:  # noqa: BLE001
            return []
    else:
        tool = Part.makePlane(
            reach * 2, reach * 2,
            App.Vector(-reach, depth_mm, -reach),
            App.Vector(0.0, 1.0, 0.0),
        )
    try:
        cut = shape.common(tool)
    except Exception:  # noqa: BLE001 — an unsectionable shape simply has no hatch
        return []

    outlines: list[list[tuple[float, float]]] = []
    for face in cut.Faces:
        wire = face.OuterWire
        # OrderedEdges walks the wire in connection order; Edges does not, and
        # sampling them as they come produced a boundary that jumped between
        # opposite ends of the part — a bow-tie polygon whose "hatch" was a
        # diagonal line straight across the drawing.
        ordered = getattr(wire, "OrderedEdges", None) or wire.Edges
        points: list[tuple[float, float]] = []
        for edge in ordered:
            try:
                first, last = edge.FirstParameter, edge.LastParameter
                sampled = []
                for index in range(samples + 1):
                    point = edge.valueAt(first + (last - first) * index / samples)
                    sampled.append((
                        round((point.z - centre_u) * scale, 6),
                        round((point.x - centre_v) * scale, 6),
                    ))
            except Exception:  # noqa: BLE001
                continue
            # OrderedEdges walks the wire in connection order, but each edge
            # keeps its OWN parameterisation, so consecutive edges can run in
            # opposite directions. Sampling them as-is zig-zags across the
            # section, and the hatch follows a diagonal instead of the bore.
            if points and sampled:
                start_gap = math.dist(points[-1], sampled[0])
                end_gap = math.dist(points[-1], sampled[-1])
                if end_gap < start_gap:
                    sampled.reverse()
            points.extend(sampled)
        deduped: list[tuple[float, float]] = []
        for point in points:
            if not deduped or point != deduped[-1]:
                deduped.append(point)
        if len(deduped) >= 3:
            outlines.append(deduped)
    return outlines


def _techdraw_edges(view, samples: int) -> dict[str, list[dict[str, Any]]]:
    """Edges of a computed TechDraw view, in the view's own millimetres.

    Each visible edge carries its ``edge_index`` — the number a dimension
    addresses it by (``References2D = [(view, "EdgeN")]``). Without it the
    caller can SEE every edge of the view and still not name one, which is why
    dimensioning a derived sheet was impossible: the geometry came back, the
    handles did not.
    """

    def uv(point: App.Vector) -> tuple[float, float]:
        return (round(point.x, 6), round(point.y, 6))

    out: dict[str, list[dict[str, Any]]] = {"visible": [], "hidden": []}
    for key, getter in (("visible", "getVisibleEdges"), ("hidden", "getHiddenEdges")):
        if not hasattr(view, getter):
            continue
        try:
            edges = getattr(view, getter)()
        except Exception:  # noqa: BLE001 — a view with no hidden set is normal
            continue
        for index, edge in enumerate(edges or []):
            item = _project_edge(edge, 1.0, 1.0, samples)
            if item is not None:
                # Visible edges are TechDraw's Edge0..N in this order; hidden
                # ones are not addressable as dimension references, so they are
                # left unnumbered rather than given a number that means nothing.
                if key == "visible":
                    item["edge_index"] = index
                out[key].append(item)
    return out


@app.post("/drawing")
def build_drawing(request: DrawingRequest) -> dict[str, Any]:
    """A sheet's views built by TechDraw, sections included.

    ``/project`` returns raw orthographic projections; this returns what a
    DRAWING needs and that endpoint cannot express — above all a section view,
    which for a hollow turned part IS the main view. TechDraw does the cut, the
    hidden-line removal and the silhouette work that would otherwise have to be
    reimplemented against OpenCascade by hand.

    Geometry comes back in each view's own millimetres, already scaled, so the
    caller places views without re-measuring the model.
    """
    shape, warnings = _build_shape(
        CompileRequest(
            candidate=request.candidate,
            confirm_assumptions=request.confirm_assumptions,
            metadata={},
        )
    )

    document = App.newDocument("sheet")
    try:
        body = document.addObject("Part::Feature", "Body")
        body.Shape = shape
        page = document.addObject("TechDraw::DrawPage", "Page")
        page.Template = document.addObject("TechDraw::DrawSVGTemplate", "Template")

        base_view = None
        views: list[dict[str, Any]] = []
        view_objects: list[Any] = []
        for index, wanted in enumerate(request.views):
            if wanted.kind == "section":
                if base_view is None:
                    raise HTTPException(
                        422, "a section needs a base view: list a front/side/top view first"
                    )
                stepped = len(wanted.section_path_mm) >= 2
                view = document.addObject(
                    "TechDraw::DrawComplexSection" if stepped else "TechDraw::DrawViewSection",
                    f"View{index}",
                )
                page.addView(view)
                if stepped:
                    try:
                        wire = Part.makePolygon(
                            [App.Vector(*point) for point in wanted.section_path_mm]
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(
                            422, f"section path is not a valid polyline: {exc}"
                        ) from exc
                    tool = document.addObject("Part::Feature", f"Tool{index}")
                    tool.Shape = wire
                    document.recompute()
                    view.CuttingToolWireObject = tool
                    view.ProjectionStrategy = wanted.section_strategy
                if wanted.section_symbol:
                    view.SectionSymbol = wanted.section_symbol
                view.BaseView = base_view
                view.Source = [body]
                view.SectionNormal = App.Vector(*_VIEW_FRAMES["front"][0])
                # Without an explicit X the section picks its own, and it comes
                # out mirrored against the base view: the shaft was drawn with
                # its flange on the wrong end while every dimension still said
                # otherwise.
                view.XDirection = App.Vector(0.0, 0.0, 1.0)
                centre = shape.BoundBox.Center
                depth = (
                    wanted.section_origin_mm
                    if wanted.section_origin_mm is not None
                    else centre.z
                )
                view.SectionOrigin = App.Vector(centre.x, centre.y, depth)
            else:
                view = document.addObject("TechDraw::DrawViewPart", f"View{index}")
                page.addView(view)
                view.Source = [body]
                direction = _VIEW_FRAMES[wanted.kind][0]
                view.Direction = App.Vector(*direction)
                if wanted.kind == "front":
                    view.XDirection = App.Vector(0.0, 0.0, 1.0)
                if base_view is None:
                    base_view = view
            # ScaleType governs whether Scale is honoured at all: it defaults to
            # "Page", and a view left on that setting silently ignores Scale and
            # projects at the page's 1:1 — the shaft came back 470 mm wide on a
            # 1:2 sheet after the FreeCAD 1.1 upgrade, with no error anywhere.
            if "ScaleType" in view.PropertiesList:
                view.ScaleType = "Custom"
            view.Scale = request.scale
            if hasattr(view, "HardHidden"):
                view.HardHidden = bool(request.hidden_lines)
            document.recompute()
            entities = _techdraw_edges(view, request.curve_samples)
            # Circles carry a centre and a radius, not a point list; measuring
            # only "points" left every round view — a shaft seen end-on, a
            # flange — reporting no bounds at all, which the caller reads as
            # "nothing to place".
            points: list[tuple[float, float]] = []
            for group in entities.values():
                for item in group:
                    if item.get("points"):
                        points.extend(item["points"])
                    elif item.get("type") == "circle":
                        cu, cv = item["center"]
                        radius = item["radius"]
                        points.extend([(cu - radius, cv - radius), (cu + radius, cv + radius)])
            bounds = None
            if points:
                bounds = {
                    "u_min": min(p[0] for p in points), "u_max": max(p[0] for p in points),
                    "v_min": min(p[1] for p in points), "v_max": max(p[1] for p in points),
                }
            hatch: list[list[tuple[float, float]]] = []
            if wanted.kind == "section" and bounds is not None:
                # The cutting plane's own coordinate is the one along its
                # NORMAL — Y here — not the section origin's depth along the
                # part. Passing the latter put the plane past the end of the
                # shaft, where it intersects nothing, and the hatch came back
                # empty with no error to explain it.
                candidate_hatch = _cut_face_outlines(
                    shape, shape.BoundBox.Center.y, request.scale,
                    max(4, request.curve_samples // 4),
                    path=list(wanted.section_path_mm) or None,
                )
                # Fail closed on misalignment: a hatch drawn next to the view
                # instead of inside it is worse than none, and the only honest
                # check is whether it lands within the view's own bounds.
                margin = 1.0
                for outline in candidate_hatch:
                    us = [p[0] for p in outline]
                    vs = [p[1] for p in outline]
                    if (
                        min(us) >= bounds["u_min"] - margin
                        and max(us) <= bounds["u_max"] + margin
                        and min(vs) >= bounds["v_min"] - margin
                        and max(vs) <= bounds["v_max"] + margin
                    ):
                        hatch.append(outline)

            view_objects.append(view)
            views.append({
                "kind": wanted.kind,
                "label": wanted.label,
                "bounds_mm": bounds,
                "visible": entities["visible"],
                "hidden": entities["hidden"],
                # Closed outlines of the cut MATERIAL (ГОСТ 2.306) — the bore
                # is excluded by construction, so it stays white.
                "hatch": hatch,
            })
        dimensions: list[dict[str, Any]] = []
        for wanted_dim in request.dimensions:
            if wanted_dim.view_index >= len(view_objects):
                continue
            owner = view_objects[wanted_dim.view_index]
            dim = document.addObject("TechDraw::DrawViewDimension", f"Dim{len(dimensions)}")
            page.addView(dim)
            dim.Type = wanted_dim.kind
            references = [(owner, f"Edge{wanted_dim.edge_index}")]
            if wanted_dim.second_edge_index is not None:
                references.append((owner, f"Edge{wanted_dim.second_edge_index}"))
            dim.References2D = references
            try:
                document.recompute()
                anchors = [
                    (round(point.x, 6), round(point.y, 6))
                    for point in dim.getLinearPoints()
                ]
            except Exception as exc:  # noqa: BLE001 — one dimension, not the sheet
                warnings.append(f"dimension on edge {wanted_dim.edge_index}: {exc}")
                continue
            if len(anchors) < 2:
                continue
            dimensions.append({
                "view_index": wanted_dim.view_index,
                "kind": wanted_dim.kind,
                "label": wanted_dim.label,
                # MEASURED off the model, in the view's own millimetres. The
                # arrows and the dimension line are the caller's to draw:
                # TechDraw computes those in its GUI renderer, and headless
                # getArrowPositions returns zeros.
                "anchors_mm": anchors,
                "value_mm": round(
                    math.dist(anchors[0], anchors[1]) / request.scale, 6
                ),
            })

        return {
            "views": views, "dimensions": dimensions,
            "scale": request.scale, "warnings": warnings,
            "kernel": "FreeCAD/TechDraw",
        }
    finally:
        App.closeDocument(document.Name)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "kernel": "FreeCAD/OpenCascade",
        "freecad_version": ".".join(part for part in App.Version()[:3] if part),
    }


def _brep_report(shape: "Part.Shape", metadata: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """D4: honest B-Rep validity + mass properties, not a hardcoded 'valid'.

    - ``valid``: OCC topology/geometry check (BRepCheck via Shape.isValid()).
    - ``manifold``: the solid is closed/watertight (a non-manifold or open
      shell cannot be a real part).
    - mass properties: surface area, centre of mass, and mass when the
      material density is known (metadata.density_kg_m3).
    """
    problems = list(warnings)
    try:
        valid = bool(shape.isValid())
    except Exception as exc:  # noqa: BLE001
        valid, problems = False, [*problems, f"проверка B-Rep не выполнена: {exc}"]
    try:
        manifold = bool(shape.isClosed())
    except Exception:  # noqa: BLE001
        manifold = False
    if not valid:
        problems.append("B-Rep невалиден (OpenCascade BRepCheck)")
    if not manifold:
        problems.append("модель не замкнута (не manifold/не watertight)")

    volume_mm3 = float(shape.Volume)
    report: dict[str, Any] = {
        "valid": valid and manifold,
        "brep_valid": valid,
        "manifold": manifold,
        "solid_count": len(shape.Solids),
        "volume_mm3": volume_mm3,
        "surface_area_mm2": float(shape.Area),
        "warnings": problems,
    }
    try:
        com = shape.CenterOfMass
        report["center_of_mass_mm"] = {"x": com.x, "y": com.y, "z": com.z}
    except Exception:  # noqa: BLE001
        pass
    density = metadata.get("density_kg_m3") if isinstance(metadata, dict) else None
    if isinstance(density, (int, float)) and density > 0:
        # volume mm³ → m³ (1e-9), × kg/m³
        report["mass_kg"] = round(volume_mm3 * 1e-9 * float(density), 6)
        report["density_kg_m3"] = float(density)
    return report


@app.post("/compile")
def compile_candidate(request: CompileRequest) -> Response:
    cosmetic_threads = _cosmetic_threads(request)
    shape, warnings = _build_shape(request)
    document = App.newDocument("EngineeringModel")
    try:
        model = document.addObject("Part::Feature", "Model")
        model.Label = request.candidate.label
        model.Shape = shape
        model.addProperty("App::PropertyString", "EngineeringMetadata", "Traceability")
        model.addProperty("App::PropertyString", "FeatureTree", "Traceability")
        model.EngineeringMetadata = json.dumps(request.metadata, ensure_ascii=False, sort_keys=True)
        model.FeatureTree = request.candidate.model_dump_json()
        document.recompute()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            step_path = root / "model.step"
            iges_path = root / "model.iges"
            fcstd_path = root / "model.FCStd"
            stl_path = root / "model.stl"
            report_path = root / "report.json"

            shape.exportStep(str(step_path))
            # D4: exact-geometry IGES export alongside STEP.
            try:
                shape.exportIges(str(iges_path))
            except Exception:  # noqa: BLE001 — IGES is a bonus format, never fatal
                iges_path = None
            document.saveAs(str(fcstd_path))
            Mesh.export([model], str(stl_path))
            bounds = shape.BoundBox
            report_path.write_text(
                json.dumps(
                    {
                        **_brep_report(shape, request.metadata, warnings),
                        "bounds_mm": {
                            "x": bounds.XLength,
                            "y": bounds.YLength,
                            "z": bounds.ZLength,
                        },
                        "edges": _edge_descriptors(shape),
                        "cosmetic_threads": cosmetic_threads,
                        "kernel": "FreeCAD/OpenCascade",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            payload = io.BytesIO()
            exports = [step_path, fcstd_path, stl_path, report_path]
            if iges_path is not None:
                exports.append(iges_path)
            with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in exports:
                    if not path.exists() or path.stat().st_size == 0:
                        raise HTTPException(500, f"CAD export {path.name} is empty")
                    archive.write(path, path.name)
            return Response(payload.getvalue(), media_type="application/zip")
    finally:
        App.closeDocument(document.Name)
