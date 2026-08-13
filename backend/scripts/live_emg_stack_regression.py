#!/usr/bin/env python3
"""Live multi-domain regression against production engineering runtimes.

This is intentionally separate from the deterministic golden suite.  It calls
the running FreeCAD/OpenCascade service, reopens exported assembly STEP files,
builds and reopens IFC through the backend's installed IfcOpenShell runtime,
and executes the canonical system graph verifier in the production container.
Every case records the exact evidence it checked; a backend-only system case is
never presented as a CAD-kernel check.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.assembly_emg import (
    assembly_as_graph,
    assembly_drawing_patch,
    build_assembly_drawing_svg,
)
from app.ai.construction_emg import (
    ConstructionModel,
    build_construction_sheets_svg,
    compile_construction_ifc,
    construction_as_graph,
    construction_sheets_patch,
)
from app.ai.system_emg import (
    EngineeringSystemModel,
    build_system_diagram_svg,
    system_as_graph,
    system_diagram_patch,
)
from app.domain.assembly import analyze_assembly_dof
from app.domain.engineering_model_graph import apply_graph_patch, compile_build_plan
from app.services.engineering_model_graph import verify_graph

KERNEL_URL = os.environ.get("CAD_KERNEL_URL", "http://cad-kernel:8092").rstrip("/")
ARTIFACT_DIR: Path | None = None


def _evidence_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)

    def default(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json", by_alias=True)
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=default)
        + "\n"
    ).encode()


def _write_evidence_bundle(case_id: str, files: dict[str, Any]) -> dict[str, Any]:
    """Persist the live evidence chain and a content-addressed manifest."""
    if ARTIFACT_DIR is None:
        return {"saved": False, "reason": "artifact_dir_not_requested"}
    case_dir = ARTIFACT_DIR / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, value in sorted(files.items()):
        body = _evidence_bytes(value)
        (case_dir / name).write_bytes(body)
        entries.append({
            "path": name,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })
    manifest = {
        "schema_version": "emg-live-evidence/1.0",
        "case_id": case_id,
        "complete": True,
        "files": entries,
    }
    manifest_body = _evidence_bytes(manifest)
    (case_dir / "manifest.json").write_bytes(manifest_body)
    return {
        "saved": True,
        "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
        "file_count": len(entries),
    }


def _feature(kind: str, **params: object) -> dict[str, Any]:
    return {"kind": kind, "params": params, "confidence": 1.0}


def _candidate(label: str, *features: dict[str, Any]) -> dict[str, Any]:
    return {
        "features": list(features),
        "score": 1.0,
        "label": label,
        "missing_data": [],
        "correspondences": [],
    }


def _post(path: str, payload: dict[str, Any], timeout: int = 300) -> tuple[int, bytes, str]:
    request = urllib.request.Request(
        f"{KERNEL_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")


def _json_body(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))


def _zip_members(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _mechanical_cases() -> list[tuple[str, dict[str, Any]]]:
    shaft_profile = [
        {"r": 40.0, "z": 0.0},
        {"r": 40.0, "z": 120.0},
        {"r": 55.0, "z": 120.0},
        {"r": 55.0, "z": 260.0},
        {"r": 32.0, "z": 260.0},
        {"r": 32.0, "z": 360.0},
    ]
    return [
        (
            "mechanical-stepped-shaft",
            _candidate(
                "Stepped shaft",
                _feature("revolve", profile_points=shaft_profile),
                _feature(
                    "groove",
                    axial_position_mm=190.0,
                    width_mm=8.0,
                    depth_mm=3.0,
                ),
                _feature(
                    "hole",
                    axis="radial",
                    diameter_mm=12.0,
                    axial_position_mm=310.0,
                    center_x_mm=0.0,
                    center_y_mm=0.0,
                    through=True,
                ),
            ),
        ),
        (
            "mechanical-rounded-plate",
            _candidate(
                "Rounded mounting plate",
                _feature(
                    "extrude",
                    width_mm=160.0,
                    height_mm=100.0,
                    depth_mm=12.0,
                    corner_radius_mm=12.0,
                ),
                _feature(
                    "hole",
                    diameter_mm=18.0,
                    center_x_mm=40.0,
                    center_y_mm=30.0,
                    through=True,
                ),
                _feature(
                    "hole",
                    diameter_mm=18.0,
                    center_x_mm=120.0,
                    center_y_mm=70.0,
                    through=True,
                ),
            ),
        ),
        (
            "mechanical-flange",
            _candidate(
                "Pipe flange",
                _feature(
                    "revolve",
                    profile_points=[
                        {"r": 120.0, "z": 0.0},
                        {"r": 120.0, "z": 24.0},
                    ],
                ),
                _feature(
                    "hole",
                    diameter_mm=70.0,
                    center_x_mm=0.0,
                    center_y_mm=0.0,
                    through=True,
                ),
                _feature(
                    "hole",
                    diameter_mm=16.0,
                    center_x_mm=85.0,
                    center_y_mm=0.0,
                    through=True,
                ),
            ),
        ),
    ]


def _run_mechanical(case_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    compile_payload = {
        "candidate": candidate,
        "confirm_assumptions": True,
        "metadata": {"live_case": case_id},
    }
    status, body, _ = _post(
        "/compile",
        compile_payload,
    )
    if status != 200:
        raise RuntimeError(f"/compile HTTP {status}: {body[:400].decode(errors='replace')}")
    members = _zip_members(body)
    report = json.loads(members["report.json"])
    repeated_status, repeated_body, _ = _post("/compile", compile_payload)
    if repeated_status != 200:
        raise RuntimeError(
            f"repeated /compile HTTP {repeated_status}: "
            f"{repeated_body[:400].decode(errors='replace')}"
        )
    repeated_members = _zip_members(repeated_body)
    repeated_report = json.loads(repeated_members["report.json"])
    status, projection_body, _ = _post(
        "/project",
        {"candidate": candidate, "views": ["front", "side", "top"], "confirm_assumptions": True},
    )
    if status != 200:
        raise RuntimeError(f"/project HTTP {status}: {projection_body[:400].decode(errors='replace')}")
    projection = _json_body(projection_body)
    views = projection.get("views", {})
    primitive_count = sum(
        len(view.get("visible", [])) + len(view.get("hidden", []))
        for view in views.values()
    )
    checks = {
        "brep_valid": bool(report.get("brep_valid")),
        "manifold": bool(report.get("manifold")),
        "single_solid": report.get("solid_count") == 1,
        "step_signature": members.get("model.step", b"")[:16].startswith(b"ISO-10303-21"),
        "step_reopen_valid": bool(report.get("reopen", {}).get("valid")),
        "step_reopen_sha_matches": (
            report.get("reopen", {}).get("step_sha256")
            == hashlib.sha256(members["model.step"]).hexdigest()
        ),
        "artifact_deterministic": (
            members["model.step"] == repeated_members["model.step"]
            and report.get("reopen", {}).get("step_sha256")
            == repeated_report.get("reopen", {}).get("step_sha256")
        ),
        "all_views_present": set(views) == {"front", "side", "top"},
        "projection_nonempty": primitive_count > 0,
    }
    evidence_bundle = _write_evidence_bundle(case_id, {
        "source.json": candidate,
        "reader-graph.json": {
            "status": "not_applicable",
            "reason": "mechanical kernel case starts from a reviewed feature candidate",
        },
        "generator-payload.json": compile_payload,
        "model.step": members["model.step"],
        "drawing-projection.json": projection,
        "validation-report.json": report,
        "audit-trace.json": {"checks": checks, "runtime": KERNEL_URL},
    })
    return {
        "runtime": "cad-kernel FreeCAD/OpenCascade + TechDraw",
        "evidence_level": "live_kernel_build_and_projection",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(members["model.step"]).hexdigest(),
        "volume_mm3": report.get("volume_mm3"),
        "topology": {key: report.get(key) for key in ("solid_count", "face_count", "edge_count", "vertex_count")},
        "projection_primitive_count": primitive_count,
        "evidence_bundle": evidence_bundle,
    }


def _assembly_cases() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "assembly-bearing-block",
            {
                "name": "Bearing block assembly",
                "components": [
                    {"key": "base", "shape": {"kind": "box", "width_mm": 180, "height_mm": 100, "depth_mm": 20}, "transform": {}},
                    {"key": "left-support", "shape": {"kind": "box", "width_mm": 20, "height_mm": 100, "depth_mm": 80}, "transform": {"translate": [0, 0, 20]}},
                    {"key": "right-support", "shape": {"kind": "box", "width_mm": 20, "height_mm": 100, "depth_mm": 80}, "transform": {"translate": [160, 0, 20]}},
                ],
                "drawing_mates": [
                    {"id": "base-left", "mate_type": "fixed", "first_instance_key": "base", "second_instance_key": "left-support", "parameters": {}},
                    {"id": "base-right", "mate_type": "fixed", "first_instance_key": "base", "second_instance_key": "right-support", "parameters": {}},
                ],
                "metadata": {"live_case": "assembly-bearing-block"},
            },
        ),
        (
            "assembly-pump-skid",
            {
                "name": "Pump skid assembly",
                "components": [
                    {"key": "skid", "shape": {"kind": "box", "width_mm": 400, "height_mm": 220, "depth_mm": 20}, "transform": {}},
                    {"key": "motor", "shape": {"kind": "cylinder", "diameter_mm": 120, "height_mm": 220}, "transform": {"translate": [90, 110, 20], "rotate_z_deg": 0}},
                    {"key": "pump", "shape": {"kind": "cylinder", "diameter_mm": 90, "height_mm": 150}, "transform": {"translate": [270, 110, 20], "rotate_z_deg": 0}},
                ],
                "drawing_mates": [
                    {"id": "skid-motor", "mate_type": "fixed", "first_instance_key": "skid", "second_instance_key": "motor", "parameters": {}},
                    {"id": "skid-pump", "mate_type": "fixed", "first_instance_key": "skid", "second_instance_key": "pump", "parameters": {}},
                ],
                "metadata": {"live_case": "assembly-pump-skid"},
            },
        ),
    ]


def _run_assembly(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    kernel_payload = {key: value for key, value in payload.items() if key != "drawing_mates"}
    status, body, _ = _post("/assembly/compile", kernel_payload)
    if status != 200:
        raise RuntimeError(f"/assembly/compile HTTP {status}: {body[:400].decode(errors='replace')}")
    members = _zip_members(body)
    report = json.loads(members["assembly-report.json"])
    reopen = report.get("reopen", {})
    step = members["assembly.step"]
    mates = payload.get("drawing_mates", [])
    svg, drawing_report = build_assembly_drawing_svg(
        components=payload["components"], mates=mates, name=payload["name"]
    )
    repeated_svg, repeated_drawing_report = build_assembly_drawing_svg(
        components=payload["components"], mates=mates, name=payload["name"]
    )
    graph_components = [
        {
            "instance_key": item["key"],
            "designation": item["key"],
            "quantity": 1,
            "metadata_": {"grounded": index == 0, "shape": item["shape"]},
            "transform": item.get("transform", {}),
        }
        for index, item in enumerate(payload["components"])
    ]
    dof = analyze_assembly_dof(graph_components, mates)
    graph = assembly_as_graph(
        graph_id=f"live-regression:{case_id}",
        name=payload["name"],
        designation=case_id,
        components=graph_components,
        mates=mates,
        dof=dof,
        collisions=[],
        exact_checked=[item["key"] for item in payload["components"]],
        interference_degraded=None,
    )
    drawing_graph = apply_graph_patch(
        graph,
        assembly_drawing_patch(graph, svg=svg, report=drawing_report),
    )
    drawing_plan = compile_build_plan(drawing_graph, "production")
    checks = {
        "assembly_brep_valid": bool(report.get("assembly", {}).get("brep_valid")),
        "component_count": len(report.get("components", [])) == len(payload["components"]),
        "reopen_valid": bool(reopen.get("valid")),
        "reopened_solid_count": reopen.get("solid_count") == len(payload["components"]),
        "reopen_sha_matches": reopen.get("step_sha256") == hashlib.sha256(step).hexdigest(),
        "step_signature": step[:16].startswith(b"ISO-10303-21"),
        "drawing_svg_reopened": bool(drawing_report.get("valid")),
        "drawing_deterministic": (
            svg == repeated_svg and drawing_report == repeated_drawing_report
        ),
        "drawing_has_both_views": drawing_report.get("views") == ["assembled", "exploded"],
        "drawing_covers_instances": drawing_report.get("instance_occurrences") == 2 * len(payload["components"]),
        "drawing_covers_mates": drawing_report.get("mate_occurrences") == 2 * len(mates),
        "drawing_release_gate_admitted": "assertion:assembly:required-2d" not in drawing_plan.critical_assumption_ids,
    }
    evidence_bundle = _write_evidence_bundle(case_id, {
        "source.json": payload,
        "reader-graph.json": graph,
        "generator-payload.json": kernel_payload,
        "model.step": step,
        "drawing.svg": svg,
        "validation-report.json": report,
        "audit-trace.json": {
            "checks": checks,
            "drawing_report": drawing_report,
            "build_plan": drawing_plan,
        },
    })
    return {
        "runtime": "cad-kernel FreeCAD/OpenCascade isolated STEP reopen",
        "evidence_level": "live_kernel_build_and_independent_reopen",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(step).hexdigest(),
        "drawing_artifact_sha256": hashlib.sha256(svg).hexdigest(),
        "component_count": len(payload["components"]),
        "reopen": reopen,
        "evidence_bundle": evidence_bundle,
    }


def _construction_cases() -> list[tuple[str, ConstructionModel]]:
    return [
        (
            "construction-room-opening",
            ConstructionModel.model_validate({
                "site_name": "Live site A",
                "building_name": "Room and opening",
                "storeys": [{"id": "l1", "name": "Ground floor", "elevation_mm": 0}],
                "elements": [
                    {"id": "wall", "kind": "wall", "name": "External wall", "storey_id": "l1", "material": "Concrete C30/37", "load_bearing": True, "box": {"x_mm": 0, "y_mm": 0, "z_mm": 0, "width_mm": 6000, "depth_mm": 250, "height_mm": 3200}},
                    {"id": "door", "kind": "opening", "name": "Door opening", "storey_id": "l1", "host_id": "wall", "box": {"x_mm": 1200, "y_mm": 0, "z_mm": 0, "width_mm": 1000, "depth_mm": 250, "height_mm": 2200}},
                    {"id": "room", "kind": "space", "name": "Room 101", "storey_id": "l1", "box": {"x_mm": 250, "y_mm": 250, "z_mm": 0, "width_mm": 5500, "depth_mm": 4200, "height_mm": 3000}},
                ],
            }),
        ),
        (
            "construction-two-storey-frame",
            ConstructionModel.model_validate({
                "site_name": "Live site B",
                "building_name": "Two storey frame",
                "storeys": [
                    {"id": "l1", "name": "Level 1", "elevation_mm": 0},
                    {"id": "l2", "name": "Level 2", "elevation_mm": 3600},
                ],
                "elements": [
                    {"id": "slab-1", "kind": "slab", "name": "Ground slab", "storey_id": "l1", "material": "Concrete C25/30", "load_bearing": True, "box": {"x_mm": 0, "y_mm": 0, "z_mm": 0, "width_mm": 8000, "depth_mm": 6000, "height_mm": 250}},
                    {"id": "column-1", "kind": "column", "name": "Column L1", "storey_id": "l1", "material": "Concrete C30/37", "load_bearing": True, "box": {"x_mm": 500, "y_mm": 500, "z_mm": 250, "width_mm": 400, "depth_mm": 400, "height_mm": 3350}},
                    {"id": "slab-2", "kind": "slab", "name": "Level 2 slab", "storey_id": "l2", "material": "Concrete C25/30", "load_bearing": True, "box": {"x_mm": 0, "y_mm": 0, "z_mm": 0, "width_mm": 8000, "depth_mm": 6000, "height_mm": 250}},
                    {"id": "column-2", "kind": "column", "name": "Column L2", "storey_id": "l2", "material": "Concrete C30/37", "load_bearing": True, "box": {"x_mm": 500, "y_mm": 500, "z_mm": 250, "width_mm": 400, "depth_mm": 400, "height_mm": 3350}},
                ],
            }),
        ),
    ]


def _run_construction(case_id: str, model: ConstructionModel) -> dict[str, Any]:
    artifact, report = compile_construction_ifc(model)
    repeated_artifact, repeated_report = compile_construction_ifc(model)
    sheets_svg, sheets_report = build_construction_sheets_svg(model)
    repeated_sheets_svg, repeated_sheets_report = build_construction_sheets_svg(model)
    graph = construction_as_graph(
        graph_id=f"live-regression:{case_id}",
        model=model,
        source_revision_id=f"live:{case_id}:r1",
        source_approved=True,
    )
    sheets_graph = apply_graph_patch(
        graph,
        construction_sheets_patch(
            graph, svg=sheets_svg, report=sheets_report
        ),
    )
    sheets_plan = compile_build_plan(sheets_graph, "production")
    checks = {
        "ifc_signature": artifact.startswith(b"ISO-10303-21"),
        "reopen_valid": bool(report.get("valid")),
        "schema_ifc4": report.get("schema") == "IFC4",
        "all_products_represented": report.get("represented_product_count") == len(model.elements),
        "geometry_reopened": not report.get("geometry_failures"),
        "artifact_sha_matches": report.get("ifc_sha256") == hashlib.sha256(artifact).hexdigest(),
        "artifact_deterministic": (
            artifact == repeated_artifact
            and report.get("ifc_sha256") == repeated_report.get("ifc_sha256")
        ),
        "sheets_svg_reopened": bool(sheets_report.get("valid")),
        "sheets_deterministic": (
            sheets_svg == repeated_sheets_svg
            and sheets_report == repeated_sheets_report
        ),
        "all_storey_plans_and_section_present": (
            len(sheets_report.get("views", [])) == len(model.storeys) + 1
            and "section" in sheets_report.get("views", [])
        ),
        "sheets_cover_all_elements": (
            sheets_report.get("element_occurrences") == 2 * len(model.elements)
        ),
        "sheets_release_gate_admitted": (
            "assertion:construction:required-sheets"
            not in sheets_plan.critical_assumption_ids
        ),
    }
    evidence_bundle = _write_evidence_bundle(case_id, {
        "source.json": model,
        "reader-graph.json": graph,
        "generator-payload.json": model,
        "model.ifc": artifact,
        "drawing.svg": sheets_svg,
        "validation-report.json": report,
        "audit-trace.json": {
            "checks": checks,
            "sheets_report": sheets_report,
            "build_plan": sheets_plan,
        },
    })
    return {
        "runtime": "production backend IfcOpenShell geometry runtime",
        "evidence_level": "live_ifc_build_geometry_and_reopen",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "sheets_artifact_sha256": hashlib.sha256(sheets_svg).hexdigest(),
        "storey_count": len(model.storeys),
        "element_count": len(model.elements),
        "product_class_counts": report.get("product_class_counts"),
        "entity_count": report.get("entity_count"),
        "evidence_bundle": evidence_bundle,
    }


def _system_cases() -> list[tuple[str, EngineeringSystemModel]]:
    return [
        (
            "system-hydraulic-power",
            EngineeringSystemModel.model_validate({
                "profile": "hydraulic",
                "name": "Hydraulic power line",
                "system_kind": "hydraulic_power",
                "equipment": [
                    {"id": "tank", "name": "Tank", "equipment_type": "reservoir"},
                    {"id": "pump", "name": "Pump", "equipment_type": "pump"},
                    {"id": "actuator", "name": "Cylinder", "equipment_type": "actuator"},
                ],
                "ports": [
                    {"id": "tank-out", "equipment_id": "tank", "kind": "suction", "direction": "out", "medium": "oil", "nominal_size_mm": 32},
                    {"id": "pump-in", "equipment_id": "pump", "kind": "suction", "direction": "in", "medium": "oil", "nominal_size_mm": 32},
                    {"id": "pump-out", "equipment_id": "pump", "kind": "pressure", "direction": "out", "medium": "oil", "nominal_size_mm": 20},
                    {"id": "actuator-in", "equipment_id": "actuator", "kind": "pressure", "direction": "in", "medium": "oil", "nominal_size_mm": 20},
                ],
                "connections": [
                    {"id": "suction", "first_port_id": "tank-out", "second_port_id": "pump-in"},
                    {"id": "pressure", "first_port_id": "pump-out", "second_port_id": "actuator-in"},
                ],
            }),
        ),
        (
            "system-electrical-feeder",
            EngineeringSystemModel.model_validate({
                "profile": "electrical",
                "name": "Electrical feeder",
                "system_kind": "low_voltage_distribution",
                "equipment": [
                    {"id": "panel", "name": "Main panel", "equipment_type": "switchboard"},
                    {"id": "load-a", "name": "Motor A", "equipment_type": "motor"},
                    {"id": "load-b", "name": "Motor B", "equipment_type": "motor"},
                ],
                "ports": [
                    {"id": "panel-a", "equipment_id": "panel", "kind": "feeder", "direction": "out", "medium": "electricity", "max_connections": 1},
                    {"id": "panel-b", "equipment_id": "panel", "kind": "feeder", "direction": "out", "medium": "electricity", "max_connections": 1},
                    {"id": "motor-a", "equipment_id": "load-a", "kind": "supply", "direction": "in", "medium": "electricity"},
                    {"id": "motor-b", "equipment_id": "load-b", "kind": "supply", "direction": "in", "medium": "electricity"},
                ],
                "connections": [
                    {"id": "feeder-a", "first_port_id": "panel-a", "second_port_id": "motor-a"},
                    {"id": "feeder-b", "first_port_id": "panel-b", "second_port_id": "motor-b"},
                ],
            }),
        ),
    ]


def _run_system(case_id: str, model: EngineeringSystemModel) -> dict[str, Any]:
    graph = system_as_graph(
        graph_id=f"live-regression:{case_id}",
        model=model,
        source_revision_id=f"live:{case_id}:r1",
        source_approved=True,
    )
    repeated = system_as_graph(
        graph_id=f"live-regression:{case_id}",
        model=model,
        source_revision_id=f"live:{case_id}:r1",
        source_approved=True,
    )
    svg, diagram_report = build_system_diagram_svg(model)
    released = apply_graph_patch(
        graph,
        system_diagram_patch(graph, svg=svg, report=diagram_report),
    )
    state, issues = verify_graph(released)
    error_codes = sorted(item["code"] for item in issues if item["severity"] == "error")
    checks = {
        "connectivity_closed": not model.unresolved_port_ids(),
        "canonical_graph_deterministic": graph.canonical_sha256 == repeated.canonical_sha256,
        "diagram_svg_reopened": bool(diagram_report.get("valid")),
        "diagram_covers_all_objects": (
            len(diagram_report.get("equipment_ids", [])) == len(model.equipment)
            and len(diagram_report.get("port_ids", [])) == len(model.ports)
            and len(diagram_report.get("connection_ids", [])) == len(model.connections)
        ),
        "all_verifier_levels_executed": state.checked_levels == list(range(1, 13)),
        "no_release_errors": error_codes == [],
        "production_export_allowed": compile_build_plan(
            released, "production"
        ).production_export_allowed,
    }
    evidence_bundle = _write_evidence_bundle(case_id, {
        "source.json": model,
        "reader-graph.json": graph,
        "generator-payload.json": model,
        "diagram.svg": svg,
        "validation-report.json": {
            "state": state,
            "issues": issues,
            "diagram_report": diagram_report,
        },
        "audit-trace.json": {"checks": checks, "released_graph": released},
    })
    return {
        "runtime": "production backend EMG adapter, deterministic SVG builder and verifier",
        "evidence_level": "live_backend_semantic_svg_reopen_and_release_gate",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(svg).hexdigest(),
        "canonical_sha256": graph.canonical_sha256,
        "equipment_count": len(model.equipment),
        "port_count": len(model.ports),
        "connection_count": len(model.connections),
        "error_codes": error_codes,
        "evidence_bundle": evidence_bundle,
    }


def run() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    groups = [
        ("mechanical", _mechanical_cases(), _run_mechanical),
        ("assembly", _assembly_cases(), _run_assembly),
        ("construction", _construction_cases(), _run_construction),
        ("system", _system_cases(), _run_system),
    ]
    for domain, domain_cases, runner in groups:
        for case_id, payload in domain_cases:
            try:
                result = runner(case_id, payload)
            except Exception as exc:  # noqa: BLE001 - preserve the live failure in the report
                result = {
                    "runtime": "failed before runtime evidence was complete",
                    "evidence_level": "live_failure",
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            cases.append({"id": case_id, "domain": domain, **result})
            print(f"{'PASS' if result['passed'] else 'FAIL'}  {case_id}", flush=True)
    return {
        "schema_version": "emg-live-stack-regression/1.0",
        "kernel_url": KERNEL_URL,
        "passed": all(item["passed"] for item in cases),
        "case_count": len(cases),
        "domains": sorted({item["domain"] for item in cases}),
        "cases": cases,
    }


def main() -> int:
    global ARTIFACT_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    ARTIFACT_DIR = args.artifact_dir
    report = run()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(rendered + "\n")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
