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


def test_adjacent_steps_never_share_a_diameter():
    """Две соседние Ø12 — это один цилиндр, а не две ступени.

    Лист справедливо не проставил «длину ступени», которой у тела нет, а
    метрика полноты сочла это пропуском размера. Эталон обязан описывать
    деталь, которая действительно такая.
    """
    for seed in SEEDS:
        outer = synth_spec("shaft", seed)["main_view"]["outer"]
        for left, right in zip(outer, outer[1:], strict=False):
            assert left["diameter_mm"] != right["diameter_mm"], seed


def test_no_cross_hole_is_drilled_into_a_keyway():
    """shaft-6 корпуса: Ø6 на 94,9 в пазу 83..96 — контур паза на листе ломался."""
    for seed in range(400):
        body = synth_spec("shaft", seed)["main_view"]
        for hole in body.get("cross_holes") or []:
            reach = hole["diameter_mm"] / 2.0 + 1.0 - 1e-6
            for keyway in body.get("keyways") or []:
                start, end = (
                    keyway["axial_start_mm"],
                    keyway["axial_start_mm"] + keyway["length_mm"],
                )
                assert not (start - reach < hole["axial_position_mm"] < end + reach), (
                    seed,
                    hole,
                    keyway,
                )


def test_completeness_requires_keyway_width_and_depth_on_the_section():
    """Лист без b и t1 паза (X1b) — неполный: по нему паз не изготовить."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "build_verify_corpus.py"
    module_spec = importlib.util.spec_from_file_location("build_verify_corpus", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    spec = next(
        s for s in (synth_spec("shaft", seed) for seed in SEEDS) if s["main_view"].get("keyways")
    )
    keyway = spec["main_view"]["keyways"][0]
    needed = module.needed_dimensions(spec)
    lengths = list(needed["lengths"])
    for value in (keyway["width_mm"], keyway["depth_mm"]):
        assert any(abs(v - value) <= 1e-6 for v in lengths)
        lengths.remove(next(v for v in lengths if abs(v - value) <= 1e-6))

    labels = [
        {"kind": "dimension", "dimension_kind": "linear", "value_mm": v}
        for v in needed["lengths"] + needed["overall"]
    ] + [
        {"kind": "dimension", "dimension_kind": "diameter", "value_mm": v}
        for v in needed["diameters"]
    ]
    assert module.coverage(spec, {"labels": labels})["ratio"] == 1.0
    without_depth = list(labels)
    without_depth.remove(
        next(item for item in without_depth if abs(item["value_mm"] - keyway["depth_mm"]) <= 1e-6)
    )
    assert module.coverage(spec, {"labels": without_depth})["ratio"] < 1.0


# ── Корпуса (G2) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(200))
def test_every_housing_passes_the_schema_and_builds_a_tree(seed):
    spec = synth_spec("housing", seed)
    EngineeringDrawingSpec.model_validate(spec)
    assert feature_tree_from_spec(spec) is not None


@pytest.mark.parametrize("seed", range(200))
def test_no_housing_feature_hangs_off_its_own_face(seed):
    """Эталон, который сам ставит прилив за краем стенки, учил бы проверку
    принимать брак: у корпуса это ещё и отказ ядра."""
    profile = synth_spec("housing", seed)["main_view"]["profile"]
    width, height = profile["width_mm"], profile["height_mm"]
    thickness = profile["thickness_mm"]
    faces = {
        "top": (width, height),
        "bottom": (width, height),
        "front": (width, thickness),
        "back": (width, thickness),
        "left": (height, thickness),
        "right": (height, thickness),
    }
    for item in profile["wall_features"]:
        face_u, face_v = faces[item["on_plane"]]
        if item["profile"] == "circle":
            reach_u = reach_v = item["diameter_mm"] / 2
        else:
            reach_u, reach_v = item["width_mm"] / 2, item["height_mm"] / 2
        assert abs(item["center_u_mm"]) + reach_u <= face_u / 2 + 1e-6, item
        assert abs(item["center_v_mm"]) + reach_v <= face_v / 2 + 1e-6, item
        if item["kind"] == "pocket" and item["on_plane"] != "top":
            # Карман в стенке не прорезает её насквозь.
            assert item["depth_mm"] < thickness
    for pattern in profile["hole_patterns"]:
        radius = pattern["hole_diameter_mm"] / 2
        assert abs(pattern["start_x_mm"]) + radius <= width / 2
        assert abs(pattern["start_y_mm"]) + radius <= height / 2


def test_a_housing_has_a_cavity_and_at_least_one_wall_feature():
    """Иначе это не корпус, а пластина, и Ф5 мерить нечем."""
    for seed in range(20):
        walls = synth_spec("housing", seed)["main_view"]["profile"]["wall_features"]
        cavity = [item for item in walls if item["on_plane"] == "top"]
        assert len(cavity) == 1 and cavity[0]["kind"] == "pocket"
        assert [item for item in walls if item["on_plane"] != "top"]


# ── Листовые детали (X4) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(200))
def test_every_sheet_metal_part_is_bendable_and_builds_a_tree(seed):
    """Полка не короче трёх толщин и R + s, радиус гиба не меньше толщины."""
    spec = synth_spec("sheet_metal", seed)
    EngineeringDrawingSpec.model_validate(spec)
    sheet = spec["main_view"]["sheet_metal"]
    assert sheet["radius_mm"] >= sheet["thickness_mm"]
    shortest = max(3 * sheet["thickness_mm"], sheet["radius_mm"] + sheet["thickness_mm"])
    assert min(sheet["flanges_mm"]) >= shortest
    assert feature_tree_from_spec(spec) is not None


def test_a_hat_profile_has_webs_of_one_height():
    hats = [
        synth_spec("sheet_metal", seed)["main_view"]["sheet_metal"]
        for seed in range(60)
        if synth_spec("sheet_metal", seed)["main_view"]["name"] == "шляпный профиль"
    ]
    assert hats
    assert all(hat["flanges_mm"][1] == hat["flanges_mm"][3] for hat in hats)


# ── Сварные узлы (X3) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(200))
def test_every_weldment_builds_every_plate_and_every_bead(seed):
    """Живое ядро (20 seed сквозь граф): объём = пластины + валики по формуле."""
    spec = synth_spec("weldment", seed)
    EngineeringDrawingSpec.model_validate(spec)
    tree = feature_tree_from_spec(spec)
    bodies = len(spec["parts"])
    beads = [feature for feature in tree.features if feature.body_index >= bodies]
    assert len(beads) == sum(2 if weld["both_sides"] else 1 for weld in spec["welds"])
    assert tree.missing_data == []


@pytest.mark.parametrize("seed", range(200))
def test_a_weld_leg_is_never_thicker_than_the_plates_it_joins(seed):
    spec = synth_spec("weldment", seed)
    for weld in spec["welds"]:
        first, second = (spec["parts"][index]["profile"] for index in weld["bodies"])
        assert weld["leg_mm"] <= min(first["thickness_mm"], second["thickness_mm"])
