"""Негодная канавка не должна уносить с собой прочитанный вал.

Разбор живого прогона `z4-r4.jpg` (2026-09-07): три прохода полного чтения из
пяти были отброшены целиком, и в каждом случае виновником был один
вспомогательный элемент, а не деталь:

    cad_spec_rejected fields=['main_view.grooves.0']
        messages=['groove needs exactly one of depth_mm / root_diameter_mm']
    cad_spec_rejected fields=['main_view']
        messages=['keyway 0 runs past the end of the part']

Вместе с канавкой каждый раз пропадал корректно прочитанный ступенчатый
профиль. Выборка для консенсуса усохла с пяти чтений до двух, они разошлись на
одной ступени — и прогон закончился сообщением «выбран тип „тело вращения“, но
чтение не подтвердило осевой ступенчатый профиль».

Канавка — это аннотация на валу. Потерять её — замечание для ревью; потерять
вал — проваленная оцифровка.
"""

from __future__ import annotations

import copy

from app.ai.cad_recognize.spec_vectorize import validate_spec_lenient

_SHAFT = {
    "schema_version": 1,
    "part": "Вал",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 18, "length_mm": 15},
            {"diameter_mm": 25, "length_mm": 30},
            {"diameter_mm": 35, "length_mm": 50},
        ],
    },
}


def _shaft(**main_view) -> dict:
    spec = copy.deepcopy(_SHAFT)
    spec["main_view"].update(main_view)
    return spec


def test_groove_without_depth_does_not_take_the_shaft_with_it():
    """Первая из двух живых причин отказа."""
    spec, dropped = validate_spec_lenient(
        _shaft(grooves=[{"axial_position_mm": 20, "width_mm": 3}])
    )

    assert spec is not None
    assert len(spec["main_view"]["outer"]) == 3
    assert spec["main_view"].get("grooves") == []
    assert len(dropped) == 1
    assert "groove needs exactly one" in dropped[0]


def test_keyway_past_the_end_is_removed_not_fatal():
    """Вторая: валидатор уровня ТЕЛА, который называет индекс только в тексте.

    Pydantic сообщает про такую ошибку ``loc = ("main_view",)`` — по одному
    ``loc`` виновника не найти, номер элемента спрятан в сообщении.
    """
    spec, dropped = validate_spec_lenient(
        _shaft(keyways=[{"axial_start_mm": 10, "length_mm": 500, "width_mm": 8, "depth_mm": 4}])
    )

    assert spec is not None
    assert len(spec["main_view"]["outer"]) == 3
    assert spec["main_view"].get("keyways") == []
    assert "runs past the end" in dropped[0]


def test_every_removal_is_recorded_in_the_fail_closed_contract():
    """Снятое обязано быть видно — молча оно исчезать не может."""
    spec, dropped = validate_spec_lenient(
        _shaft(grooves=[{"axial_position_mm": 20, "width_mm": 3}])
    )

    assert any(item.startswith("dropped:") for item in spec["unresolved"])
    assert spec["geometry_validation_errors"] == dropped


def test_an_error_inside_the_geometry_is_still_fatal():
    """Удалением чинятся аннотации, а не сама деталь.

    Отрицательный диаметр ступени — не лишний элемент, который можно снять:
    это и есть то, ради чего читали лист. Такой спек по-прежнему уходит в
    observation-only, а не «чинится» выбрасыванием ступени.
    """
    broken = copy.deepcopy(_SHAFT)
    broken["main_view"]["outer"][0]["diameter_mm"] = -5

    spec, _dropped = validate_spec_lenient(broken)

    assert spec is None


def test_several_bad_features_are_removed_in_one_pass():
    spec, dropped = validate_spec_lenient(
        _shaft(
            grooves=[
                {"axial_position_mm": 20, "width_mm": 3},
                {"axial_position_mm": 40, "width_mm": 2, "depth_mm": 1},
            ],
            keyways=[{"axial_start_mm": 10, "length_mm": 500, "width_mm": 8, "depth_mm": 4}],
        )
    )

    assert spec is not None
    assert len(dropped) == 2
    # Годная канавка остаётся: снимается виновник, а не всё семейство.
    assert len(spec["main_view"]["grooves"]) == 1
    assert spec["main_view"]["grooves"][0]["axial_position_mm"] == 40


def test_a_valid_spec_is_untouched():
    spec, dropped = validate_spec_lenient(copy.deepcopy(_SHAFT))

    assert dropped == []
    assert spec["unresolved"] == []
    assert len(spec["main_view"]["outer"]) == 3


def test_removal_indices_do_not_shift_under_multiple_errors():
    """Удаление идёт с конца — иначе позиции остальных ошибок съезжают."""
    spec, dropped = validate_spec_lenient(
        _shaft(
            grooves=[
                {"axial_position_mm": 10, "width_mm": 3},  # негодная
                {"axial_position_mm": 20, "width_mm": 2, "depth_mm": 1},  # годная
                {"axial_position_mm": 30, "width_mm": 4},  # негодная
            ]
        )
    )

    assert spec is not None
    assert len(dropped) == 2
    remaining = spec["main_view"]["grooves"]
    assert [g["axial_position_mm"] for g in remaining] == [20]
