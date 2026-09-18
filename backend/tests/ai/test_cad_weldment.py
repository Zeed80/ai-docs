"""Сварной узел (X3): несколько пластин по местам и валики угловых швов.

Живое ядро (2026-09-18): основание 100×60×8 + ребро 100×40×6 на нём,
двусторонний шов △5 — объём 74 500 = 48 000 + 24 000 + 2 · 12,5 · 100.
"""

from __future__ import annotations

import pytest

from app.ai.cad_solid import feature_tree_from_spec


def _plate(width: float, height: float, thickness: float) -> dict:
    return {
        "type": "пластина",
        "profile": {
            "shape": "rectangle",
            "width_mm": width,
            "height_mm": height,
            "thickness_mm": thickness,
            "holes": [],
            "hole_patterns": [],
            "slots": [],
        },
    }


def _bracket(weld: dict | None = None) -> dict:
    rib = _plate(100.0, 40.0, 6.0)
    # Ребро поставлено на основание: поворот вокруг x на 90°, затем сдвиг.
    rib["placement"] = {"position_mm": [0.0, 33.0, 8.0], "axis": [1.0, 0.0, 0.0], "angle_deg": 90.0}
    spec = {"part": "Кронштейн", "main_view": {"type": "узел"}, "parts": [_plate(100, 60, 8), rib]}
    if weld is not None:
        spec["welds"] = [weld]
    return spec


def test_every_plate_of_a_welded_part_is_built_not_only_the_first():
    """Строилось только первое тело: ребро пропадало молча, без замечания."""
    tree = feature_tree_from_spec(_bracket())

    bases = [feature for feature in tree.features if feature.kind == "extrude"]
    assert [feature.body_index for feature in bases] == [0, 1]
    assert bases[1].params["placement"]["angle_deg"] == 90.0
    assert bases[1].param_provenance["placement"].origin == "stated"


def test_a_fillet_weld_runs_along_the_joint_on_both_sides():
    spec = _bracket({"bodies": [0, 1], "designation": "Т3", "leg_mm": 5.0, "both_sides": True})
    tree = feature_tree_from_spec(spec)

    beads = [feature for feature in tree.features if feature.body_index >= 2]
    assert len(beads) == 2
    for bead in beads:
        assert bead.params["depth_mm"] == pytest.approx(100.0)
        corner = bead.params["placement"]["position_mm"]
        # Валик лежит на верхней грани основания, у стенок ребра (y 27 и 33).
        assert corner[2] == pytest.approx(8.0)
        assert min(abs(corner[1] - 27.0), abs(corner[1] - 33.0)) < 1e-6
    area = 5.0 * 5.0 / 2.0
    assert sum(area * bead.params["depth_mm"] for bead in beads) == pytest.approx(2500.0)


def test_a_weld_between_bodies_that_do_not_touch_is_not_invented():
    spec = _bracket({"bodies": [0, 1], "designation": "Т1", "leg_mm": 5.0})
    spec["parts"][1]["placement"]["position_mm"] = [0.0, 33.0, 20.0]

    tree = feature_tree_from_spec(spec)

    assert all(feature.body_index < 2 for feature in tree.features)
    assert any("не касаются" in note for note in tree.missing_data)


def test_the_bead_orientation_is_a_proper_rotation():
    """Поворот валика задан осью-углом; обратный перевод обязан дать ту же
    матрицу — иначе валик встал бы зеркально, внутрь ребра."""
    from app.ai.cad_solid import _axis_angle, _rotation_of

    for columns in (
        [[-1, 0, 0], [0, 0, 1], [0, 1, 0]],
        [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
        [[0, -1, 0], [1, 0, 0], [0, 0, 1]],
    ):
        matrix = [[float(columns[c][r]) for c in range(3)] for r in range(3)]
        axis, angle = _axis_angle(matrix)
        rebuilt = _rotation_of({"axis": axis, "angle_deg": angle})
        flat = [value for row in rebuilt for value in row]
        assert flat == pytest.approx([value for row in matrix for value in row], abs=1e-9)


def test_the_consensus_votes_welds_and_drops_a_single_pass_one():
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    weld = {"bodies": [0, 1], "designation": "Т3", "leg_mm": 5.0, "both_sides": True}
    stray = {"bodies": [0, 1], "designation": "Н1", "leg_mm": 4.0, "both_sides": False}
    passes = [_bracket(weld), _bracket(weld), _bracket(stray)]

    merged = consensus_spec(passes)

    assert merged["welds"] == [weld]


def test_the_beads_survive_the_round_trip_through_the_graph():
    from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph

    spec = _bracket({"bodies": [0, 1], "designation": "Т3", "leg_mm": 5.0, "both_sides": True})
    graph = spec_feature_tree_as_graph(spec, feature_tree_from_spec(spec), graph_id="g")
    rebuilt = feature_tree_from_graph(graph, target_id="preview")

    beads = [feature for feature in rebuilt.features if feature.body_index >= 2]
    assert len(beads) == 2
    assert all("placement" in bead.params for bead in beads)


def test_every_body_keeps_its_own_index_through_the_graph():
    """Ядро получает дерево только через граф, а граф номер тела не хранил:
    ребро узла и валики швов приходили в ядро частью основания."""
    from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph

    spec = _bracket({"bodies": [0, 1], "designation": "Т3", "leg_mm": 5.0, "both_sides": True})
    tree = feature_tree_from_spec(spec)
    graph = spec_feature_tree_as_graph(spec, tree, graph_id="g")
    rebuilt = feature_tree_from_graph(graph, target_id="preview")

    assert sorted(f.body_index for f in rebuilt.features) == sorted(
        f.body_index for f in tree.features
    )
    assert all("body_index" not in f.params for f in rebuilt.features)
    assert not any(
        f.param_provenance.get(name) and f.param_provenance[name].origin == "guessed"
        for f in rebuilt.features
        for name in f.params
    )


def _taken(*numbers: float):
    return lambda value: value if any(abs(value - n) < 1e-6 for n in numbers) else None


def test_the_reader_places_each_rib_where_the_sheet_dimensions_it():
    """Ответ модели по перечню и виду слева → тела с размещением и швы."""
    from app.ai.cad_recognize.spec_fragments import weldment_from_answer

    answer = {
        "plates": [
            {
                "position": 2,
                "role": "rib",
                "width_mm": 80,
                "height_mm": 30,
                "thickness_mm": 5,
                "offset_mm": 65,
            },
            {
                "position": 1,
                "role": "base",
                "width_mm": 80,
                "height_mm": 120,
                "thickness_mm": 8,
                "offset_mm": None,
            },
        ],
        "welds": [{"between": [1, 2], "type": "T3", "leg_mm": 4}],
    }
    built = weldment_from_answer(answer, _taken(80, 30, 5, 65, 120, 8, 4))

    base, rib = built["parts"]
    assert base["profile"]["height_mm"] == 120.0 and "placement" not in base
    # Ближняя стенка ребра на 65 от кромки: ребро y 65…70 стоит на z = 8.
    assert rib["placement"]["position_mm"] == [0.0, 70.0, 8.0]
    assert built["welds"] == [
        {
            "bodies": [0, 1],
            "designation": "Т3",
            "standard": "ГОСТ 5264-80",
            "leg_mm": 4.0,
            "both_sides": True,
        }
    ]
    # Из прочитанного строится узел с валиками по обе стороны ребра.
    tree = feature_tree_from_spec({"part": "Опора", "main_view": {"type": "узел"}, **built})
    assert sum(1 for f in tree.features if f.body_index >= 2) == 2


def test_the_reader_builds_no_rib_from_a_position_the_sheet_does_not_state():
    from app.ai.cad_recognize.spec_fragments import weldment_from_answer

    notes: list[str] = []
    answer = {
        "plates": [
            {"position": 1, "role": "base", "width_mm": 80, "height_mm": 120, "thickness_mm": 8},
            {
                "position": 2,
                "role": "rib",
                "width_mm": 80,
                "height_mm": 30,
                "thickness_mm": 5,
                "offset_mm": 64,
            },
        ],
        "welds": [],
    }
    assert weldment_from_answer(answer, _taken(80, 30, 5, 65, 120, 8), notes) is None
    assert "положение ребра не проставлено" in notes[0]
