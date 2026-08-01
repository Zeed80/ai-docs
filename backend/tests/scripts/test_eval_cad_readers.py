from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_cad_readers import (
    _PreferredCadReaderRouter,
    reader_trace,
    score_parameters,
    summarize_results,
)


def test_reader_trace_keeps_fragment_and_whole_sheet_attempts_separate() -> None:
    fragment = {
        "pass": 1,
        "mode": "fragments",
        "spec": {"fragment_answers": [{"prompt": "profile"}]},
    }
    whole = {"pass": 1, "mode": "whole_sheet", "spec": {}}

    trace = reader_trace({
        "reader_attempts": [whole],
        "fragment_reader_attempts": [fragment],
        "fragments": {"geometry": True},
    })

    assert trace["fragment_attempts"] == [fragment]
    assert trace["whole_sheet_attempts"] == [whole]
    assert trace["fragments"] == {"geometry": True}


def test_candidate_router_pins_reader_but_preserves_ocr_assignment() -> None:
    import asyncio

    class Request:
        def __init__(self, task: str, preferred_model: str | None = None) -> None:
            self.task = task
            self.preferred_model = preferred_model

        def model_copy(self, *, update: dict) -> "Request":
            return Request(self.task, update.get("preferred_model"))

    class Router:
        def __init__(self) -> None:
            self.requests = []

        async def run(self, request):
            self.requests.append(request)
            return request

    router = Router()
    candidate = _PreferredCadReaderRouter(router, "candidate-vlm")

    reader = asyncio.run(candidate.run(Request("cad_spec_read", "production-vlm")))
    ocr = asyncio.run(candidate.run(Request("cad_text_ocr", "document-ocr")))

    assert reader.preferred_model == "candidate-vlm"
    assert ocr.preferred_model == "document-ocr"


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


def test_reader_scores_external_and_internal_threads_on_their_carriers() -> None:
    reference = {
        "main_view": {
            "outer": [{
                "diameter_mm": 75,
                "length_mm": 18,
                "thread": {
                    "nominal_diameter_mm": 75,
                    "pitch_mm": 1.5,
                    "length_mm": 18,
                    "internal": False,
                },
            }],
            "bore": [{
                "diameter_mm": 55,
                "length_mm": 25,
                "thread": {
                    "nominal_diameter_mm": 54.5,
                    "pitch_mm": 2,
                    "length_mm": 25,
                    "internal": True,
                },
            }],
        }
    }
    prediction = {
        "main_view": {
            "outer": [{
                "diameter_mm": 75,
                "length_mm": 18,
                "thread": {
                    "nominal_diameter_mm": 75,
                    "pitch_mm": 1.5,
                    "length_mm": 18,
                    "internal": False,
                },
            }],
            # The right numeric thread on the wrong (external) carrier must not
            # satisfy the expected internal-thread groups.
            "bore": [{"diameter_mm": 55, "length_mm": 25}],
        }
    }

    score = score_parameters(prediction, reference)

    assert score["parameter_details"]["outer.thread.pitch_mm"]["matched"] == 1
    assert score["parameter_details"]["bore.thread.pitch_mm"]["matched"] == 0
    assert score["parameters_matched"] == 8
    assert score["parameters_total"] == 12


def test_reader_scores_blind_hole_depth_and_counterbore_as_parameters() -> None:
    reference = {
        "main_view": {
            "cross_holes": [{
                "diameter_mm": 10,
                "depth_mm": 8.5,
                "angle_deg": 0,
                "through": False,
                "counterbore_diameter_mm": 24,
                "counterbore_depth_mm": 3,
            }]
        }
    }
    prediction = {
        "main_view": {
            "cross_holes": [{
                "diameter_mm": 10,
                "depth_mm": 8.5,
                "angle_deg": 0,
                "through": True,
                "counterbore_diameter_mm": 24,
            }]
        }
    }

    score = score_parameters(prediction, reference)

    assert score["parameters_matched"] == 4
    assert score["parameters_total"] == 6
    assert score["parameter_details"]["cross_holes.counterbore_depth_mm"] == {
        "matched": 0,
        "expected": 1,
        "predicted_values": [],
        "expected_values": [3.0],
    }


def test_reader_scores_only_source_supported_axial_pattern_fields() -> None:
    reference = {"main_view": {"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 65,
        "from_face": "zmin",
        "through": False,
        "thread_depth_mm": 15,
        "drill_depth_mm": 17,
        "thread": {"nominal_diameter_mm": 8, "internal": True},
    }]}}
    prediction = {"main_view": {"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 65,
        "from_face": "zmin",
        "through": False,
        "thread_depth_mm": 15,
        "drill_depth_mm": 17,
        "pilot_diameter_mm": None,
        "thread": {"nominal_diameter_mm": 8, "internal": True},
    }]}}

    score = score_parameters(prediction, reference)

    assert score["parameters_matched"] == 8
    assert score["parameters_total"] == 8
    assert score["parameter_accuracy"] == 1.0


def test_detal_126_reference_has_corrected_independent_source_parameters() -> None:
    fixture = pathlib.Path(__file__).parents[1] / "fixtures" / "detal_126_reference_spec_v3.json"
    reference = json.loads(fixture.read_text(encoding="utf-8"))

    score = score_parameters(reference, reference)

    assert score["parameters_matched"] == 77
    assert score["parameters_total"] == 77


def test_reader_scores_taper_ratio_as_a_semantic_parameter() -> None:
    reference = {
        "main_view": {
            "bore": [{
                "diameter_mm": 56.55,
                "length_mm": 78,
                "taper": {"ratio": "7:24"},
            }]
        }
    }
    prediction = {
        "main_view": {
            "bore": [{
                "diameter_mm": 56.55,
                "length_mm": 78,
                "taper": {"ratio": "1:10"},
            }]
        }
    }

    score = score_parameters(prediction, reference)

    assert score["parameters_matched"] == 2
    assert score["parameters_total"] == 3
    assert score["parameter_details"]["bore.taper.ratio"]["matched"] == 0


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
