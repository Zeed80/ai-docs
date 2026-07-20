from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_cad_manifest import aggregate


def _record(*, tp: int, fp: int, fn: int, exact: bool, false_exact: bool = False):
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "entity_metrics": {
            "micro": {
                "matched": tp,
                "false_positive": fp,
                "false_negative": fn,
            },
            "per_type": {
                "segment": {
                    "matched": tp,
                    "false_positive": fp,
                    "false_negative": fn,
                }
            },
        },
        "exact_sheet": exact,
        "false_exact": false_exact,
        "diagnostic_precision": precision,
        "diagnostic_recall": recall,
    }


def test_aggregate_is_micro_entity_accuracy_not_mean_of_sheet_scores() -> None:
    summary = aggregate(
        [
            _record(tp=99, fp=1, fn=1, exact=False, false_exact=True),
            _record(tp=1, fp=0, fn=0, exact=True),
        ]
    )

    assert summary["entity_precision"] == 0.990099
    assert summary["entity_recall"] == 0.990099
    assert summary["entity_f1"] == 0.990099
    assert summary["exact_sheet_rate"] == 0.5
    assert summary["false_exact_rate"] == 0.5


def test_declines_cannot_disappear_from_file_count() -> None:
    summary = aggregate([_record(tp=1, fp=0, fn=0, exact=True), {"declined": True}])

    assert summary["files"] == 2
    assert summary["evaluated_files"] == 1
    assert summary["declined_files"] == 1
