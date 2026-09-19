"""Массив отверстий на окружности тела вращения — дополнение из листа (X1)."""

from app.ai.cad_recognize.verifiers.reconcile import complete_rotation_patterns


def _spec() -> dict:
    pattern = {
        "count": 6,
        "hole_diameter_mm": 11.0,
        "bolt_circle_diameter_mm": 90.0,
        "axis_mode": "axial",
    }
    return {
        "main_view": {
            "outer": [
                {"diameter_mm": 120.0, "length_mm": 15.0},
                {"diameter_mm": 60.0, "length_mm": 40.0},
            ],
            "bore": [{"diameter_mm": 28.0, "length_mm": 55.0}],
            # Живой прогон: массив выписан дважды.
            "circular_hole_patterns": [dict(pattern), dict(pattern)],
        },
        "unresolved": [
            "малые элементы: массив 6×Ø11: не определены торец/входная поверхность, угловая фаза массива, сквозное/глухое исполнение",
            "малые элементы: поперечное отверстие Ø11 указано, но не локализовано",
            "диаметр ступени 60 мм не подтвержден",
        ],
    }


def _report(status: str = "confirmed") -> dict:
    return {
        "items": [
            {
                "kind": "bolt_circle",
                "path": "main_view.circular_hole_patterns[0]",
                "status": status,
                "measured": {"count": 6, "start_angle_deg": 30.0},
            }
        ]
    }


def test_a_confirmed_pattern_in_the_disk_gets_face_through_and_phase_from_the_sheet():
    spec, notes = complete_rotation_patterns(_spec(), _report())
    (pattern,) = spec["main_view"]["circular_hole_patterns"]
    assert pattern["from_face"] == "zmin" and pattern["through"] is True
    assert pattern["start_angle_deg"] == 30.0
    # Устаревшие пометки о массиве и о «поперечном» Ø11 сняты, чужая — нет.
    assert spec["unresolved"] == ["диаметр ступени 60 мм не подтвержден"]


def test_an_unconfirmed_pattern_is_not_completed():
    spec, _notes = complete_rotation_patterns(_spec(), _report("unmeasurable"))
    (pattern,) = spec["main_view"]["circular_hole_patterns"]
    assert pattern.get("from_face") is None and pattern.get("start_angle_deg") is None
    assert len(spec["unresolved"]) == 3


def test_holes_crossing_a_step_boundary_get_no_through_or_face():
    raw = _spec()
    # Ступица Ø100: полоса отверстий 39,5…50,5 режет её частично — не «через диск».
    raw["main_view"]["outer"][1]["diameter_mm"] = 100.0
    spec, _notes = complete_rotation_patterns(raw, _report())
    (pattern,) = spec["main_view"]["circular_hole_patterns"]
    assert pattern.get("through") is None and pattern.get("from_face") is None
