"""A hole in the reading is a labelled assumption, not a refusal.

The rule this file defends: a value the sheet did not give may be supplied only
where there is a principle behind it, and it must arrive marked. A quietly
invented dimension is worse than no part at all; a visible one is where the
person editing starts.
"""

from __future__ import annotations

from app.ai.cad_recognize.spec_assumptions import apply_assumptions

_SHAFT = {
    "part": "Вал",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 80.0, "length_mm": 150.0},
            {"diameter_mm": 102.0, "length_mm": None},
            {"diameter_mm": 60.0, "length_mm": 120.0},
        ],
    },
    "dimensions": [{"value": "470"}, {"value": "150"}, {"value": "120"}],
}


def test_one_missing_length_is_arithmetic_not_a_guess():
    """470 overall minus 150 and 120 leaves 200. That is subtraction."""
    completed, assumptions = apply_assumptions(_SHAFT)

    assert completed["main_view"]["outer"][1]["length_mm"] == 200.0
    assert len(assumptions) == 1
    assert assumptions[0].origin == "derived"
    assert "остаток габарита" in assumptions[0].rule
    # The reading itself is untouched — the pair is what a reviewer compares.
    assert _SHAFT["main_view"]["outer"][1]["length_mm"] is None


def test_two_missing_lengths_remain_unresolved():
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 150.0},
                {"diameter_mm": 102.0, "length_mm": None},
                {"diameter_mm": 60.0, "length_mm": None},
            ]
        },
        "dimensions": [{"value": "450"}],
    }
    completed, assumptions = apply_assumptions(spec)

    lengths = [s["length_mm"] for s in completed["main_view"]["outer"]]
    assert lengths == [150.0, None, None]
    assert assumptions == []


def test_without_an_overall_missing_length_remains_unresolved():
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 100.0},
                {"diameter_mm": 60.0, "length_mm": None},
            ]
        },
        "dimensions": [{"value": "Ø80"}],
    }
    completed, assumptions = apply_assumptions(spec)

    assert completed["main_view"]["outer"][1]["length_mm"] is None
    assert assumptions == []


def test_a_chamfer_the_sheet_only_named_gets_a_standard_size():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 60.0, "length_mm": 100.0}],
            "chamfers": [{"location": "left_end"}],
        },
    }
    completed, assumptions = apply_assumptions(spec)

    chamfer = completed["main_view"]["chamfers"][0]
    assert chamfer["size_mm"] == 1.6  # ГОСТ 10948 for Ø60
    assert chamfer["angle_deg"] == 45.0
    assert "ГОСТ 10948" in assumptions[0].rule


def test_a_keyway_gets_its_width_and_depth_from_the_shaft_diameter():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 60.0, "length_mm": 200.0}],
            "keyways": [{"axial_start_mm": 20.0, "length_mm": 80.0}],
        },
    }
    completed, assumptions = apply_assumptions(spec)

    keyway = completed["main_view"]["keyways"][0]
    assert (keyway["width_mm"], keyway["depth_mm"]) == (18.0, 7.0)  # ГОСТ 23360, Ø58..65
    assert all("ГОСТ 23360" in item.rule for item in assumptions)


def test_a_thread_gets_its_coarse_pitch_but_only_from_the_series():
    spec = {
        "main_view": {
            "outer": [
                {
                    "diameter_mm": 24.0,
                    "length_mm": 60.0,
                    "thread": {"designation": "M24", "nominal_diameter_mm": 24.0},
                },
            ]
        },
    }
    completed, assumptions = apply_assumptions(spec)
    assert completed["main_view"]["outer"][0]["thread"]["pitch_mm"] == 3.0
    assert "ГОСТ 8724" in assumptions[0].rule

    # A nominal outside the standard series gets nothing: a wrong pitch is a
    # scrapped part, and there is no principle to pick one from.
    odd = {
        "main_view": {
            "outer": [
                {
                    "diameter_mm": 23.0,
                    "length_mm": 60.0,
                    "thread": {"designation": "M23", "nominal_diameter_mm": 23.0},
                },
            ]
        },
    }
    completed, assumptions = apply_assumptions(odd)
    assert completed["main_view"]["outer"][0]["thread"].get("pitch_mm") is None
    assert assumptions == []


def test_a_stated_value_is_never_overwritten():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 60.0, "length_mm": 100.0}],
            "chamfers": [{"location": "left_end", "size_mm": 0.5, "angle_deg": 30.0}],
            "keyways": [
                {"axial_start_mm": 10.0, "length_mm": 50.0, "width_mm": 10.0, "depth_mm": 4.0}
            ],
        },
    }
    completed, assumptions = apply_assumptions(spec)
    assert completed["main_view"]["chamfers"][0]["size_mm"] == 0.5
    assert completed["main_view"]["keyways"][0]["width_mm"] == 10.0
    assert assumptions == []


def test_every_assumption_is_recorded_where_the_value_lives():
    completed, _assumptions = apply_assumptions(_SHAFT)
    entry = completed["provenance"]["main_view.outer.1.length_mm"]
    assert entry["origin"] == "derived"
    assert entry["value_mm"] == 200.0
    assert "габарит" in entry["detail"]


def test_a_complete_reading_is_left_exactly_as_it_is():
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 150.0},
                {"diameter_mm": 60.0, "length_mm": 120.0},
            ]
        },
    }
    completed, assumptions = apply_assumptions(spec)
    assert assumptions == []
    assert "provenance" not in completed
