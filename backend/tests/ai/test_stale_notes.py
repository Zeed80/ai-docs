"""Замечания, чей предмет уже решён листом, не держат сборку (живые
turned_multiaxis-0/1), а нерешённое остаётся."""

from __future__ import annotations

from app.ai.cad_recognize.verifiers.reconcile import settle_stale_notes


def _spec(notes: list[str]) -> dict:
    return {
        "main_view": {
            "outer": [
                {"diameter_mm": 28.0, "length_mm": 12.0},
                {"diameter_mm": 25.0, "length_mm": 70.0},
            ],  # fmt: skip
            "bore": [],
            "placed_features": [
                {"kind": "hole", "diameter_mm": 3.0, "origin_mm": [0.0, 12.5, 62.8]},
                {"kind": "hole", "diameter_mm": 8.0, "origin_mm": [10.0, 17.3, 206.8]},
            ],
        },
        "provenance": {"main_view.placed_features[0]": {"origin": "sheet_measurement"}},
        "unresolved": notes,
    }


def _report(traces: list[float], confirmed: list[int]) -> dict:
    return {
        "section_traces_mm": traces,
        "items": [
            {
                "kind": "placed_feature",
                "path": f"main_view.placed_features[{i}]",
                "status": "confirmed",
            }
            for i in confirmed
        ],
    }


def test_notes_about_what_the_sheet_settled_are_dropped_and_the_rest_stays():
    notes = [
        "bore:0:length_mm",  # модель вписала код на расточку, которой нет
        "расточка: диаметры расточки не подтверждены локализованным внутренним контуром: Ø8",
        "малые элементы: поперечное отверстие Ø3 указано, но не локализовано",
        "малые элементы: поперечное отверстие Ø25 указано, но не локализовано",  # Ø ступени
        "малые элементы: поперечное отверстие Ø5 указано, но не локализовано",  # не поставлено
        "отверстие Ø3 на сечении Б-Б: числа не с листа или ступень не определяется однозначно",
        "outer:1:length_mm",  # ступень есть — код живой
    ]

    left = settle_stale_notes(_spec(notes))["unresolved"]

    assert left == [
        "малые элементы: поперечное отверстие Ø5 указано, но не локализовано",
        "outer:1:length_mm",
    ]


def test_a_section_note_goes_only_when_every_trace_holds_a_confirmed_feature():
    note = "лыска 23.7 на сечении Г-Г: числа не с листа или ступень не определяется однозначно"

    free_trace = settle_stale_notes(_spec([note]), _report([62.45, 173.48, 206.65], [0, 1]))
    all_taken = settle_stale_notes(_spec([note]), _report([62.45, 206.65], [0, 1]))

    assert free_trace["unresolved"] == [note]  # 173,5 — возможно, эта лыска
    assert all_taken["unresolved"] == []


def test_notes_about_the_old_chain_go_with_the_profile_taken_from_the_sheet():
    """Живой turned_multiaxis-0: после профиля 12·70·70·40·70 по листу
    оставались «ступени дают 180 мм…» и «цепочка не сходится…» прежнего."""
    from app.ai.cad_recognize.verifiers.reconcile import apply_profile

    spec = {
        "main_view": {"outer": [{"diameter_mm": 28.0, "length_mm": 70.0}]},
        "unresolved": [
            "профиль короче листа: ступени дают 180 мм, а наибольший линейный размер на чертеже — 262 мм",
            "размерная цепочка: цепочка не сходится с числом ступеней (3 диаметров, 4 осевых размеров)",
            "малые элементы: поперечное отверстие Ø5 указано, но не локализовано",
        ],
    }
    decision = {"value": [{"diameter_mm": 28.0, "length_mm": 12.0}], "reason": "по листу"}

    assert apply_profile(spec, decision)["unresolved"] == [
        "малые элементы: поперечное отверстие Ø5 указано, но не локализовано"
    ]


def test_a_flat_whose_size_is_only_a_diameter_on_the_sheet_is_not_a_flat():
    """Живой z4-r4: Ø21,7 и Ø15,7 — дно канавок на выносных элементах; ридер
    сечений выдал их «лысками», и пять таких замечаний держали сборку."""
    spec = _spec(
        [
            "лыска 21.7 на сечении Б-Б: числа не с листа или ступень не определяется однозначно",
            "лыска 15.7 на сечении В-В: числа не с листа или ступень не определяется однозначно",
            "лыска 23.7 на сечении Г-Г: числа не с листа или ступень не определяется однозначно",
        ]
    )
    spec["dimensions"] = ["Ø21,7", "φ21,7", {"value": "Ø15,7"}, "23.7", "Ø25"]

    assert settle_stale_notes(spec)["unresolved"] == [
        "лыска 23.7 на сечении Г-Г: числа не с листа или ступень не определяется однозначно"
    ]
