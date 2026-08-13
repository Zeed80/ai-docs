"""Deterministic corruptions for the EMG-to-generator admission boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.ai.emg_regression import build_regression_graph
from app.domain.engineering_model_graph import (
    BuildGenerator,
    EngineeringModelGraph,
    UnknownValue,
    compile_build_plan,
)
from app.services.engineering_model_graph import evaluate_build_admission


def _replace_assertion(
    graph: EngineeringModelGraph,
    predicate: str,
    *,
    unknown_reason: str | None = None,
    unit: str | None = None,
) -> EngineeringModelGraph:
    found = False
    assertions = []
    for item in graph.assertions:
        if not found and item.state == "active" and item.predicate == predicate:
            found = True
            updates: dict[str, Any] = {}
            if unknown_reason is not None:
                updates.update({
                    "value": UnknownValue(kind="unknown", reason=unknown_reason),
                    "assurance": "proposed",
                })
            if unit is not None:
                updates["unit"] = unit
            item = item.model_copy(update=updates)
        assertions.append(item)
    if not found:
        raise ValueError(f"corruption predicate not found: {predicate}")
    return graph.model_copy(update={
        "assertions": assertions,
        "canonical_sha256": "",
    }).sealed()


def _pending_outputs(graph: EngineeringModelGraph, predicates: set[str]) -> set[str]:
    return {
        item.id for item in graph.assertions
        if item.state == "active" and item.predicate in predicates
    }


def run_emg_admission_corruption(
    manifest: dict[str, Any], *, fixture_root: Path
) -> dict[str, Any]:
    cases_by_id = {item["id"]: item for item in manifest["cases"]}
    mechanical = build_regression_graph(
        cases_by_id["mechanical-detal-126-v2"], fixture_root
    )
    assembly = build_regression_graph(
        cases_by_id["assembly-fixed-shaft"], fixture_root
    )
    construction = build_regression_graph(
        cases_by_id["construction-wall-opening"], fixture_root
    )
    system = build_regression_graph(
        cases_by_id["hydraulic-closed-loop"], fixture_root
    )

    scenarios: list[dict[str, Any]] = []

    def evaluate(
        *,
        scenario_id: str,
        source_group_id: str,
        drawing_class: str,
        graph: EngineeringModelGraph,
        target_id: str,
        generator: BuildGenerator,
        expected_codes: set[str],
        pending: set[str] | None = None,
    ) -> None:
        report = evaluate_build_admission(
            graph,
            target_id,
            generator,
            pending_output_assertion_ids=pending,
        )
        actual = {item.code for item in report.blockers}
        passed = not report.allowed and expected_codes <= actual
        scenarios.append({
            "id": scenario_id,
            "source_group_id": source_group_id,
            "drawing_class": drawing_class,
            "passed": passed,
            "generator_allowed": report.allowed,
            "expected_blocker_codes": sorted(expected_codes),
            "actual_blocker_codes": sorted(actual),
            "critical_miss": not passed,
        })

    evaluate(
        scenario_id="mechanical-canonical-hash-tamper",
        source_group_id="fixture:detal-126",
        drawing_class="rotation_body",
        graph=mechanical.model_copy(update={"canonical_sha256": "0" * 64}),
        target_id="preview",
        generator="mechanical_brep",
        expected_codes={"verification_canonical_hash_mismatch"},
    )
    evaluate(
        scenario_id="mechanical-unknown-unit",
        source_group_id="fixture:detal-126",
        drawing_class="rotation_body",
        graph=_replace_assertion(
            mechanical, "operation.param.length_mm", unit="inch"
        ),
        target_id="preview",
        generator="mechanical_brep",
        expected_codes={"verification_unknown_unit"},
    )
    evaluate(
        scenario_id="mechanical-wrong-domain-generator",
        source_group_id="fixture:detal-126",
        drawing_class="rotation_body",
        graph=mechanical,
        target_id="preview",
        generator="construction_ifc",
        expected_codes={
            "generator_profile_incompatible",
            "generator_target_incompatible",
        },
    )
    evaluate(
        scenario_id="assembly-component-count-unknown",
        source_group_id="fixture:assembly-fixed-shaft",
        drawing_class="assembly",
        graph=_replace_assertion(
            assembly, "component.quantity", unknown_reason="corruption injection"
        ),
        target_id="production",
        generator="assembly_step",
        pending=_pending_outputs(assembly, {
            "assembly.artifact_reopen_valid",
            "assembly.required_2d_complete",
        }),
        expected_codes={"critical_parameter_unknown"},
    )
    corrupted_system = _replace_assertion(
        system,
        "system.connectivity_closed",
        unknown_reason="corruption injection",
    )
    system_plan = compile_build_plan(corrupted_system, "production")
    connectivity = next(
        item for item in corrupted_system.assertions
        if item.state == "active"
        and item.predicate == "system.connectivity_closed"
    )
    system_passed = (
        not system_plan.production_export_allowed
        and connectivity.id in system_plan.critical_assumption_ids
    )
    scenarios.append({
        "id": "system-connectivity-unknown",
        "source_group_id": "fixture:hydraulic-closed-loop",
        "drawing_class": "hydraulic_system",
        "passed": system_passed,
        "generator_allowed": system_plan.production_export_allowed,
        "expected_blocker_codes": ["critical_connectivity_unknown"],
        "actual_blocker_codes": (
            ["critical_connectivity_unknown"] if system_passed else []
        ),
        "critical_miss": not system_passed,
    })
    evaluate(
        scenario_id="construction-material-unknown",
        source_group_id="fixture:construction-wall-opening",
        drawing_class="architectural_bim",
        graph=_replace_assertion(
            construction, "element.material", unknown_reason="corruption injection"
        ),
        target_id="production",
        generator="construction_ifc",
        pending=_pending_outputs(construction, {
            "construction.ifc_reopen_valid",
            "construction.required_sheets_complete",
        }),
        expected_codes={"critical_parameter_unknown"},
    )
    missed = sum(bool(item["critical_miss"]) for item in scenarios)
    return {
        "schema_version": "emg-admission-corruption-report/1.0",
        "split": "dev",
        "scenario_count": len(scenarios),
        "passed": missed == 0,
        "critical_misses": missed,
        "critical_miss_rate": round(missed / len(scenarios), 8),
        "invented_geometry_admitted": sum(
            item["generator_allowed"] for item in scenarios
            if "tamper" in item["id"]
        ),
        "scenarios": scenarios,
    }


def load_and_run(manifest_path: Path) -> dict[str, Any]:
    return run_emg_admission_corruption(
        json.loads(manifest_path.read_text()), fixture_root=manifest_path.parent
    )
