"""Карманы и приливы на гранях призматической детали (X2, корпуса).

До этого элемент строился только от верхней грани (+Z), и корпус с приливом на
стенке (крышка part_05) выразить было нечем ни спеком, ни деревом операций.
"""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec


def _spec(*wall_features):
    return {
        "part": "Корпус",
        "main_view": {
            "type": "плита",
            "profile": {
                "shape": "rectangle",
                "width_mm": 60.0,
                "height_mm": 40.0,
                "thickness_mm": 30.0,
                "wall_features": list(wall_features),
            },
        },
    }


def _wall(**item):
    return {"kind": "boss", "profile": "circle", "diameter_mm": 12.0, "depth_mm": 8.0, **item}


def test_a_wall_feature_reaches_the_kernel_with_its_plane_and_corner_coordinates():
    spec = _spec(
        _wall(on_plane="front", center_u_mm=5.0, center_v_mm=-2.0),
        {
            "kind": "pocket",
            "on_plane": "left",
            "profile": "rectangle",
            "width_mm": 10.0,
            "height_mm": 20.0,
            "depth_mm": 5.0,
        },
    )

    features = feature_tree_from_spec(spec).features

    boss = next(f for f in features if f.kind == "boss")
    # Грань «перёд» — ширина × толщина: центр от угла грани.
    assert boss.params["on_plane"] == "front"
    assert (boss.params["center_x_mm"], boss.params["center_y_mm"]) == (35.0, 13.0)
    pocket = next(f for f in features if f.kind == "pocket")
    # Грань «слева» у ядра — толщина × высота: стороны меняются местами.
    assert (pocket.params["center_x_mm"], pocket.params["center_y_mm"]) == (15.0, 20.0)
    assert (pocket.params["width_mm"], pocket.params["height_mm"]) == (20.0, 10.0)
    assert all(p.origin == "stated" for p in boss.param_provenance.values())


@pytest.mark.parametrize(
    ("plane", "centre"),
    [
        ("top", (30.0, 20.0)),
        ("bottom", (30.0, 20.0)),
        ("front", (30.0, 15.0)),
        ("back", (15.0, 30.0)),
        ("left", (15.0, 20.0)),
        ("right", (20.0, 15.0)),
    ],
)
def test_the_centre_of_each_face_is_the_middle_of_that_face(plane, centre):
    features = feature_tree_from_spec(_spec(_wall(on_plane=plane))).features

    boss = next(f for f in features if f.kind == "boss")
    assert (boss.params["center_x_mm"], boss.params["center_y_mm"]) == centre


def test_the_plane_survives_the_round_trip_through_the_graph():
    """Дерево для ядра собирается из графа: потерянная плоскость — потерянный элемент."""
    from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph

    spec = _spec(_wall(on_plane="right"))
    graph = spec_feature_tree_as_graph(spec, feature_tree_from_spec(spec), graph_id="g")

    rebuilt = feature_tree_from_graph(graph, target_id="preview")

    boss = next(f for f in rebuilt.features if f.kind == "boss")
    assert boss.params["on_plane"] == "right"
    assert boss.params["diameter_mm"] == 12.0


def test_the_schema_refuses_a_half_described_feature():
    with pytest.raises(ValueError, match="circle requires diameter_mm"):
        EngineeringDrawingSpec.model_validate(
            _spec({"kind": "boss", "on_plane": "top", "profile": "circle", "depth_mm": 4.0})
        )
    with pytest.raises(ValueError, match="rectangle requires"):
        EngineeringDrawingSpec.model_validate(
            _spec({"kind": "pocket", "on_plane": "top", "profile": "rectangle", "depth_mm": 4.0})
        )
    # У круга и эскиза граней габарита нет — угадывать их нельзя.
    round_spec = _spec(_wall(on_plane="top"))
    round_spec["main_view"]["profile"] = {
        "shape": "circle",
        "diameter_mm": 50.0,
        "thickness_mm": 6.0,
        "wall_features": round_spec["main_view"]["profile"]["wall_features"],
    }
    with pytest.raises(ValueError, match="shape='rectangle'"):
        EngineeringDrawingSpec.model_validate(round_spec)


def test_the_consensus_votes_wall_features_like_every_other_cut():
    """D6: элементы контура, которые видел один проход из трёх, не строятся."""
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    agreed = _wall(on_plane="front")
    imagined = _wall(on_plane="back", diameter_mm=20.0)
    passes = [_spec(agreed), _spec(agreed), _spec(agreed, imagined)]

    merged = consensus_spec(passes)

    walls = merged["main_view"]["profile"]["wall_features"]
    assert [w["on_plane"] for w in walls] == ["front"]
