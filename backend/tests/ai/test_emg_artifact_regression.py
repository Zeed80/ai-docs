import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.ai.class_balanced_regression import evaluate_class_balanced_manifest
from app.ai.emg_artifact_regression import run_emg_artifact_regression
from app.ai.emg_live_stage import mechanical_live_stage_report
from app.ai.emg_regression import run_emg_regression

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _mechanical_live_report() -> dict:
    checks = {
        key: True
        for key in (
            "brep_valid",
            "manifold",
            "single_solid",
            "step_signature",
            "step_reopen_valid",
            "step_reopen_sha_matches",
            "artifact_deterministic",
            "all_views_present",
            "projection_nonempty",
        )
    }
    return mechanical_live_stage_report(
        "mechanical-detal-126-v2",
        {
            "checks": checks,
            "artifact_sha256": "a" * 64,
            "projection_primitive_count": 534,
        },
    )


def _domain_build_report() -> dict:
    return {
        "schema_version": "emg-domain-build-report/1.0",
        "split": "dev",
        "passed": True,
        "evidence": [
            {
                "id": f"build:{case_id}:model_3d_bim",
                "case_id": case_id,
                "stage": "model_3d_bim",
                "passed": True,
            }
            for case_id in (
                "assembly-fixed-shaft",
                "construction-wall-opening",
            )
        ],
    }


def _manifest() -> dict:
    return json.loads((FIXTURES / "emg_domain_golden.json").read_text())


def test_required_2d_artifacts_are_deterministic_and_verify_cleanly():
    first = run_emg_artifact_regression(_manifest(), fixture_root=FIXTURES)
    second = run_emg_artifact_regression(_manifest(), fixture_root=FIXTURES)

    assert first == second
    assert first["passed"] is True
    assert first["case_count"] == 3
    assert all(item["passed"] for item in first["cases"])
    assert all(item["required_views_complete"] for item in first["cases"])
    assert all(not item["verification_error_codes"] for item in first["cases"])
    assert (
        next(item for item in first["cases"] if item["kind"] == "system")[
            "production_export_allowed"
        ]
        is True
    )


def test_incomplete_system_connectivity_fails_the_2d_artifact_gate():
    manifest = deepcopy(_manifest())
    system = next(item for item in manifest["cases"] if item["kind"] == "system")
    system["input"]["model"]["connections"] = []

    report = run_emg_artifact_regression(manifest, fixture_root=FIXTURES)
    system_result = next(item for item in report["cases"] if item["kind"] == "system")

    assert report["passed"] is False
    assert system_result["passed"] is False
    assert "2D artifact did not pass" in system_result["failures"][0]


def test_class_balanced_passes_require_matching_graph_and_artifact_evidence():
    manifest = _manifest()
    pipeline = json.loads((FIXTURES / "cad_class_balanced_dev.json").read_text())
    graph_report = run_emg_regression(manifest, fixture_root=FIXTURES)
    artifact_report = run_emg_artifact_regression(manifest, fixture_root=FIXTURES)

    report = evaluate_class_balanced_manifest(
        pipeline,
        evidence_reports=[
            graph_report,
            artifact_report,
            _mechanical_live_report(),
            _domain_build_report(),
        ],
    )

    assert report["macro"]["stages"]["graph"]["pass_rate"] == 1.0
    assert report["macro"]["stages"]["drawing_2d"]["pass_rate"] == 1.0
    broken = deepcopy(artifact_report)
    broken["cases"][0]["passed"] = False
    with pytest.raises(ValueError, match="stage evidence did not pass"):
        evaluate_class_balanced_manifest(
            pipeline,
            evidence_reports=[
                graph_report,
                broken,
                _mechanical_live_report(),
                _domain_build_report(),
            ],
        )
