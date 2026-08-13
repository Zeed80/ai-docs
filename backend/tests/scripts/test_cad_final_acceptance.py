from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from cad_final_acceptance import _score_domain


def _report(**overrides):
    summary = {
        "entity_precision": 0.995,
        "entity_recall": 0.995,
        "exact_sheet_rate": 0.99,
        "false_exact_rate": 0.0,
        "errors": 0,
    }
    summary.update(overrides)
    return {"summary": summary}


def test_final_gate_accepts_only_all_exact_thresholds():
    assert _score_domain(_report())["passed"] is True
    assert _score_domain(_report(entity_recall=0.994999))["passed"] is False
    assert _score_domain(_report(false_exact_rate=0.000001))["passed"] is False
    assert _score_domain(_report(errors=1))["passed"] is False
