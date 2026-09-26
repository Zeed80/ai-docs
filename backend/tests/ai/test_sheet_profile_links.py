"""Профиль по листу: повтор звена, которое ридер выписал один раз."""

from __future__ import annotations

from app.ai.cad_recognize.verifiers.sheet_profile import _assign

# Ступени 12 · 70 · 70 · 40 · 70 = 262; уступы вида на 12, 82, 152, 192.
BOUNDS = [[12.1], [81.9], [152.1], [191.9]]


def test_a_repeated_length_written_once_explains_both_steps():
    """Живой turned_multiaxis-0: «70» у двух ступеней, выписано одно."""
    marks = [(262.0, None), (12.0, None), (70.0, None), (40.0, None)]

    found = _assign(BOUNDS, marks, 262.0, 2.0, strict=True)

    assert not isinstance(found, str), found
    assert [round(v, 1) for v in found[0]] == [12.0, 82.0, 152.0, 192.0]


def test_a_label_given_to_another_element_is_only_a_spare_link():
    """«70» отдана выдуманному пазу — свободной её нет, но звеном к соседу она годится."""
    marks = [(262.0, None), (12.0, None), (40.0, None)]

    found = _assign(BOUNDS, marks, 262.0, 2.0, strict=True, spare=[(70.0, None)])

    assert not isinstance(found, str), found
    assert [round(v, 1) for v in found[0]] == [12.0, 82.0, 152.0, 192.0]


def test_without_the_link_the_step_stays_unexplained():
    marks = [(262.0, None), (12.0, None), (40.0, None)]

    assert isinstance(_assign(BOUNDS, marks, 262.0, 2.0, strict=True), str)


def test_a_label_on_a_shelf_beyond_its_end_face_explains_the_end_link():
    """Живой turned_multiaxis-0: «12» — на полке в 39 мм левее торца; без неё
    уступ 12 объясняла и «14,8» отверстия (запасная) — отказ всего профиля."""
    bounds = [[10.5, 11.7, 12.9], [81.6, 81.7, 81.9], [151.8, 152.0, 152.3], [191.3, 191.6, 191.8]]
    marks = [
        (10.35, 202.1),
        (12.0, -39.5),
        (22.5, 242.5),
        (23.7, 632.6),
        (40.0, 195.0),
        (50.8, 56.1),
        (63.4, 106.0),
        (70.0, None),
        (262.0, 133.9),
    ]

    found = _assign(bounds, marks, 262.0, 5.24, strict=False, spare=[(70.0, 48.9), (14.8, 242.5)])

    assert not isinstance(found, str), found
    assert [round(v) for v in found[0]] == [12, 82, 152, 192]


def test_a_label_beyond_the_end_face_does_not_explain_an_inner_link():
    """Полка за торцом — только у звена от этого торца: «70» левее вала не
    объясняет уступ 152 = 82 + 70."""
    marks = [(262.0, None), (12.0, None), (70.0, -30.0), (40.0, None)]

    assert isinstance(_assign(BOUNDS, marks, 262.0, 2.0, strict=True), str)
