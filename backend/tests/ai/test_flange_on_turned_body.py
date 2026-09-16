"""Фланец на теле вращения: втулка part_03 с трёхушковым фланцем посередине оси."""

from __future__ import annotations

import math

import pytest

from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec


def _lug_sketch(pcd_r: float = 12.5, lug_r: float = 4.0):
    centres = [
        (pcd_r * math.cos(math.radians(90 + 120 * k)), pcd_r * math.sin(math.radians(90 + 120 * k)))
        for k in range(3)
    ]
    tangents = []
    for k in range(3):
        (x1, y1), (x2, y2) = centres[k], centres[(k + 1) % 3]
        length = math.hypot(x2 - x1, y2 - y1)
        nx, ny = (y2 - y1) / length, -(x2 - x1) / length
        if nx * (x1 + x2) + ny * (y1 + y2) < 0:
            nx, ny = -nx, -ny
        tangents.append(((x1 + lug_r * nx, y1 + lug_r * ny), (x2 + lug_r * nx, y2 + lug_r * ny)))
    ox, oy = tangents[-1][1]
    sketch = []
    for k, (start, end) in enumerate(tangents):
        cx, cy = centres[k]
        sketch.append(
            {
                "kind": "arc",
                "to": [start[0] - ox, start[1] - oy],
                "center": [cx - ox, cy - oy],
                "clockwise": False,
            }
        )
        sketch.append({"kind": "line", "to": [end[0] - ox, end[1] - oy]})
    sketch[-1]["to"] = [0.0, 0.0]
    return sketch, (ox, oy)


def _sleeve(flange_start: float = 10.0) -> dict:
    sketch, origin = _lug_sketch()
    return {
        "part_class": "rotation",
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": 15.0, "length_mm": 4.0},
                {"diameter_mm": 13.0, "length_mm": 14.0},
                {"diameter_mm": 15.0, "length_mm": 6.0},
            ],
            "bore": [{"diameter_mm": 11.0, "length_mm": 24.0}],
            "flanges": [
                {
                    "axial_start_mm": flange_start,
                    "thickness_mm": 2.0,
                    "sketch_origin_mm": origin,
                    "profile": {
                        "shape": "sketch",
                        "sketch": sketch,
                        "hole_patterns": [
                            {
                                "kind": "bolt_circle",
                                "count": 3,
                                "bolt_circle_diameter_mm": 25.0,
                                "hole_diameter_mm": 2.5,
                                "start_angle_deg": 90.0,
                            }
                        ],
                    },
                }
            ],
        },
    }


def test_a_sleeve_with_a_lug_flange_is_expressed_and_every_parameter_has_a_source():
    spec = EngineeringDrawingSpec.model_validate(_sleeve()).model_dump(mode="json")

    assert len(spec["main_view"]["flanges"]) == 1, "фланец потерян схемой"
    candidate = feature_tree_from_spec(spec)

    assert candidate is not None
    kinds = [feature.kind for feature in candidate.features]
    assert kinds.count("revolve") == 1 and kinds.count("boss") == 1 and kinds.count("pocket") == 3
    boss = next(f for f in candidate.features if f.kind == "boss")
    assert boss.params["axial_start_mm"] == 10.0 and boss.params["profile"] == "sketch"
    pockets = [f for f in candidate.features if f.kind == "pocket"]
    radii = sorted(
        round(math.hypot(p.params["center_x_mm"], p.params["center_y_mm"]), 3) for p in pockets
    )
    assert radii == [12.5, 12.5, 12.5]
    for feature in candidate.features:
        if feature.kind not in ("boss", "pocket"):
            continue
        assert not set(feature.params) - set(feature.param_provenance), feature.kind
        assert all(p.origin != "guessed" for p in feature.param_provenance.values())


def test_a_flange_past_the_end_of_the_part_is_a_misread():
    with pytest.raises(ValueError, match="flange 0 runs past the end"):
        EngineeringDrawingSpec.model_validate(_sleeve(flange_start=23.0))


def _kernel_report(volume: float) -> dict:
    """Отчёт живого ядра по этой втулке (33af4ce6): одно тело, всё построено."""
    kinds = ["revolve", "boss", "pocket", "pocket", "pocket"]
    return {
        "brep_valid": True,
        "manifold": True,
        "solid_count": 1,
        "volume_mm3": volume,
        "bounds_mm": {"x": 29.648148550617492, "y": 26.75, "z": 24.0},
        "feature_results": [
            {"kind": kind, "feature_index": i, "status": "built", "localization_ok": True}
            for i, kind in enumerate(kinds)
        ],
    }


def test_the_built_sleeve_with_a_flange_matches_the_spec():
    """Живая втулка part_03 (17d9ac5f): тело верное, но «размеры не совпали» —
    габарит поперёк оси сверялся только с Ø профиля, объём — без фланца."""
    from app.ai.cad_solid import verify_solid_against_spec

    spec = EngineeringDrawingSpec.model_validate(_sleeve()).model_dump(mode="json")
    candidate = feature_tree_from_spec(spec)

    good = verify_solid_against_spec(_kernel_report(2075.78), spec, candidate)
    assert good.checks["ok"], good.checks

    # Расточка не вырезана — объём больше, чем профиль + фланец: отказ остаётся.
    solid = verify_solid_against_spec(_kernel_report(2075.78 + 900.0), spec, candidate)
    assert not solid.checks["ok"]


def _gear(end: str = "left", outer: float = 38.0) -> dict:
    return {
        "part_class": "rotation",
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": 46.0, "length_mm": 6.0},
                {"diameter_mm": 14.0, "length_mm": 9.0},
            ],
            "bore": [{"diameter_mm": 7.0, "length_mm": 15.0}],
            "face_grooves": [
                {"end": end, "depth_mm": 2.0, "outer_diameter_mm": outer, "inner_diameter_mm": 16.0}
            ],
        },
    }


def test_a_face_groove_of_a_gear_wheel_is_a_ring_pocket_from_its_end():
    """Колесо part_06: выточка между Ø16 и Ø38 на 2 мм с левого торца."""
    spec = EngineeringDrawingSpec.model_validate(_gear()).model_dump(mode="json")
    assert len(spec["main_view"]["face_grooves"]) == 1, "выточка потеряна схемой"

    candidate = feature_tree_from_spec(spec)

    assert candidate is not None
    (pocket,) = [f for f in candidate.features if f.kind == "pocket"]
    assert pocket.params["inner_diameter_mm"] == 16.0 and pocket.params["diameter_mm"] == 38.0
    assert pocket.params["axial_start_mm"] == 0.0 and pocket.params["depth_mm"] == 2.0
    assert not set(pocket.params) - set(pocket.param_provenance)
    assert all(p.origin != "guessed" for p in pocket.param_provenance.values())

    right = feature_tree_from_spec(
        EngineeringDrawingSpec.model_validate(_gear(end="right")).model_dump(mode="json")
    )
    (pocket,) = [f for f in right.features if f.kind == "pocket"]
    assert pocket.params["axial_start_mm"] == 13.0


def test_a_face_groove_wider_than_the_part_is_a_misread():
    with pytest.raises(ValueError, match="face groove 0 is wider"):
        EngineeringDrawingSpec.model_validate(_gear(outer=50.0))
