"""Размещение тел (X3): несколько тел листа ставятся туда, где их показывает лист.

Раньше они строились раздельно условным сдвигом с замечанием «взаимное
расположение не прочитано» — сварной узел или сборку так не выразить.
"""

from __future__ import annotations

from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec


def _shaft(diameter: float, length: float) -> dict:
    return {
        "type": "тело вращения",
        "outer": [
            {"diameter_mm": diameter, "length_mm": length / 2.0},
            {"diameter_mm": diameter - 4.0, "length_mm": length / 2.0},
        ],
    }


def _two_body_spec(placement: dict | None) -> dict:
    second = _shaft(20.0, 40.0)
    if placement is not None:
        second["placement"] = placement
    return {
        "part": "Узел",
        "main_view": {"type": "тело вращения", "outer": []},
        "parts": [_shaft(30.0, 60.0), second],
    }


def test_a_placed_body_carries_its_placement_to_the_kernel():
    placement = {"position_mm": [0.0, 0.0, 60.0], "axis": [0.0, 0.0, 1.0], "angle_deg": 0.0}
    spec = _two_body_spec(placement)
    EngineeringDrawingSpec.model_validate(spec)

    tree = feature_tree_from_spec(spec)

    revolves = [feature for feature in tree.features if feature.kind == "revolve"]
    assert len(revolves) == 2
    assert revolves[1].params["placement"]["position_mm"] == [0.0, 0.0, 60.0]
    assert revolves[1].param_provenance["placement"].origin == "stated"
    assert not any("не прочитано" in note for note in tree.missing_data)


def test_an_unplaced_body_is_built_apart_with_a_note():
    tree = feature_tree_from_spec(_two_body_spec(None))

    assert all("placement" not in feature.params for feature in tree.features)
    assert any("не прочитано" in note for note in tree.missing_data)


def test_the_consensus_keeps_an_agreed_placement_and_drops_a_disputed_one():
    """Молчаливая потеря той же семьи, что и фланцы: консенсус голосует только
    известные поля тела, и размещение выпало бы без следа."""
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    agreed = {"position_mm": [0.0, 0.0, 60.0]}
    passes = [_two_body_spec(agreed), _two_body_spec(agreed), _two_body_spec(agreed)]
    merged = consensus_spec(passes)
    assert merged["parts"][1]["placement"]["position_mm"] == [0.0, 0.0, 60.0]

    split = [
        _two_body_spec({"position_mm": [0.0, 0.0, 60.0]}),
        _two_body_spec({"position_mm": [0.0, 0.0, 10.0]}),
        _two_body_spec({"position_mm": [0.0, 0.0, 35.0]}),
    ]
    assert "placement" not in consensus_spec(split)["parts"][1]


def test_the_placement_survives_the_round_trip_through_the_graph():
    """Дерево для ядра собирается из графа: потерянное размещение — тело не там."""
    from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph

    spec = _two_body_spec({"position_mm": [0.0, 0.0, 60.0]})
    graph = spec_feature_tree_as_graph(spec, feature_tree_from_spec(spec), graph_id="g")

    rebuilt = feature_tree_from_graph(graph, target_id="preview")

    placed = [f for f in rebuilt.features if f.kind == "revolve" and "placement" in f.params]
    assert [f.params["placement"]["position_mm"] for f in placed] == [[0.0, 0.0, 60.0]]
