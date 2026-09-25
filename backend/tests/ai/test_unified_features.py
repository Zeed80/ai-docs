"""Единая модель элементов (дорожка У, шаг У1): спек любого типа → элементы с
3D-размещением в системе детали, без потерь."""

import math

import pytest

from app.ai.cad_recognize.features import (
    PROFILE_GROUPS,
    ROTATION_GROUPS,
    features_of,
)
from app.ai.verify_corpus.synth import synth_spec


def _items(body: dict, group: str) -> int:
    return len([item for item in body.get(group) or [] if isinstance(item, dict)])


@pytest.mark.parametrize("kind", ["shaft", "plate", "flange", "hub_flange", "housing", "weldment"])
def test_no_geometry_building_item_is_lost(kind):
    """Каждый элемент каждой группы спека становится ровно одним элементом
    единой модели, с путём назад в спек."""
    for seed in range(40):
        spec = synth_spec(kind, seed)
        features = features_of(spec)
        bodies = spec.get("parts") or [spec.get("main_view") or {}]
        expected = 0
        for body in bodies:
            expected += sum(_items(body, group) for group in ROTATION_GROUPS)
            profile = body.get("profile") or {}
            expected += sum(_items(profile, group) for group in PROFILE_GROUPS)
            for flange in body.get("flanges") or []:
                expected += sum(_items(flange.get("profile") or {}, g) for g in PROFILE_GROUPS)
        assert len(features) == expected, (kind, seed)
        assert all(feature.source_path for feature in features)


def _close(a, b, tolerance=1e-6):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def test_a_radial_hole_at_an_angle_sits_on_the_surface_pointing_to_the_axis():
    """Многоосевое тело вращения: отверстие Ø6 на 90° в ступени Ø40 — точка
    на поверхности (0; 20; z), ось инструмента — к оси детали."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 20.0},
                {"diameter_mm": 40.0, "length_mm": 50.0},
            ],
            "cross_holes": [{"diameter_mm": 6.0, "axial_position_mm": 45.0, "angle_deg": 90.0}],
        }
    }

    (hole,) = features_of(spec)

    assert hole.kind == "hole" and "radial" in hole.tags
    assert _close(hole.placement.origin, (0.0, 20.0, 45.0))
    assert _close(hole.placement.axis, (0.0, -1.0, 0.0))
    assert hole.params["through"] is True


@pytest.mark.parametrize(
    "plane, origin, axis",
    [
        ("top", (0.0, 0.0, 30.0), (0.0, 0.0, -1.0)),
        ("bottom", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ("front", (0.0, -25.0, 15.0), (0.0, 1.0, 0.0)),
        ("back", (0.0, 25.0, 15.0), (0.0, -1.0, 0.0)),
        ("left", (-40.0, 0.0, 15.0), (1.0, 0.0, 0.0)),
        ("right", (40.0, 0.0, 15.0), (-1.0, 0.0, 0.0)),
    ],
)
def test_a_pocket_in_the_middle_of_each_face_sits_on_that_face(plane, origin, axis):
    """Корпус 80 × 50 × 30: карман в центре грани — точка в центре грани, ось
    инструмента — внутрь (та же математика, что у ядра)."""
    spec = {
        "main_view": {
            "profile": {
                "shape": "rectangle",
                "width_mm": 80.0,
                "height_mm": 50.0,
                "thickness_mm": 30.0,
                "wall_features": [
                    {
                        "kind": "pocket",
                        "on_plane": plane,
                        "profile": "circle",
                        "diameter_mm": 8.0,
                        "depth_mm": 4.0,
                    }
                ],
            }
        }
    }

    (pocket,) = features_of(spec)

    assert _close(pocket.placement.origin, origin), pocket.placement.origin
    assert _close(pocket.placement.axis, axis)


def test_flange_holes_on_a_turned_body_are_axial_holes_on_the_flange_face():
    """Втулка с фланцем: отверстия фланца — параллельно оси, с его грани."""
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 15.0, "length_mm": 24.0}],
            "flanges": [
                {
                    "axial_start_mm": 4.0,
                    "thickness_mm": 2.0,
                    "profile": {
                        "shape": "circle",
                        "diameter_mm": 29.0,
                        "thickness_mm": 2.0,
                        "holes": [{"center_x_mm": 11.0, "center_y_mm": 0.0, "diameter_mm": 2.5}],
                    },
                }
            ],
        }
    }

    flange, hole = features_of(spec)

    assert flange.kind == "flange"
    assert hole.kind == "hole" and "axial" in hole.tags
    assert _close(hole.placement.origin, (11.0, 0.0, 4.0))
    assert math.isclose(hole.params["diameter_mm"], 2.5)
