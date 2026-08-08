from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_pmi_manifest import evaluate, validate_candidate, validate_reference


def _record(identifier: str = "nist:ctc:01:atc:1"):
    return {
        "schema_version": "nist-pmi-truth/1.0",
        "semantic_id": identifier,
        "suite": "ctc",
        "primary_case_id": "1",
        "category": "Geometric Tolerances",
        "description": "Profile tolerance",
        "specification": "⌓ | 0.5 | A",
        "evidence": {},
        "assurance": {
            "semantic_status": "source_defined",
            "geometry_linked": False,
            "drawing_located": False,
        },
    }


def test_self_pair_has_perfect_semantics_but_cannot_promote_incomplete_truth():
    record = _record()
    report = evaluate([record], [record])
    assert report["semantic_f1"] == 1.0
    assert report["exact_source_spelling_accuracy"] == 1.0
    assert report["reference_promotion_ready"] is False
    assert report["promotion_eligible"] is False
    assert report["promotion_failures"] == [
        "reference_geometry_links_incomplete",
        "reference_drawing_locations_incomplete",
    ]


def test_corrupted_specification_is_missing_and_invented():
    reference = _record()
    candidate = {**reference, "specification": "⌓ | 5.0 | A"}
    report = evaluate([reference], [candidate])
    assert report["semantic_precision"] == 0.0
    assert report["semantic_recall"] == 0.0
    assert report["missing_pmi_rate"] == 1.0
    assert report["invented_pmi_rate"] == 1.0


def test_false_verified_claim_is_reported():
    reference = _record()
    candidate = {
        **reference,
        "assurance": {
            "semantic_status": "verified",
            "geometry_linked": False,
            "drawing_located": False,
        },
    }
    report = evaluate([reference], [candidate])
    assert report["false_verified_claims"] == 1
    assert report["false_exact_rate"] == 1.0
    assert "false_verified_claims" in report["promotion_failures"]


def test_complete_associations_can_pass_all_gates():
    reference = _record()
    reference["evidence"] = {
        "topology_targets": ["face-12"],
        "drawing_regions": ["page-1:box-3"],
    }
    reference["assurance"] = {
        "semantic_status": "source_defined",
        "geometry_linked": True,
        "drawing_located": True,
    }
    candidate = {
        **reference,
        "assurance": {
            "semantic_status": "verified",
            "geometry_linked": True,
            "drawing_located": True,
        },
    }
    report = evaluate([reference], [candidate])
    assert report["geometry_attachment"]["f1"] == 1.0
    assert report["drawing_location"]["f1"] == 1.0
    assert report["promotion_eligible"] is True


def test_reference_and_candidate_duplicates_fail_closed():
    record = _record()
    with pytest.raises(ValueError, match="duplicate reference"):
        validate_reference([record, record])
    with pytest.raises(ValueError, match="duplicate candidate"):
        validate_candidate([record, record])
