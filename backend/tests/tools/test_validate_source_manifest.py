from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "validate_source_manifest.py"
SPEC = importlib.util.spec_from_file_location("validate_source_manifest", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _row(**changes):
    row = {
        "schema_version": 2,
        "source_id": "source",
        "source_group_id": "source:part",
        "domain": "mechanical",
        "profile": "mechanical",
        "drawing_class": "mechanical_component",
        "asset_format": "step",
        "truth_layers": ["brep_geometry"],
        "license": "CC BY 4.0",
        "sha256": "0" * 64,
        "split": "train",
        "relative_path": "part.step",
        "output_path": "/not/read/in-this-test.step",
    }
    row.update(changes)
    return row


def test_valid_v2_row_passes_without_file_check():
    report = MODULE.validate_rows([_row()], verify_files=False)
    assert report["ok"] is True
    assert report["assets"] == 1


def test_format_cannot_claim_wrong_truth_layer():
    report = MODULE.validate_rows(
        [_row(asset_format="ifc", truth_layers=["brep_geometry"])],
        verify_files=False,
    )
    assert report["ok"] is False
    assert {issue["code"] for issue in report["issues"]} == {"truth_layers"}


def test_source_group_cannot_leak_between_splits():
    report = MODULE.validate_rows(
        [
            _row(relative_path="part-a.step", split="train"),
            _row(relative_path="part-b.step", split="holdout"),
        ],
        verify_files=False,
    )
    assert report["ok"] is False
    assert "source_group_leakage" in {issue["code"] for issue in report["issues"]}


def test_legacy_profile_must_match_domain():
    report = MODULE.validate_rows(
        [_row(domain="construction", profile="mechanical")], verify_files=False
    )
    assert report["ok"] is False
    assert "legacy_profile_mismatch" in {issue["code"] for issue in report["issues"]}
