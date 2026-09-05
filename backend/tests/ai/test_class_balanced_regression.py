import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ai.class_balanced_regression import evaluate_class_balanced_manifest

FIXTURE = Path(__file__).parents[1] / "fixtures" / "cad_class_balanced_dev.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _passed() -> dict:
    return {"status": "passed", "evidence_ids": ["test:stage"]}


def test_dev_baseline_is_macro_balanced_and_honestly_not_promotion_ready():
    report = evaluate_class_balanced_manifest(_payload())

    assert report["accepted"] is True
    assert report["promotion_eligible"] is False
    assert report["source_group_count"] == 4
    assert report["macro"]["stages"]["graph"]["pass_rate"] == 1.0
    assert report["macro"]["stages"]["reader"]["coverage_rate"] == 1.0
    assert {item["code"] for item in report["promotion_failures"]} >= {
        "stage_evidence_not_validated",
        "safety_evidence_not_validated",
    }


def test_many_variants_in_one_group_do_not_outweigh_rare_class_failure():
    payload = _payload()
    mechanical = payload["cases"][0]
    payload["cases"] = [deepcopy(mechanical) for _ in range(10)] + [deepcopy(payload["cases"][2])]
    for index, item in enumerate(payload["cases"][:10]):
        item["id"] = f"mechanical-variant-{index}"
        item["stages"]["reader"] = _passed()
        item["stages"]["model_3d_bim"] = _passed()
        item["stages"]["drawing_2d"] = _passed()
        item["safety"] = {
            "status": "evaluated",
            "invented_geometry": False,
            "critical_miss": False,
            "evidence_ids": ["test:safety"],
        }
    construction = payload["cases"][-1]
    construction["stages"]["reader"] = _passed()
    construction["stages"]["model_3d_bim"] = {"status": "failed"}
    construction["stages"]["drawing_2d"] = _passed()
    construction["safety"] = {
        "status": "evaluated",
        "invented_geometry": False,
        "critical_miss": False,
        "evidence_ids": ["test:safety"],
    }
    payload["required_classes"] = ["rotation_body", "architectural_bim"]

    report = evaluate_class_balanced_manifest(payload)

    assert report["source_group_count"] == 2
    assert report["macro"]["stages"]["model_3d_bim"]["pass_rate"] == 0.5
    assert report["by_class"]["architectural_bim"]["stages"]["model_3d_bim"]["pass_rate"] == 0.0


def test_sealed_holdout_rows_are_rejected_by_the_dev_contract():
    payload = _payload()
    payload["cases"][0]["split"] = "holdout"

    with pytest.raises(ValidationError, match="split"):
        evaluate_class_balanced_manifest(payload)


def test_not_applicable_stage_is_excluded_from_macro_denominator():
    payload = _payload()
    report = evaluate_class_balanced_manifest(payload)

    system_stage = report["by_class"]["hydraulic_system"]["stages"]["model_3d_bim"]
    assert system_stage["applicable"] is False
    assert system_stage["coverage_rate"] is None
    assert system_stage["pass_rate"] is None
    assert report["macro"]["stages"]["model_3d_bim"]["applicable_class_count"] == 3


def test_not_applicable_stage_requires_an_explicit_reason():
    payload = _payload()
    payload["cases"][3]["stages"]["model_3d_bim"] = {"status": "not_applicable"}

    with pytest.raises(ValidationError, match="reason code"):
        evaluate_class_balanced_manifest(payload)


def test_invented_geometry_blocks_promotion_even_when_every_stage_passes():
    payload = _payload()
    for item in payload["cases"]:
        item["stages"] = {
            stage: _passed() for stage in ("reader", "graph", "model_3d_bim", "drawing_2d")
        }
        item["safety"] = {
            "status": "evaluated",
            "invented_geometry": item["id"] == "mechanical-detal-126-v2",
            "critical_miss": False,
            "evidence_ids": ["test:safety"],
        }

    report = evaluate_class_balanced_manifest(payload)

    assert report["promotion_eligible"] is False
    assert "invented_geometry_detected" in {item["code"] for item in report["promotion_failures"]}


def test_per_class_regression_is_rejected_even_if_macro_can_be_preserved():
    payload = _payload()
    for item in payload["cases"]:
        item["stages"] = {
            stage: _passed() for stage in ("reader", "graph", "model_3d_bim", "drawing_2d")
        }
        item["safety"] = {
            "status": "evaluated",
            "invented_geometry": False,
            "critical_miss": False,
            "evidence_ids": ["test:safety"],
        }
    baseline = evaluate_class_balanced_manifest(payload)
    regressed = deepcopy(payload)
    regressed["cases"][2]["stages"]["drawing_2d"] = {
        "status": "failed",
        "failure_clusters": ["projection"],
    }

    report = evaluate_class_balanced_manifest(regressed, baseline=baseline)

    assert report["accepted"] is False
    assert report["promotion_eligible"] is False
    assert report["regression"]["status"] == "failed"
    assert {
        (item.get("class"), item.get("stage"), item["code"])
        for item in report["regression"]["regressions"]
    } >= {("architectural_bim", "drawing_2d", "stage_pass_rate_regression")}


def test_external_safety_evidence_must_match_case_provenance():
    payload = _payload()
    report = {
        "schema_version": "emg-admission-corruption-report/1.0",
        "scenarios": [
            {
                "id": evidence_id,
                "source_group_id": "fixture:detal-126",
                "drawing_class": "rotation_body",
                "generator_allowed": False,
                "critical_miss": False,
            }
            for evidence_id in payload["cases"][0]["safety"]["evidence_ids"]
        ],
    }

    with pytest.raises(ValueError, match="safety evidence not found"):
        evaluate_class_balanced_manifest(payload, safety_report=report)
