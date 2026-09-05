"""Deterministic dev regression for EMG-derived required 2D artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.ai.assembly_emg import (
    assembly_drawing_patch,
    build_assembly_drawing_svg,
)
from app.ai.construction_emg import (
    ConstructionModel,
    build_construction_sheets_svg,
    construction_sheets_patch,
)
from app.ai.emg_regression import build_regression_graph
from app.ai.system_emg import (
    EngineeringSystemModel,
    build_system_diagram_svg,
    system_diagram_patch,
)
from app.domain.engineering_model_graph import apply_graph_patch, compile_build_plan
from app.services.engineering_model_graph import verify_graph


def _build_artifact(case: dict[str, Any]) -> tuple[bytes, dict[str, Any], Any]:
    payload = case["input"]
    kind = case["kind"]
    if kind == "assembly":
        svg, report = build_assembly_drawing_svg(
            components=payload["drawing_components"],
            mates=payload["mates"],
            name=payload["name"],
        )
        return svg, report, assembly_drawing_patch
    if kind == "construction":
        model = ConstructionModel.model_validate(payload["model"])
        svg, report = build_construction_sheets_svg(model)
        return svg, report, construction_sheets_patch
    if kind == "system":
        model = EngineeringSystemModel.model_validate(payload["model"])
        svg, report = build_system_diagram_svg(model)
        return svg, report, system_diagram_patch
    raise ValueError(f"{case['id']}: no deterministic 2D artifact adapter for {kind}")


def _case_result(case: dict[str, Any], fixture_root: Path) -> dict[str, Any]:
    graph = build_regression_graph(case, fixture_root)
    first_svg, first_report, patch_builder = _build_artifact(case)
    second_svg, second_report, _ = _build_artifact(case)
    failures: list[str] = []
    if first_svg != second_svg or first_report != second_report:
        failures.append("2D artifact builder is not deterministic")
    if not first_report.get("valid"):
        failures.append("2D artifact did not pass its independent reopen report")
    errors: list[str] = []
    production_export_allowed = False
    remaining_critical = compile_build_plan(graph, "production").critical_assumption_ids
    if first_report.get("valid"):
        patched = apply_graph_patch(graph, patch_builder(graph, svg=first_svg, report=first_report))
        state, issues = verify_graph(patched)
        errors = sorted(item["code"] for item in issues if item["severity"] == "error")
        if "required_2d_artifacts_missing" in state.issue_codes:
            failures.append("required 2D release gate remains unresolved")
        if errors:
            failures.append(f"patched graph verification errors: {errors}")
        plan = compile_build_plan(patched, "production")
        production_export_allowed = plan.production_export_allowed
        remaining_critical = plan.critical_assumption_ids
    return {
        "id": f"artifact:{case['id']}",
        "case_id": case["id"],
        "kind": case["kind"],
        "stage": "drawing_2d",
        "passed": not failures,
        "failures": failures,
        "artifact_sha256": first_report["artifact_sha256"],
        "report_sha256": first_report["canonical_report_sha256"],
        "required_views_complete": bool(first_report.get("required_views_complete")),
        "production_export_allowed": production_export_allowed,
        "remaining_critical_assumption_ids": remaining_critical,
        "verification_error_codes": errors,
    }


def run_emg_artifact_regression(manifest: dict[str, Any], *, fixture_root: Path) -> dict[str, Any]:
    if manifest.get("schema_version") != "emg-regression/1.0":
        raise ValueError("unsupported EMG regression manifest schema")
    cases = [
        _case_result(case, fixture_root)
        for case in manifest.get("cases", [])
        if case.get("kind") in {"assembly", "construction", "system"}
    ]
    if not cases:
        raise ValueError("EMG manifest has no supported deterministic 2D cases")
    return {
        "schema_version": "emg-artifact-regression-report/1.0",
        "split": "dev",
        "stage": "drawing_2d",
        "case_count": len(cases),
        "passed": all(item["passed"] for item in cases),
        "cases": cases,
    }


def load_and_run(manifest_path: Path) -> dict[str, Any]:
    return run_emg_artifact_regression(
        json.loads(manifest_path.read_text()), fixture_root=manifest_path.parent
    )
