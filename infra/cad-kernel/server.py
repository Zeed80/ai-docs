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
    model_config = ConfigDict(extra="forbid")

    kind: Literal["extrude", "hole", "boss", "pocket", "fillet", "chamfer"]
    source_entity_ids: list[str] = Field(default_factory=list, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    features: list[Feature] = Field(min_length=1, max_length=500)
    score: float = Field(ge=0, le=1)
    label: str = Field(min_length=1, max_length=500)
    missing_data: list[str] = Field(default_factory=list, max_length=500)


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


def _build_shape(request: CompileRequest) -> tuple[Part.Shape, list[str]]:
    extrudes = [feature for feature in request.candidate.features if feature.kind == "extrude"]
    if len(extrudes) != 1:
        raise HTTPException(422, "Exactly one base extrude is required")
    if request.candidate.missing_data and not request.confirm_assumptions:
        raise HTTPException(409, "Explicit confirmation of feature-tree assumptions is required")

    base = extrudes[0]
    width = _number(base.params, "width_mm")
    height = _number(base.params, "height_mm")
    depth = _number(base.params, "depth_mm")
    shape = Part.makeBox(width, height, depth)
    warnings: list[str] = []

    for feature in request.candidate.features:
        if feature.kind not in ("boss", "pocket"):
            continue
        profile = feature.params.get("profile")
        x = _number(feature.params, "center_x_mm")
        y = _number(feature.params, "center_y_mm")
        operation_depth = _number(feature.params, "depth_mm")
        if feature.kind == "pocket" and operation_depth > depth + 1e-6:
            raise HTTPException(422, "Pocket depth exceeds base depth")
        z = depth if feature.kind == "boss" else depth - operation_depth
        solid_height = operation_depth if feature.kind == "boss" else operation_depth + 1.0
        if profile == "circle":
            diameter = _number(feature.params, "diameter_mm", maximum=min(width, height) * 2)
            radius = diameter / 2
            if x - radius < -1e-6 or y - radius < -1e-6 or x + radius > width + 1e-6 or y + radius > height + 1e-6:
                raise HTTPException(422, f"{feature.kind} lies outside the base footprint")
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
        if feature.kind not in ("fillet", "chamfer"):
            continue
        key = feature.params.get("edge_key")
        if not isinstance(key, str):
            raise HTTPException(422, "Edge operation requires edge_key")
        edge = _find_edge(shape, key)
        size = _number(feature.params, "size_mm", maximum=max(width, height, depth))
        try:
            shape = (
                shape.makeFillet(size, [edge])
                if feature.kind == "fillet"
                else shape.makeChamfer(size, [edge])
            )
        except Exception as exc:
            raise HTTPException(422, f"OpenCascade rejected {feature.kind}: {exc}") from exc
        if shape.isNull() or not shape.isValid():
            raise HTTPException(422, f"OpenCascade produced invalid geometry after {feature.kind}")

    for feature in request.candidate.features:
        if feature.kind != "hole":
            continue
        diameter = _number(feature.params, "diameter_mm", maximum=min(width, height) * 2)
        x = _number(feature.params, "center_x_mm")
        y = _number(feature.params, "center_y_mm")
        radius = diameter / 2
        if x - radius < -1e-6 or y - radius < -1e-6 or x + radius > width + 1e-6 or y + radius > height + 1e-6:
            raise HTTPException(422, "Hole lies outside the base footprint")
        through = feature.params.get("through")
        if through is True:
            cutter = Part.makeCylinder(
                radius,
                shape.BoundBox.ZMax - shape.BoundBox.ZMin + 2.0,
                App.Vector(x, y, shape.BoundBox.ZMin - 1.0),
            )
        elif through is False:
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
        shape = shape.cut(cutter)

    shape = shape.removeSplitter()
    if shape.isNull() or not shape.isValid() or shape.Volume <= 0:
        raise HTTPException(422, "OpenCascade produced an invalid or empty solid")
    return shape, warnings


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "kernel": "FreeCAD/OpenCascade",
        "freecad_version": ".".join(part for part in App.Version()[:3] if part),
    }


@app.post("/compile")
def compile_candidate(request: CompileRequest) -> Response:
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
            fcstd_path = root / "model.FCStd"
            stl_path = root / "model.stl"
            report_path = root / "report.json"

            shape.exportStep(str(step_path))
            document.saveAs(str(fcstd_path))
            Mesh.export([model], str(stl_path))
            bounds = shape.BoundBox
            report_path.write_text(
                json.dumps(
                    {
                        "valid": True,
                        "solid_count": len(shape.Solids),
                        "volume_mm3": shape.Volume,
                        "bounds_mm": {
                            "x": bounds.XLength,
                            "y": bounds.YLength,
                            "z": bounds.ZLength,
                        },
                        "warnings": warnings,
                        "edges": _edge_descriptors(shape),
                        "kernel": "FreeCAD/OpenCascade",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in (step_path, fcstd_path, stl_path, report_path):
                    if not path.exists() or path.stat().st_size == 0:
                        raise HTTPException(500, f"CAD export {path.name} is empty")
                    archive.write(path, path.name)
            return Response(payload.getvalue(), media_type="application/zip")
    finally:
        App.closeDocument(document.Name)
