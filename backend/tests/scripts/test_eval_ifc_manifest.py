from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_ifc_manifest import _counter_prf, _promotion_decision, _relative_error


def _passing_metrics():
    return {
        "reference_ok": True,
        "candidate_ok": True,
        "product_class_f1": 1.0,
        "storey_count_match": True,
        "space_count_match": True,
        "containment_relative_error": 0.0,
        "candidate_geometry_failures": 0,
        "bbox_max_relative_error": 0.0,
        "volume_relative_error": 0.0,
    }


def test_counter_prf_scores_multiset_classes():
    metrics = _counter_prf({"IfcWall": 2, "IfcDoor": 1}, {"IfcWall": 1, "IfcSlab": 1})
    assert metrics == {
        "matched": 1,
        "false_positive": 1,
        "false_negative": 2,
        "precision": 0.5,
        "recall": 1 / 3,
        "f1": 0.4,
    }


def test_relative_error_is_safe_for_zero_reference():
    assert _relative_error(0, 0) == 0
    assert _relative_error(1, 0) > 1e6


def test_pair_promotion_requires_semantics_and_geometry():
    eligible, failures = _promotion_decision(_passing_metrics())
    assert eligible is True
    assert failures == []

    metrics = _passing_metrics()
    metrics["product_class_f1"] = 0.8
    metrics["volume_relative_error"] = 0.2
    eligible, failures = _promotion_decision(metrics)
    assert eligible is False
    assert failures == ["product_class_mismatch", "geometry_volume_mismatch"]
