from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_cad_drawing_graphs import match_parameter_values, summarize_results


def _result(
    *,
    matched: int,
    total: int,
    passed: bool,
    accepted: bool = True,
    false_accept: bool = False,
    dxf_reopens: bool = True,
) -> dict:
    return {
        "parameters_matched": matched,
        "parameters_total": total,
        "passed": passed,
        "accepted": accepted,
        "false_accept": false_accept,
        "dxf_reopens": dxf_reopens,
    }


def test_parameter_matching_is_one_to_one_for_duplicate_nominals() -> None:
    assert match_parameter_values([10.0, 10.0], [10.0, 10.0, 10.0]) == 2


def test_summary_uses_corpus_micro_parameter_accuracy() -> None:
    summary = summarize_results([
        _result(matched=9, total=10, passed=False),
        _result(matched=1, total=1, passed=True),
    ])

    assert summary["parameter_accuracy"] == 10 / 11
    assert summary["exact_graph_rate"] == 0.5


def test_false_accept_rate_counts_green_status_with_wrong_truth() -> None:
    summary = summarize_results([
        _result(matched=1, total=1, passed=True),
        _result(matched=0, total=1, passed=False, false_accept=True),
        _result(matched=0, total=1, passed=False, accepted=False),
    ])

    assert summary["accepted_cases"] == 2
    assert summary["false_accepts"] == 1
    assert summary["false_accept_rate"] == 0.5
