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
