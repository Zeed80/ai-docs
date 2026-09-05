"""Deterministic golden regression for the supported EMG domain adapters."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.ai.assembly_emg import assembly_as_graph
from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph
from app.ai.cad_solid import feature_tree_from_spec
from app.ai.construction_emg import ConstructionModel, construction_as_graph
from app.ai.system_emg import EngineeringSystemModel, system_as_graph
from app.domain.assembly import analyze_assembly_dof
from app.domain.engineering_model_graph import EngineeringModelGraph, compile_build_plan
from app.services.engineering_model_graph import verify_graph


def build_regression_graph(case: dict[str, Any], fixture_root: Path) -> EngineeringModelGraph:
    case_id = str(case["id"])
    kind = case["kind"]
    payload = case["input"]
    if kind == "mechanical_spec":
        spec = json.loads((fixture_root / payload["fixture"]).read_text())
        candidate = feature_tree_from_spec(spec)
        if candidate is None:
            raise ValueError(f"{case_id}: mechanical feature tree was not compiled")
        return spec_feature_tree_as_graph(spec, candidate, graph_id=f"emg-regression:{case_id}")
    if kind == "assembly":
        components = payload["components"]
        mates = payload["mates"]
        return assembly_as_graph(
            graph_id=f"emg-regression:{case_id}",
            name=payload["name"],
            designation=payload.get("designation"),
            components=components,
            mates=mates,
            dof=analyze_assembly_dof(components, mates),
            collisions=[tuple(item) for item in payload.get("collisions", [])],
            exact_checked=payload.get("exact_checked", []),
            interference_degraded=payload.get("interference_degraded"),
        )
    if kind == "construction":
        return construction_as_graph(
            graph_id=f"emg-regression:{case_id}",
            model=ConstructionModel.model_validate(payload["model"]),
            source_revision_id=payload["source_revision_id"],
            source_approved=payload["source_approved"],
        )
    if kind == "system":
        return system_as_graph(
            graph_id=f"emg-regression:{case_id}",
            model=EngineeringSystemModel.model_validate(payload["model"]),
            source_revision_id=payload["source_revision_id"],
            source_approved=payload["source_approved"],
        )
    raise ValueError(f"{case_id}: unsupported regression kind {kind!r}")


def _case_result(case: dict[str, Any], fixture_root: Path) -> tuple[dict[str, Any], list[str]]:
    graph = build_regression_graph(case, fixture_root)
    repeated = build_regression_graph(case, fixture_root)
    preview = compile_build_plan(graph, "preview")
    production = compile_build_plan(graph, "production")
    state, issues = verify_graph(graph)
    result: dict[str, Any] = {
        "profile": graph.profile,
        "canonical_sha256": graph.canonical_sha256,
        "production_artifact_hash": production.artifact_hash,
        "nodes_by_type": dict(sorted(Counter(item.type for item in graph.nodes).items())),
        "edges_by_type": dict(sorted(Counter(item.type for item in graph.edges).items())),
        "assertion_count": len(graph.assertions),
        "critical_assumption_ids": production.critical_assumption_ids,
        "preview_export_allowed": preview.production_export_allowed,
        "production_export_allowed": production.production_export_allowed,
        "checked_levels": state.checked_levels,
        "error_codes": sorted(item["code"] for item in issues if item["severity"] == "error"),
    }
    if case["kind"] == "mechanical_spec":
        projected = feature_tree_from_graph(graph, target_id="preview")
        result["feature_kinds"] = [item.kind for item in projected.features]
    failures = []
    if repeated.canonical_sha256 != graph.canonical_sha256:
        failures.append("canonical graph is not deterministic")
    if compile_build_plan(repeated, "production").artifact_hash != production.artifact_hash:
        failures.append("production build plan is not deterministic")
    for key, expected in case["expected"].items():
        if result.get(key) != expected:
            failures.append(f"{key}: expected {expected!r}, got {result.get(key)!r}")
    return result, failures


def run_emg_regression(manifest: dict[str, Any], *, fixture_root: Path) -> dict[str, Any]:
    if manifest.get("schema_version") != "emg-regression/1.0":
        raise ValueError("unsupported EMG regression manifest schema")
    cases = []
    passed = True
    for case in manifest.get("cases", []):
        actual, failures = _case_result(case, fixture_root)
        passed = passed and not failures
        cases.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "passed": not failures,
                "failures": failures,
                "actual": actual,
            }
        )
    if not cases:
        raise ValueError("EMG regression manifest has no cases")
    return {
        "schema_version": "emg-regression-report/1.0",
        "passed": passed,
        "case_count": len(cases),
        "cases": cases,
    }
