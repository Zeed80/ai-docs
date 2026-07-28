from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_cad_readers import score_parameters, summarize_results


def test_reader_parameter_accuracy_is_one_to_one_and_micro_aggregated() -> None:
    reference = {
        "main_view": {
            "outer": [
                {"diameter_mm": 80, "length_mm": 40},
                {"diameter_mm": 80, "length_mm": 60},
            ]
        }
    }
    prediction = {
        "main_view": {
            "outer": [{"diameter_mm": 80, "length_mm": 40}]
        }
    }

    score = score_parameters(prediction, reference)

    assert score["parameters_matched"] == 2
    assert score["parameters_total"] == 4
    assert score["parameter_accuracy"] == 0.5


def test_reader_summary_exposes_false_success_claims() -> None:
    summary = summarize_results([
        {
            "validates": True,
            "parameters_matched": 9,
            "parameters_total": 10,
            "success_claimed": True,
            "false_accept": True,
        },
        {
            "validates": True,
            "parameters_matched": 10,
            "parameters_total": 10,
            "success_claimed": True,
            "false_accept": False,
        },
    ])

    assert summary["parameter_accuracy"] == 0.95
    assert summary["false_accept_rate"] == 0.5


def test_invalid_reader_answer_cannot_disappear_from_parameter_denominator() -> None:
    summary = summarize_results([{
        "validates": False,
        "parameters_matched": 0,
        "parameters_total": 31,
        "success_claimed": False,
        "false_accept": False,
    }])

    assert summary["valid_specs"] == 0
    assert summary["parameters_total"] == 31
    assert summary["parameter_accuracy"] == 0.0
