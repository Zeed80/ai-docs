"""Согласование элементов по размещению: угол — по замеру и угловой надписи."""

from __future__ import annotations

import math

from app.ai.cad_recognize.verifiers.reconcile import apply_reconciliation, reconcile


def _spec(dimensions: list[str]) -> dict:
    return {
        "dimensions": dimensions,
        "main_view": {
            "outer": [{"diameter_mm": 40.0, "length_mm": 80.0}],
            "placed_features": [
                {
                    "kind": "hole",
                    "origin_mm": [20.0, 0.0, 40.0],
                    "axis": [-1.0, 0.0, 0.0],
                    "ref": [0.0, 0.0, 1.0],
                    "diameter_mm": 6.0,
                    "through": True,
                }
            ],
        },
    }


REPORT = {
    "items": [
        {
            "kind": "placed_feature",
            "path": "main_view.placed_features[0]",
            "status": "refuted",
            "read": {"angle_deg": 0.0, "diameter_mm": 6.0},
            "measured": {"angle_deg": 59.8, "diameter_mm": 6.1},
            "tolerance_mm": {"diameter": 0.8, "angle": 6.0},
            "reason": "угол 59.8°, прочитано 0°",
        }
    ]
}


def test_the_measured_angle_is_adopted_when_the_sheet_carries_it():
    spec = _spec(["Ø6", "60°", "0.5×45°"])
    decisions = reconcile(spec, REPORT)

    assert [(d["field"], d["action"], d.get("value")) for d in decisions] == [
        ("angle_deg", "adopt", 60.0)
    ]
    spec2, report2 = apply_reconciliation(spec, REPORT, decisions)
    hole = spec2["main_view"]["placed_features"][0]
    assert abs(math.degrees(math.atan2(hole["origin_mm"][1], hole["origin_mm"][0])) - 60) < 1e-3
    assert abs(math.hypot(hole["origin_mm"][0], hole["origin_mm"][1]) - 20.0) < 1e-3
    assert hole["origin_mm"][2] == 40.0
    assert report2["items"][0]["status"] == "confirmed"


def test_a_linear_number_does_not_stand_for_an_angle():
    """Замер 59,8° и длина «60» на листе — не угловая надпись: человеку."""
    decisions = reconcile(_spec(["Ø6", "60", "0.5×45°"]), REPORT)

    assert [(d["field"], d["action"]) for d in decisions] == [("angle_deg", "ask_human")]


def test_stations_follow_the_profile_accepted_by_the_sheet():
    """Живой turned_multiaxis-0: ридер прочёл 3 ступени из 5 — станции от
    уступов встали мимо; принятый по листу контур их пересчитывает."""
    from app.ai.cad_recognize.verifiers.reconcile import restation_placed

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 28.0, "length_mm": 12.0},
                {"diameter_mm": 25.0, "length_mm": 70.0},
                {"diameter_mm": 40.0, "length_mm": 70.0},
            ],
            "placed_features": [
                {
                    "kind": "hole",
                    "origin_mm": [0.0, 14.0, 75.4],  # стояло по неверному контуру
                    "axis": [0.0, -1.0, 0.0],
                    "diameter_mm": 3.0,
                    "sheet_station": {"step_diameter_mm": 25.0, "from_shoulder_mm": 50.8},
                }
            ],
        }
    }
    hole = restation_placed(spec)["main_view"]["placed_features"][0]

    assert hole["origin_mm"][2] == 62.8
    assert abs(hole["origin_mm"][1] - 12.5) < 1e-6 and abs(hole["origin_mm"][0]) < 1e-6
