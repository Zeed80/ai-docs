"""Смысл надписей чертежа — на надписях с реальных листов."""

from __future__ import annotations

import pytest

from app.ai.cad_views.labels import parse_label


@pytest.mark.parametrize(
    ("text", "kind", "value", "surface"),
    [
        ("Ø8,5H10(+0,058)", "diameter", 8.5, "hole"),  # «Опора пружин»: отверстие
        ("Ø6 120°", "diameter", 6.0, "hole"),  # выноска радиального отверстия
        ("Ø4 гл.11.9", "diameter", 4.0, "hole"),  # глухое отверстие
        ("Ø5,7H10(+0,048)", "diameter", 5.7, "hole"),
        ("Ø9,8H10(+0,048)", "diameter", 9.8, "hole"),
        ("Ø1,5C11(+0,12 / +0,06)", "diameter", 1.5, "hole"),
        ("φ25", "diameter", 25.0, None),
        ("Ø30k6", "diameter", 30.0, "shaft"),
        ("50h7", "diameter", 50.0, "shaft"),  # вал без Ø — по квалитету
        ("Ø11,5", "diameter", 11.5, None),
        ("29", "linear", 29.0, None),
        ("21,2 -0,1", "linear", 21.2, None),
        ("6,7+0,1", "linear", 6.7, None),
        ("470 h14", "linear", 470.0, "shaft"),  # грубый квалитет — линейный
        ("R0,3*", "radius", 0.3, None),
        ("18°", "angle", 18.0, None),
        ("ПТС 170.10.03.008", "designation", None, None),  # не габарит 170,1
        ("Ra3,2", "roughness", 3.2, None),
    ],
)
def test_label_meaning(text, kind, value, surface):
    label = parse_label(text)

    assert label.kind == kind, label
    assert label.value == value, label
    assert label.surface == surface, label


def test_thread_chamfer_depth_and_count():
    thread = parse_label("M10x0,5-6g")
    assert (thread.kind, thread.value, thread.pitch) == ("thread", 10.0, 0.5)
    assert parse_label("M18×1,5-6g").pitch == 1.5

    chamfer = parse_label("0,5x45°")
    assert (chamfer.kind, chamfer.value, chamfer.angle) == ("chamfer", 0.5, 45.0)

    blind = parse_label("Ø3 гл.8.3")
    assert (blind.kind, blind.value, blind.depth) == ("diameter", 3.0, 8.3)

    holes = parse_label("4 отв. Ø9")
    assert (holes.kind, holes.value, holes.count) == ("diameter", 9.0, 4)


def test_tolerance_of_a_hole_fit():
    assert parse_label("Ø8,5H10(+0,058)").tolerance == (0.058, 0.0)
    assert parse_label("21,2 -0,1").tolerance == (0.0, -0.1)


def test_main_view_falls_back_to_the_view_the_model_named_main():
    from app.ai.cad_views.sheet_reading import parse_reading

    boxes = [(0, 0, 10, 10), (20, 0, 200, 300), (220, 0, 260, 40)]
    answer = {
        "sheet_kind": "detail",
        "main": 3,  # подпись, не изображение
        "regions": [
            {"n": 1, "role": "section", "name": "А-А"},
            {"n": 2, "role": "view", "name": "главный вид"},
            {"n": 3, "role": "label", "of": 1},
        ],
    }
    assert parse_reading(answer, boxes).main == 2

    answer = {
        "sheet_kind": "detail",
        "regions": [{"n": 1, "role": "view"}, {"n": 2, "role": "section"}],
    }
    assert parse_reading(answer, boxes).main == 2  # наибольшее изображение
