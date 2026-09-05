import json
from pathlib import Path

from app.ai.class_balanced_regression import evaluate_class_balanced_manifest
from app.ai.emg_corruption_regression import run_emg_admission_corruption

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_all_dev_admission_corruptions_fail_closed():
    manifest = json.loads((FIXTURES / "emg_domain_golden.json").read_text())

    report = run_emg_admission_corruption(manifest, fixture_root=FIXTURES)

    assert report["scenario_count"] == 6
    assert report["passed"] is True
    assert report["critical_misses"] == 0
    assert report["critical_miss_rate"] == 0.0
    assert report["invented_geometry_admitted"] == 0
    assert all(not item["generator_allowed"] for item in report["scenarios"])


def test_class_balanced_safety_claims_are_backed_by_corruption_report():
    emg_manifest = json.loads((FIXTURES / "emg_domain_golden.json").read_text())
    pipeline_manifest = json.loads((FIXTURES / "cad_class_balanced_dev.json").read_text())
    safety_report = run_emg_admission_corruption(emg_manifest, fixture_root=FIXTURES)

    report = evaluate_class_balanced_manifest(pipeline_manifest, safety_report=safety_report)

    assert report["by_class"]["rotation_body"]["safety_coverage_rate"] == 1.0
    assert report["by_class"]["assembly"]["critical_miss_rate"] == 0.0
    assert report["by_class"]["architectural_bim"]["invented_geometry_rate"] == 0.0
    assert report["by_class"]["hydraulic_system"]["safety_coverage_rate"] == 1.0
