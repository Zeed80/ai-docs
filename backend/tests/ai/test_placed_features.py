"""Элемент по 3D-размещению (дорожка У): спек → дерево → граф → дерево для ядра.

Живой прогон через ядро — смоук ядра (лыска, радиальное под 45°, наклонное
отверстие, прорезь: объёмы по формуле).
"""

import math

import pytest

from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph
from app.ai.cad_recognize.features import features_of
from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec, assign_stable_feature_ids
from app.ai.cad_solid import feature_tree_from_spec

C30, S30 = math.cos(math.radians(30)), math.sin(math.radians(30))


def _shaft() -> dict:
    return {
        "schema_version": 1,
        "part": "вал",
        "main_view": {
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 150.0},
                {"diameter_mm": 60.0, "length_mm": 50.0},
            ],
            "placed_features": [
                {
                    "kind": "pocket",
                    "profile": "rectangle",
                    "origin_mm": [40.0, 0.0, 75.0],
                    "axis": [-1.0, 0.0, 0.0],
                    "ref": [0.0, 0.0, 1.0],
                    "width_mm": 30.0,
                    "height_mm": 82.0,
                    "depth_mm": 5.0,
                },
                {
                    "kind": "hole",
                    "origin_mm": [40.0 * C30, 40.0 * S30, 30.0],
                    "axis": [-C30, -S30, 0.0],
                    "diameter_mm": 10.0,
                    "through": True,
                },
            ],
        },
    }


def _plate() -> dict:
    return {
        "schema_version": 1,
        "part": "пластина",
        "main_view": {
            "profile": {
                "shape": "rectangle",
                "width_mm": 60.0,
                "height_mm": 40.0,
                "thickness_mm": 10.0,
            },
            "placed_features": [
                {
                    "kind": "hole",
                    "origin_mm": [10.0, 0.0, 0.0],
                    "axis": [0.0, 0.5, math.sqrt(0.75)],
                    "diameter_mm": 5.0,
                }
            ],
        },
    }


def _through_graph(spec: dict):
    spec = EngineeringDrawingSpec.model_validate(spec).model_dump(mode="json")
    assign_stable_feature_ids(spec)
    tree = feature_tree_from_spec(spec)
    graph = spec_feature_tree_as_graph(spec, tree, graph_id="g")
    return spec, feature_tree_from_graph(graph, target_id="preview")


def test_a_flat_and_an_angled_radial_hole_reach_the_kernel_through_the_graph():
    """Лыска и радиальное отверстие под 30° — то, что списки по типам не выражали."""
    _spec, rebuilt = _through_graph(_shaft())

    placed = [f for f in rebuilt.features if "placement" in f.params and f.kind != "revolve"]
    assert [f.kind for f in placed] == ["pocket", "hole"]
    flat, hole = placed
    assert flat.params["placement"]["origin"] == [40.0, 0.0, 75.0]
    assert flat.params["profile"] == "rectangle" and flat.params["depth_mm"] == 5.0
    assert hole.params["placement"]["axis"] == pytest.approx([-C30, -S30, 0.0])
    assert hole.params["through"] is True


def test_a_placed_feature_on_a_rectangular_plate_is_moved_to_the_kernel_corner():
    """Система детали — от центра контура, ядро строит пластину от угла."""
    _spec, rebuilt = _through_graph(_plate())

    hole = next(f for f in rebuilt.features if f.kind == "hole" and "placement" in f.params)
    assert hole.params["placement"]["origin"] == [40.0, 20.0, 0.0]


def test_placed_features_are_in_the_unified_feature_list():
    spec, _rebuilt = _through_graph(_shaft())

    placed = [f for f in features_of(spec) if "placed" in f.tags]
    assert [f.source_path for f in placed] == [
        "main_view.placed_features[0]",
        "main_view.placed_features[1]",
    ]


def test_the_schema_refuses_a_placed_feature_without_its_sizes():
    spec = _plate()
    spec["main_view"]["placed_features"] = [
        {
            "kind": "pocket",
            "profile": "rectangle",
            "origin_mm": [0, 0, 0],
            "axis": [0, 0, 1],
            "depth_mm": 2.0,
        }
    ]
    with pytest.raises(ValueError, match="requires width_mm and height_mm"):
        EngineeringDrawingSpec.model_validate(spec)
    spec["main_view"]["placed_features"] = [
        {"kind": "hole", "origin_mm": [0, 0, 0], "axis": [0, 0, 0], "diameter_mm": 2.0}
    ]
    with pytest.raises(ValueError, match="axis must not be zero"):
        EngineeringDrawingSpec.model_validate(spec)
