import json
from pathlib import Path

from app.ai.emg_regression import run_emg_regression


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_four_domain_golden_manifest_passes_and_is_deterministic():
    manifest = json.loads((FIXTURES / "emg_domain_golden.json").read_text())

    first = run_emg_regression(manifest, fixture_root=FIXTURES)
    second = run_emg_regression(manifest, fixture_root=FIXTURES)

    assert first == second
    assert first["passed"] is True
    assert first["case_count"] == 4
    assert {item["actual"]["profile"] for item in first["cases"]} == {
        "mechanical", "assembly", "construction", "hydraulic",
    }


def test_golden_manifest_fails_closed_on_expected_hash_drift():
    manifest = json.loads((FIXTURES / "emg_domain_golden.json").read_text())
    manifest["cases"][0]["expected"]["canonical_sha256"] = "0" * 64

    report = run_emg_regression(manifest, fixture_root=FIXTURES)

    assert report["passed"] is False
    assert "canonical_sha256" in report["cases"][0]["failures"][0]
