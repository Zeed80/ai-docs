"""Согласование: когда замер по листу принимается сам, а когда — решение человеку."""

from __future__ import annotations

from app.ai.cad_recognize.verifiers.reconcile import (
    apply_reconciliation,
    reconcile,
    sheet_numbers,
)


def _spec(dimensions: list[str]) -> dict:
    return {
        "main_view": {
            "outer": [
                {"diameter_mm": 25.0, "length_mm": 12.0, "id": "s0"},
                {"diameter_mm": 40.0, "length_mm": 30.0, "id": "s1"},
                {"diameter_mm": 35.0, "length_mm": 98.0, "id": "s2"},
            ]
        },
        "dimensions": [{"value": text} for text in dimensions],
    }


def _report(items: list[dict]) -> dict:
    return {"items": items, "notes": []}


def _step(index: int, read: dict, measured: dict, status: str = "refuted") -> dict:
    return {
        "kind": "shaft_step",
        "path": f"main_view.outer[{index}]",
        "feature_id": f"s{index}",
        "read": read,
        "measured": measured,
        "status": status,
        "reason": "",
        "tolerance_mm": {"diameter": 0.3, "length": 0.5},
    }


def test_sheet_numbers_are_parsed_from_the_callouts_the_reader_wrote():
    numbers = sheet_numbers(
        _spec(["Ø80js6", "M24×1,5", "2 отв. Ø5.5", "1. Ø120 — наружный диаметр 999", "R5"])
    )

    assert numbers == [1.5, 5.0, 5.5, 24.0, 80.0, 120.0]


def test_a_measurement_matching_a_callout_is_adopted_when_the_read_value_is_not_on_the_sheet():
    """Живой shaft-1: длина 98 (на листе её нет) → замер 80,035 ≈ «80» на листе."""
    spec = _spec(["12", "30", "80", "Ø25", "Ø40", "Ø35"])
    report = _report(
        [
            _step(
                2,
                {"diameter_mm": 35.0, "length_mm": 98.0},
                {"diameter_mm": 35.0, "length_mm": 80.035},
            )
        ]
    )
    decisions = reconcile(spec, report)

    assert [(d["action"], d["field"], d["value"]) for d in decisions] == [
        ("adopt", "length_mm", 80.0)
    ]
    fixed, updated = apply_reconciliation(spec, report, decisions)
    assert fixed["main_view"]["outer"][2]["length_mm"] == 80.0
    assert spec["main_view"]["outer"][2]["length_mm"] == 98.0  # исходный спек не тронут
    assert fixed["provenance"]["main_view.outer[2].length_mm"]["origin"] == "sheet_measurement"
    assert fixed["provenance"]["main_view.outer[2].length_mm"]["read_mm"] == 98.0
    item = updated["items"][0]
    assert item["status"] == "confirmed" and item["reconciled"]["length_mm"]["read"] == 98.0
    assert item["read"]["length_mm"] == 80.0


def test_an_ambiguous_case_goes_to_a_person():
    """Прочитанное Ø40 тоже есть на листе (у соседней ступени), замер 27,9 без надписи."""
    spec = _spec(["12", "30", "Ø25", "Ø40", "Ø35"])
    report = _report(
        [
            _step(
                1,
                {"diameter_mm": 40.0, "length_mm": 30.0},
                {"diameter_mm": 27.917, "length_mm": 29.95},
            )
        ]
    )
    decisions = reconcile(spec, report)

    assert [d["action"] for d in decisions] == ["ask_human"]
    assert "тоже есть на листе" in decisions[0]["reason"]
    fixed, updated = apply_reconciliation(spec, report, decisions)
    assert fixed["main_view"]["outer"][1]["diameter_mm"] == 40.0
    assert updated["items"][0]["status"] == "refuted"


def test_positions_and_confirmed_items_are_never_adopted():
    spec = _spec(["12", "30", "80"])
    keyway = {
        "kind": "keyway",
        "path": "main_view.keyways[0]",
        "read": {"axial_start_mm": 16.0, "length_mm": 19.0, "width_mm": 8.0},
        "measured": {"axial_start_mm": 12.0, "length_mm": 19.0, "width_mm": 8.0},
        "status": "refuted",
        "tolerance_mm": {"length": 0.5, "width": 0.3},
    }
    confirmed = _step(
        0,
        {"diameter_mm": 25.0, "length_mm": 12.0},
        {"diameter_mm": 25.0, "length_mm": 12.0},
        "confirmed",
    )

    assert reconcile(spec, _report([keyway, confirmed])) == []


def test_a_keyway_with_width_and_depth_swapped_is_put_right():
    """Живой z4-r4: 4 × 8 прочитано, на листе и по ГОСТ 23360 для Ø30 — 8 × 4."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 35.0, "length_mm": 34.0},
                {"diameter_mm": 30.0, "length_mm": 30.0},
            ],
            "keyways": [
                {"axial_start_mm": 36.0, "length_mm": 22.0, "width_mm": 4.0, "depth_mm": 8.0}
            ],
        },
        "dimensions": [{"value": "22"}, {"value": "4"}, {"value": "8"}],
        "unresolved": [
            "шпоночный паз 0: ширина 4 мм при Ø30 и ничем не подтверждена, а ГОСТ 23360 даёт 8 мм — проверьте выноску",
            "PMI: 4 рамок с неразличимым знаком или значением",
        ],
    }
    report = {
        "items": [
            {
                "kind": "keyway",
                "path": "main_view.keyways[0]",
                "read": {"axial_start_mm": 36.0, "length_mm": 22.0, "width_mm": 4.0},
                "status": "refuted",
                "measured": {"axial_start_mm": 36.1, "length_mm": 22.1, "width_mm": 8.1},
                "reason": "ширина 8.1 мм, прочитано 4",
                "tolerance_mm": {"length": 0.5, "width": 0.3},
            }
        ],
        "summary": {"checked": 1, "confirmed": 0, "refuted": 1, "unmeasurable": 0},
    }

    decisions = reconcile(spec, report)

    assert {(d["field"], d["action"], d["value"]) for d in decisions} == {
        ("width_mm", "adopt", 8.0),
        ("depth_mm", "adopt", 4.0),
    }
    fixed, fixed_report = apply_reconciliation(spec, report, decisions)
    keyway = fixed["main_view"]["keyways"][0]
    assert (keyway["width_mm"], keyway["depth_mm"]) == (8.0, 4.0)
    # Замечание ГОСТ о прежней ширине устарело — снято; чужое осталось.
    assert fixed["unresolved"] == ["PMI: 4 рамок с неразличимым знаком или значением"]
    assert fixed_report["items"][0]["status"] == "confirmed"


def test_a_keyway_width_the_standard_does_not_back_is_not_swapped():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 30.0, "length_mm": 64.0}],
            "keyways": [
                {"axial_start_mm": 36.0, "length_mm": 22.0, "width_mm": 5.0, "depth_mm": 8.0}
            ],
        },
        "dimensions": [{"value": "5"}, {"value": "8"}],
    }
    report = {
        "items": [
            {
                "kind": "keyway",
                "path": "main_view.keyways[0]",
                "read": {"axial_start_mm": 36.0, "length_mm": 22.0, "width_mm": 5.0},
                "status": "refuted",
                "measured": {"axial_start_mm": 36.0, "length_mm": 22.0, "width_mm": 8.1},
                "reason": "",
                "tolerance_mm": {"length": 0.5, "width": 0.3},
            }
        ]
    }

    decisions = reconcile(spec, report)

    # Глубина 5 при ГОСТ 4 — это не перестановка: решает человек.
    assert [(d["field"], d["action"]) for d in decisions] == [("width_mm", "ask_human")]


def _keyway_case(dimensions):
    """z4-r4: паз прочитан с 63, на листе 69,3…91,5; лист точен до 1,7 мм.

    Система координат: 0,0638 мм/px от x = 287, главный вид y 442…1006.
    """
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 35.0, "length_mm": 65.0},
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 25.0, "length_mm": 90.0},
            ],
            "keyways": [
                {"axial_start_mm": 63.0, "length_mm": 22.0, "width_mm": 8.0, "depth_mm": 4.0}
            ],
        },
        "dimensions": dimensions,
        "unresolved": [],
    }
    report = {
        "frame": {
            "origin_px": [287.0, 724.0],
            "mm_per_px": 0.0638,
            "bbox_px": [284, 442, 3192, 1006],
        },
        "profile_adoption": {"station_error_mm": 1.677},
        "items": [
            {
                "kind": "keyway",
                "path": "main_view.keyways[0]",
                "read": {"axial_start_mm": 63.0, "length_mm": 22.0, "width_mm": 8.0},
                "status": "refuted",
                "measured": {"axial_start_mm": 69.278, "length_mm": 22.243, "width_mm": 8.137},
                "reason": "начало 69.278 мм, прочитано 63",
                "tolerance_mm": {"length": 0.5, "width": 0.3},
            }
        ],
    }
    return spec, report


def _at(value, x_mm, y):
    x = 287.0 + x_mm / 0.0638
    return {"value": value, "bbox": [x - 30, y - 18, x + 30, y + 18]}


def test_a_keyway_position_comes_from_the_label_off_its_shoulder():
    spec, report = _keyway_case(
        [
            _at("22", 81.0, 364),  # длина паза — своя надпись
            _at("2", 97.2, 364),  # от конца паза до уступа 95
            _at("3,5", 35.7, 1380),  # сечение Б-Б — ниже полосы вида
            _at("3", 108.0, 1798),  # выносной вид канавки
            _at("185", 100.4, 245),
        ]
    )

    decisions = reconcile(spec, report)

    assert [(d["field"], d["action"], d["value"]) for d in decisions] == [
        ("axial_start_mm", "adopt", 71.0)
    ], decisions
    fixed, fixed_report = apply_reconciliation(spec, report, decisions)
    assert fixed["main_view"]["keyways"][0]["axial_start_mm"] == 71.0
    # Паз теперь целиком в ступени Ø30 — замечания «выходит за ступень» нет.
    assert not [n for n in fixed["unresolved"] if "выходит за ступень" in n]
    assert fixed_report["items"][0]["status"] == "confirmed"


def test_two_labels_placing_the_keyway_differently_leave_it_to_a_person():
    spec, report = _keyway_case(
        [
            _at("2", 97.2, 364),  # конец 93 → начало 71
            _at("3", 70.0, 364),  # от уступа 65 → начало 68
        ]
    )

    assert reconcile(spec, report) == []


def _unread_keyway_case(dimensions):
    """z4-r4: второй паз на Ø22 (133…185) ридер не выписал; замер 149,7…175."""
    spec, report = _keyway_case(dimensions)
    spec["main_view"]["outer"] = [
        {"id": "0:outer:0", "diameter_mm": 30.0, "length_mm": 133.0},
        {"id": "0:outer:1", "diameter_mm": 22.0, "length_mm": 52.0},
    ]
    spec["main_view"]["keyways"] = []
    report["items"] = []
    report["keyway_proposals"] = [
        {
            "step_index": 1,
            "axial_start_mm": 149.653,
            "length_mm": 25.342,
            "width_mm": 6.127,
            "standard_mm": [6.0, 3.5],
            "evidence_bbox_px": [2626.0, 670.0, 3024.0, 766.0],
        }
    ]
    return spec, report


def test_a_keyway_found_on_the_sheet_is_added_by_its_labels():
    from app.ai.cad_recognize.verifiers.reconcile import apply_keyway_additions, keyway_additions

    spec, report = _unread_keyway_case(
        [
            _at("25", 162.0, 412),  # длина — над пазом
            _at("10", 180.0, 412),  # от конца паза до торца 185
            _at("185", 100.4, 245),
        ]
    )

    additions = keyway_additions(spec, report)

    assert [
        (a["axial_start_mm"], a["length_mm"], a["width_mm"], a["depth_mm"]) for a in additions
    ] == [(150.0, 25.0, 6.0, 3.5)], additions
    fixed = apply_keyway_additions(spec, additions)
    keyway = fixed["main_view"]["keyways"][0]
    assert keyway["on_section_id"] == "0:outer:1"
    assert keyway["evidence"][0]["bbox"] == [2626.0, 670.0, 3024.0, 766.0]
    assert (
        fixed["provenance"]["main_view.keyways[0].axial_start_mm"]["origin"] == "sheet_measurement"
    )
    assert spec["main_view"]["keyways"] == []  # исходный спек не тронут


def test_a_keyway_found_without_a_position_label_is_not_added():
    from app.ai.cad_recognize.verifiers.reconcile import keyway_additions

    spec, report = _unread_keyway_case([_at("25", 162.0, 412), _at("185", 100.4, 245)])

    assert keyway_additions(spec, report) == []
