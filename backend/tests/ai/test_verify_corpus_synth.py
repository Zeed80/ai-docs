"""Генератор эталона обязан выдавать ИЗГОТОВИМЫЕ детали.

Эталон, который сам нарушает ГОСТ или кладёт паз верхом на уступ, учил бы
проверку принимать брак за норму. Проверено на деле: при сборке генератора
сверка пазов нашла на эталоне два паза, вылезших за свою ступень, — `uniform`
с перевёрнутыми границами не падает, а молча возвращает число между ними.
"""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.keyway_standard import ground_keyways, standard_section
from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec
from app.ai.verify_corpus.synth import synth_spec

SEEDS = range(400)


def test_the_same_seed_gives_the_same_part():
    """Иначе split dev/holdout плывёт между прогонами."""
    assert synth_spec("shaft", 11) == synth_spec("shaft", 11)
    assert synth_spec("shaft", 11) != synth_spec("shaft", 12)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_part_passes_the_schema_and_builds_a_tree(seed):
    spec = synth_spec("shaft", seed)
    EngineeringDrawingSpec.model_validate(spec)
    assert feature_tree_from_spec(spec) is not None


def test_no_keyway_of_the_reference_straddles_a_step_or_breaks_the_standard():
    remarks = []
    for seed in SEEDS:
        spec = synth_spec("shaft", seed)
        notes: list[str] = []
        summary = ground_keyways(spec["main_view"], notes)
        if summary["straddling"] or summary["flagged"]:
            remarks.append((seed, notes))
    assert remarks == []


def test_keyway_sections_follow_the_standard_for_their_step():
    for seed in SEEDS:
        body = synth_spec("shaft", seed)["main_view"]
        position = 0.0
        stations = []
        for section in body["outer"]:
            stations.append((position, position + section["length_mm"], section["diameter_mm"]))
            position += section["length_mm"]
        for keyway in body["keyways"]:
            middle = keyway["axial_start_mm"] + keyway["length_mm"] / 2
            diameter = next(d for lo, hi, d in stations if lo <= middle <= hi)
            assert (keyway["width_mm"], keyway["depth_mm"]) == standard_section(diameter)


def test_the_corpus_actually_contains_the_features_verifiers_need():
    """Пустой корпус проверял бы только то, что и так работает."""
    counts = {"keyways": 0, "grooves": 0, "cross_holes": 0, "chamfers": 0, "bore": 0}
    for seed in SEEDS:
        body = synth_spec("shaft", seed)["main_view"]
        for key in counts:
            counts[key] += len(body.get(key) or [])
    assert all(value > 50 for value in counts.values()), counts


def test_an_unwritten_part_type_says_so():
    with pytest.raises(ValueError, match="ещё не написан"):
        synth_spec("weldment", 0)
