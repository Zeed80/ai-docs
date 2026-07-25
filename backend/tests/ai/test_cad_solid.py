"""Stage 1: an exact solid from the read spec (no neural reconstruction)."""

from __future__ import annotations

import math

import pytest

from app.ai.cad_solid import (
    estimate_mass_kg,
    feature_tree_from_spec,
    verify_solid_against_spec,
)


def _shaft_spec(**extra) -> dict:
    spec = {
        "part": "Втулка",
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ],
        },
    }
    spec["main_view"].update(extra.pop("main_view", {}))
    spec.update(extra)
    return spec


def test_profile_is_two_points_per_step_so_shoulders_stay_square():
    candidate = feature_tree_from_spec(_shaft_spec())
    assert candidate is not None
    points = candidate.features[0].params["profile_points"]
    # enter+leave for each of the two sections
    assert [(p["r"], p["z"]) for p in points] == [
        (15.0, 0.0), (15.0, 40.0), (25.0, 40.0), (25.0, 100.0),
    ]


def test_every_solid_parameter_declares_it_came_from_the_sheet():
    candidate = feature_tree_from_spec(_shaft_spec())
    assert candidate is not None
    provenance = candidate.features[0].param_provenance
    assert provenance["profile_points"].origin == "stated"


def test_bore_becomes_a_coaxial_cut_not_a_guess():
    spec = _shaft_spec(main_view={"bore": [{"diameter_mm": 16, "length_mm": 90}]})
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    params = candidate.features[0].params
    assert params["bore_points"][0]["r"] == pytest.approx(8.0)
    assert candidate.features[0].param_provenance["bore_points"].origin == "stated"
    # A solid part must SAY the cavity was never read, not silently omit it.
    assert candidate.missing_data == []


def test_solid_part_declares_the_unread_cavity():
    candidate = feature_tree_from_spec(_shaft_spec())
    assert candidate is not None
    assert any("разрез" in item for item in candidate.missing_data)


def test_bore_wider_than_the_body_is_refused_not_repaired():
    spec = _shaft_spec(main_view={"bore": [{"diameter_mm": 80, "length_mm": 50}]})
    assert feature_tree_from_spec(spec) is None


def test_incomplete_profile_builds_nothing():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50}],
        }
    }
    assert feature_tree_from_spec(spec) is None


def test_prismatic_spec_has_no_revolve_solid():
    spec = {
        "main_view": {
            "type": "призматическая",
            "profile": {"shape": "rectangle", "width_mm": 100, "height_mm": 60},
        }
    }
    assert feature_tree_from_spec(spec) is None


def test_multiple_bodies_are_declared_not_silently_dropped():
    spec = {
        "part": "Сборка",
        "parts": [
            {
                "type": "вал",
                "outer": [
                    {"diameter_mm": 30, "length_mm": 40},
                    {"diameter_mm": 50, "length_mm": 60},
                ],
            },
            {
                "type": "вал",
                "outer": [
                    {"diameter_mm": 20, "length_mm": 30},
                    {"diameter_mm": 40, "length_mm": 30},
                ],
            },
        ],
    }
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert any("тел: 2" in item for item in candidate.missing_data)


# --- verification against the source numbers --------------------------------


def _report(length: float, diameter: float, volume: float = 1000.0) -> dict:
    return {
        "bounds_mm": {"x": diameter, "y": diameter, "z": length},
        "brep_valid": True,
        "manifold": True,
        "solid_count": 1,
        "volume_mm3": volume,
    }


def test_verification_passes_when_the_solid_measures_the_stated_numbers():
    spec = _shaft_spec()
    result = verify_solid_against_spec(_report(100.0, 50.0), spec)
    assert result.ok
    assert result.checks["stated_length_mm"] == 100.0


def test_verification_catches_a_dropped_section():
    """The failure mode that matters: a section lost between read and build."""
    spec = _shaft_spec()
    result = verify_solid_against_spec(_report(60.0, 50.0), spec)
    assert not result.ok
    assert result.checks["length_ok"] is False


def test_verification_rejects_an_invalid_brep_even_when_sizes_match():
    spec = _shaft_spec()
    report = _report(100.0, 50.0)
    report["brep_valid"] = False
    assert not verify_solid_against_spec(report, spec).ok


def test_verification_window_is_half_a_percent():
    spec = _shaft_spec()
    assert verify_solid_against_spec(_report(100.4, 50.0), spec).checks["length_ok"]
    assert not verify_solid_against_spec(_report(101.0, 50.0), spec).checks["length_ok"]


# --- mass -------------------------------------------------------------------


def test_mass_uses_the_material_read_from_the_stamp():
    # Ø50×100 solid cylinder ≈ 196 350 mm³ of steel ≈ 1.54 kg
    volume = math.pi * 25.0**2 * 100.0
    assert estimate_mass_kg(volume, "Сталь 45 ГОСТ 1050-2013") == pytest.approx(1.541, abs=0.01)


def test_unknown_material_yields_no_mass_rather_than_a_steel_guess():
    assert estimate_mass_kg(1000.0, "Композит XYZ") is None
    assert estimate_mass_kg(1000.0, None) is None
    assert estimate_mass_kg(None, "Сталь 45") is None


# --- Stage 4: plates and flanges -------------------------------------------


def _plate_spec(**profile_extra) -> dict:
    profile = {
        "shape": "rectangle", "width_mm": 120, "height_mm": 60, "thickness_mm": 10,
    }
    profile.update(profile_extra)
    return {"part": "Планка", "main_view": {"type": "пластина", "profile": profile}}


def test_a_plate_becomes_an_extrusion_of_its_read_outline():
    candidate = feature_tree_from_spec(_plate_spec())
    assert candidate is not None
    base = candidate.features[0]
    assert base.kind == "extrude"
    assert base.params == {"width_mm": 120, "height_mm": 60, "depth_mm": 10}
    assert base.param_provenance["thickness_mm"].origin == "stated"


def test_a_plate_without_a_read_thickness_builds_nothing():
    """Thickness comes from a side view; guessing it would invent the part."""
    spec = _plate_spec()
    spec["main_view"]["profile"].pop("thickness_mm")
    assert feature_tree_from_spec(spec) is None


def test_hole_centres_move_from_sheet_frame_to_the_extrude_corner():
    spec = _plate_spec(holes=[{"center_x_mm": -45, "center_y_mm": 20, "diameter_mm": 10}])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    hole = next(f for f in candidate.features if f.kind == "hole")
    # The sheet measures from the centre, the box from its corner.
    assert hole.params["center_x_mm"] == pytest.approx(60 - 45)
    assert hole.params["center_y_mm"] == pytest.approx(30 + 20)


def test_a_flange_is_turned_and_keeps_the_sheet_frame():
    spec = {
        "part": "Фланец",
        "main_view": {"type": "фланец", "profile": {
            "shape": "circle", "diameter_mm": 140, "thickness_mm": 20,
            "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 40}],
        }},
    }
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert candidate.features[0].kind == "revolve"
    hole = next(f for f in candidate.features if f.kind == "hole")
    # A turned base is addressed from its axis — the drawing's own frame.
    assert hole.params["center_x_mm"] == 0
    assert hole.params["center_y_mm"] == 0


def test_a_bolt_circle_expands_into_real_holes():
    spec = {
        "part": "Фланец",
        "main_view": {"type": "фланец", "profile": {
            "shape": "circle", "diameter_mm": 140, "thickness_mm": 20,
            "hole_patterns": [{
                "kind": "bolt_circle", "count": 6, "bolt_circle_diameter_mm": 110,
                "hole_diameter_mm": 14, "start_angle_deg": 0,
            }],
        }},
    }
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [f for f in candidate.features if f.kind == "hole"]
    assert len(holes) == 6
    radii = {
        round(math.hypot(h.params["center_x_mm"], h.params["center_y_mm"]), 3)
        for h in holes
    }
    assert radii == {55.0}


def test_a_slot_is_built_as_a_true_capsule():
    spec = _plate_spec(slots=[{
        "center_x_mm": 0, "center_y_mm": 0, "length_mm": 40, "width_mm": 12,
        "rotation_deg": 0,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    pocket = next(f for f in candidate.features if f.kind == "pocket")
    assert pocket.params["width_mm"] == pytest.approx(28.0)  # 40 - 12
    ends = [f for f in candidate.features if f.kind == "hole"]
    assert len(ends) == 2
    assert {round(e.params["center_x_mm"], 3) for e in ends} == {46.0, 74.0}


def test_a_rotated_slot_is_declared_instead_of_cut_the_wrong_way():
    spec = _plate_spec(slots=[{
        "center_x_mm": 0, "center_y_mm": 0, "length_mm": 40, "width_mm": 12,
        "rotation_deg": 30,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert not [f for f in candidate.features if f.kind == "pocket"]
    assert any("повёрнут" in item for item in candidate.missing_data)


def test_plate_verification_checks_all_three_read_extents():
    spec = _plate_spec()
    report = {
        "bounds_mm": {"x": 120.0, "y": 60.0, "z": 10.0},
        "brep_valid": True, "manifold": True, "solid_count": 1,
    }
    assert verify_solid_against_spec(report, spec).ok
    report["bounds_mm"]["z"] = 12.0
    assert not verify_solid_against_spec(report, spec).ok
