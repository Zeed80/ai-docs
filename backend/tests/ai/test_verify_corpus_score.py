"""Оценка прочитанного спека против эталона генератора (задача M5)."""

from __future__ import annotations

import copy

import pytest

from app.ai.verify_corpus.score import expand_holes, score_spec, summarize
from app.ai.verify_corpus.synth import synth_spec


@pytest.mark.parametrize("kind", ["shaft", "plate", "flange"])
def test_the_truth_itself_scores_perfectly(kind):
    spec = synth_spec(kind, 1)
    score = score_spec(spec, copy.deepcopy(spec))

    assert score["geometry"] and score["class_ok"]
    for value in score.values():
        if isinstance(value, dict) and "expected" in value:
            assert value["found"] == value["expected"] and value["extra"] == 0


def test_four_corner_holes_equal_a_two_by_two_pattern():
    """Ридер вправе назвать массив по углам любым из двух способов."""
    pattern = {
        "hole_patterns": [
            {
                "kind": "rectangular",
                "hole_diameter_mm": 9,
                "rows": 2,
                "columns": 2,
                "spacing_x_mm": 80,
                "spacing_y_mm": 40,
                "start_x_mm": -40,
                "start_y_mm": -20,
            }
        ]
    }
    holes = {
        "holes": [
            {"center_x_mm": x, "center_y_mm": y, "diameter_mm": 9}
            for x in (-40, 40)
            for y in (-20, 20)
        ]
    }
    assert sorted(expand_holes(pattern)) == sorted(expand_holes(holes))


def test_a_bolt_circle_read_at_zero_phase_misses_the_positions():
    """Фаза окружности болтов: продукт сейчас всегда читает 0°."""
    truth = synth_spec("flange", 0)
    read = copy.deepcopy(truth)
    pattern = truth["main_view"]["profile"]["hole_patterns"][0]
    pattern["start_angle_deg"] = 30.0
    read["main_view"]["profile"]["hole_patterns"][0]["start_angle_deg"] = 0.0

    score = score_spec(truth, read)

    count = pattern["count"]
    assert score["hole_diameters"]["found"] == score["hole_diameters"]["expected"]
    assert score["hole_positions"]["found"] == score["hole_positions"]["expected"] - count


def test_a_misread_step_breaks_the_exact_profile_but_not_the_rest():
    truth = synth_spec("shaft", 1)
    read = copy.deepcopy(truth)
    read["main_view"]["outer"][0]["length_mm"] += 5.0

    score = score_spec(truth, read)

    assert not score["profile_exact"]
    assert score["lengths"]["found"] == score["lengths"]["expected"] - 1
    assert score["diameters"]["found"] == score["diameters"]["expected"]
    assert not score["overall_ok"]


def test_nothing_read_is_no_geometry():
    score = score_spec(synth_spec("plate", 2), {})
    assert not score["geometry"] and not score["class_ok"]


def test_summary_counts_by_kind():
    truth = synth_spec("shaft", 1)
    rows = [
        {"sheet_kind": "shaft", "score": score_spec(truth, truth)},
        {"sheet_kind": "shaft", "score": score_spec(truth, {})},
    ]
    summary = summarize(rows)["shaft"]

    assert summary["sheets"] == 2
    assert summary["geometry"] == 0.5
    assert summary["diameters"]["recall"] == 0.5
