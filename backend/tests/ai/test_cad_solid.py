"""Stage 1: an exact solid from the read spec (no neural reconstruction)."""

from __future__ import annotations

import math

import pytest

from app.ai.cad_solid import (
    estimate_mass_kg,
    feature_tree_from_spec,
    solid_build_gate,
    solid_preview_gate,
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


def test_read_thread_is_carried_to_the_kernel_feature_tree():
    spec = _shaft_spec(main_view={
        "outer": [
            {"diameter_mm": 30, "length_mm": 40},
            {
                "diameter_mm": 50,
                "length_mm": 60,
                "thread": {
                    "designation": "M50x1,5",
                    "nominal_diameter_mm": 50,
                    "pitch_mm": 1.5,
                },
            },
        ],
    })
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    thread = next(feature for feature in candidate.features if feature.kind == "thread")
    assert thread.params["spec"] == "M50x1,5"
    assert thread.params["axial_start_mm"] == 40
    assert thread.params["pitch_mm"] == 1.5


def test_incomplete_axial_thread_pattern_is_visible_but_not_built():
    spec = _shaft_spec(main_view={"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 40,
        "start_angle_deg": 90,
        "spacing_deg": 180,
        "from_face": None,
        "through": None,
        "pilot_diameter_mm": None,
        "thread": {"designation": "M8", "nominal_diameter_mm": 8, "internal": True},
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert not any(feature.kind == "hole" for feature in candidate.features)
    assert any("осевой шаблон" in item and "не построен" in item for item in candidate.missing_data)
    assert solid_build_gate(spec, candidate)["allowed"] is False


def test_complete_axial_thread_pattern_expands_to_exact_holes_and_threads():
    spec = _shaft_spec(main_view={"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 40,
        "start_angle_deg": 90,
        "spacing_deg": 180,
        "from_face": "zmax",
        "through": False,
        "depth_mm": 12,
        "pilot_diameter_mm": 6.8,
        "thread": {
            "designation": "M8",
            "nominal_diameter_mm": 8,
            "pitch_mm": 1.25,
            "internal": True,
        },
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [feature for feature in candidate.features if feature.kind == "hole"]
    threads = [feature for feature in candidate.features if feature.kind == "thread"]
    assert len(holes) == len(threads) == 2
    assert [(hole.params["center_x_mm"], hole.params["center_y_mm"]) for hole in holes] == [
        (0.0, 20.0), (-0.0, -20.0)
    ]
    assert all(hole.params["diameter_mm"] == 6.8 for hole in holes)
    assert all(hole.params["depth_mm"] == 12 for hole in holes)
    assert all(thread.params["spec"] == "M8" for thread in threads)


def test_metric_thread_uses_finished_standard_minor_diameter_not_tap_drill():
    spec = _shaft_spec(main_view={"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 40,
        "from_face": "zmin",
        "through": False,
        "thread_depth_mm": 15,
        "drill_depth_mm": 17,
        "pilot_diameter_mm": None,
        "thread": {
            "designation": "M8",
            "nominal_diameter_mm": 8,
            "pitch_mm": None,
            "internal": True,
        },
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [feature for feature in candidate.features if feature.kind == "hole"]
    threads = [feature for feature in candidate.features if feature.kind == "thread"]

    assert len(holes) == len(threads) == 2
    assert all(hole.params["diameter_mm"] == pytest.approx(6.646835) for hole in holes)
    assert all(hole.params["depth_mm"] == 17 for hole in holes)
    assert all(thread.params["pitch_mm"] == 1.25 for thread in threads)
    assert all(thread.params["length_mm"] == 15 for thread in threads)
    assert holes[0].param_provenance["diameter_mm"].origin == "standard"
    assert threads[0].param_provenance["pitch_mm"].origin == "standard"


def test_recessed_axial_pattern_preserves_entry_plane_for_hole_and_thread():
    spec = _shaft_spec(main_view={"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 40,
        "from_face": "zmin",
        "entry_offset_mm": 6,
        "entry_recess_diameter_mm": 12,
        "through": False,
        "thread_depth_mm": 15,
        "drill_depth_mm": 17,
        "thread": {"designation": "M8", "nominal_diameter_mm": 8},
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [feature for feature in candidate.features if feature.kind == "hole"]
    threads = [feature for feature in candidate.features if feature.kind == "thread"]
    assert len(holes) == 4
    assert len(threads) == 2
    recesses = [hole for hole in holes if hole.params.get("role") == "entry_recess"]
    pilots = [hole for hole in holes if hole.params.get("role") != "entry_recess"]
    assert len(recesses) == len(pilots) == 2
    assert all(recess.params["diameter_mm"] == 12 for recess in recesses)
    assert all(feature.params["entry_offset_mm"] == 6 for feature in [*pilots, *threads])
    assert pilots[0].param_provenance["entry_offset_mm"].origin == "measured"


def test_recessed_axial_pattern_without_recess_diameter_is_not_built():
    spec = _shaft_spec(main_view={"axial_holes": [{
        "count": 2,
        "bolt_circle_diameter_mm": 40,
        "from_face": "zmin",
        "entry_offset_mm": 5.6,
        "through": False,
        "thread_depth_mm": 15,
        "drill_depth_mm": 17,
        "thread": {"designation": "M8", "nominal_diameter_mm": 8},
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert not any(feature.kind in {"hole", "thread"} for feature in candidate.features)
    assert any("Ø входной выборки" in item for item in candidate.missing_data)


def test_complete_circular_patterns_expand_to_axial_and_inclined_holes():
    spec = _shaft_spec(main_view={"circular_hole_patterns": [
        {
            "count": 4,
            "hole_diameter_mm": 4,
            "bolt_circle_diameter_mm": 30,
            "axis_mode": "axial",
            "start_angle_deg": 0,
            "from_face": "zmin",
            "through": False,
            "depth_mm": 20,
        },
        {
            "count": 2,
            "hole_diameter_mm": 1,
            "bolt_circle_diameter_mm": 20,
            "axis_mode": "inclined",
            "start_angle_deg": 90,
            "from_face": "zmax",
            "through": True,
            "inclination_deg": 45,
            "radial_direction": "outward",
        },
    ]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [feature for feature in candidate.features if feature.kind == "hole"]
    assert len(holes) == 6
    assert [hole.params["axis"] for hole in holes] == ["z"] * 4 + ["inclined"] * 2
    assert holes[0].params["depth_mm"] == 20
    assert holes[-1].params["inclination_deg"] == 45
    assert not any("массив" in item for item in candidate.missing_data)


def test_incomplete_circular_pattern_is_a_visible_build_blocker():
    spec = _shaft_spec(main_view={"circular_hole_patterns": [{
        "count": 12,
        "hole_diameter_mm": 4,
        "bolt_circle_diameter_mm": 70,
        "axis_mode": "axial",
        "start_angle_deg": None,
        "from_face": None,
        "through": False,
        "depth_mm": 82,
    }]})

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert not any(feature.kind == "hole" for feature in candidate.features)
    assert any("массив 12×Ø4" in item for item in candidate.missing_data)
    assert solid_build_gate(spec, candidate)["allowed"] is False


def test_missing_tap_drill_alone_is_a_warning_not_a_geometry_blocker():
    spec = _shaft_spec(
        main_view={"axial_holes": [{
            "count": 2,
            "bolt_circle_diameter_mm": 40,
            "from_face": "zmin",
            "through": False,
            "thread_depth_mm": 15,
            "drill_depth_mm": 17,
            "thread": {"designation": "M8", "nominal_diameter_mm": 8},
        }]},
        unresolved=[
            "осевые отверстия M8: не определены Ø подготовительного отверстия"
        ],
    )
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    gate = solid_build_gate(spec, candidate)

    assert gate["allowed"] is True
    assert "технологический параметр" in gate["warnings"][0]


def test_bore_becomes_a_coaxial_cut_not_a_guess():
    spec = _shaft_spec(main_view={"bore": [{"diameter_mm": 16, "length_mm": 90}]})
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    params = candidate.features[0].params
    assert params["bore_points"][0]["r"] == pytest.approx(8.0)
    assert candidate.features[0].param_provenance["bore_points"].origin == "stated"
    # A solid part must SAY the cavity was never read, not silently omit it.
    assert candidate.missing_data == []


def test_offset_bore_is_shifted_from_the_stated_end_face():
    spec = _shaft_spec(main_view={
        "bore": [{"diameter_mm": 16, "length_mm": 30}],
        "bore_start_mm": 10,
        "bore_from_end": "right",
        "bore_blind": True,
    })
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    points = candidate.features[0].params["bore_points"]
    assert points[0]["z"] == pytest.approx(60.0)
    assert points[-1]["z"] == pytest.approx(90.0)


def test_solid_part_declares_the_unread_cavity():
    candidate = feature_tree_from_spec(_shaft_spec())
    assert candidate is not None
    assert any("разрез" in item for item in candidate.missing_data)


def _two_body_spec() -> dict:
    """A sheet reading two independent rotation bodies (Ф2.1) — parts[]
    takes over from main_view once it has qualifying (>=2 outer sections)
    entries, per _rotation_parts' own contract."""
    return {
        "part": "Вал и втулка",
        "parts": [
            {"outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ]},
            {"outer": [
                {"diameter_mm": 10, "length_mm": 20},
                {"diameter_mm": 16, "length_mm": 25},
            ]},
        ],
    }


def test_multi_body_spec_builds_one_revolve_feature_per_body():
    candidate = feature_tree_from_spec(_two_body_spec())
    assert candidate is not None
    revolves = [f for f in candidate.features if f.kind == "revolve"]
    assert len(revolves) == 2
    # _rotation_parts numbers parts starting at 1 (0 is reserved for
    # main_view, which isn't used once parts[] has qualifying bodies).
    assert {f.body_index for f in revolves} == {1, 2}


def test_multi_body_spec_scopes_every_feature_to_its_own_body():
    candidate = feature_tree_from_spec(_two_body_spec())
    assert candidate is not None
    for feature in candidate.features:
        assert feature.body_index in (1, 2)


def test_multi_body_spec_warns_positions_are_unread_not_guessed():
    candidate = feature_tree_from_spec(_two_body_spec())
    assert candidate is not None
    assert any(
        "2" in item and "тел" in item and "не прочитано" in item
        for item in candidate.missing_data
    )


def test_single_qualifying_part_keeps_body_index_zero_by_default():
    # A lone part still goes through _rotation_parts' offset+1 numbering —
    # confirms the default Feature3D.body_index=0 path is for main_view-only
    # specs, not silently reused for a single explicit part.
    spec = {"part": "Вал", "parts": [{"outer": [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 50, "length_mm": 60},
    ]}]}
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    assert {f.body_index for f in candidate.features} == {1}
    assert not any("тел" in item and "не прочитано" in item for item in candidate.missing_data)


def test_main_view_only_spec_still_defaults_body_index_to_zero():
    candidate = feature_tree_from_spec(_shaft_spec())
    assert candidate is not None
    assert all(f.body_index == 0 for f in candidate.features)


def test_incomplete_second_body_builds_with_a_flagged_guess_not_a_refusal():
    """A2: a missing step length no longer discards the whole candidate — the
    affected body's revolve compiles with a provisional average length,
    flagged ParamProvenance.origin="guessed", while the OTHER (complete)
    body's revolve stays "stated" — the assumption is scoped to the body
    that actually needs one."""
    spec = _two_body_spec()
    spec["parts"][1]["outer"][1]["length_mm"] = None
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    revolves = {f.body_index: f for f in candidate.features if f.kind == "revolve"}
    assert revolves[1].param_provenance["profile_points"].origin == "stated"
    assert revolves[2].param_provenance["profile_points"].origin == "guessed"
    # Only known length (20) in that body's outer[] — averages to itself.
    assert revolves[2].params["profile_points"][-1]["z"] == 20.0 + 20.0


def test_unread_bore_blocks_3d_when_the_reader_found_a_section():
    spec = _shaft_spec(views=[{"kind": "section"}])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    gate = solid_build_gate(spec, candidate)
    assert gate["allowed"] is False
    assert any("разрез" in item for item in gate["blockers"])


def test_unread_bore_is_only_a_warning_without_section_evidence():
    spec = _shaft_spec()
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    gate = solid_build_gate(spec, candidate)
    assert gate["allowed"] is True
    assert any("разрез" in item for item in gate["warnings"])


def test_reader_unresolved_always_blocks_the_3d_boundary():
    spec = _shaft_spec(unresolved=["размерная цепочка не сходится"])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    gate = solid_build_gate(spec, candidate)
    assert gate["allowed"] is False
    assert gate["blockers"] == ["размерная цепочка не сходится"]


def test_review_preview_allows_only_explicitly_omitted_cut_features():
    spec = _shaft_spec(
        main_view={"chamfers": []},
        unresolved=["указано 6 фасок, локализовано 0"],
    )
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    preview = solid_preview_gate(solid_build_gate(spec, candidate))

    assert preview["allowed"] is True
    assert preview["hard_blockers"] == []
    assert preview["excluded"] == ["указано 6 фасок, локализовано 0"]


def test_review_preview_accepts_live_small_feature_blocker_wording():
    spec = _shaft_spec(unresolved=[
        "малые элементы: массив 8×Ø1: не определены торец/входная поверхность, угловая фаза массива",
        "малые элементы: осевые отверстия M8: не определён Ø входной выборки",
        "малые элементы: указано 6 фасок, локализовано 0",
    ])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    preview = solid_preview_gate(solid_build_gate(spec, candidate))

    assert preview["allowed"] is True
    assert preview["hard_blockers"] == []
    assert len(preview["excluded"]) == 3


def test_review_preview_accepts_unplaced_callout_wording_from_a_real_run():
    """A3: exact live wording from a real shaft run (2026-08-12) that used to
    hard-block the whole part — a callout the reader found but could not
    place has no cross_holes[]/thread entry to build wrong in the first
    place, so it is excludable, same as the other "малые элементы" wordings
    above."""
    spec = _shaft_spec(unresolved=[
        "малые элементы: поперечное отверстие Ø0.6 указано, но не локализовано",
        "малые элементы: резьбы указаны, но не привязаны к участкам: "
        "M18×1,5: несущий участок не локализован",
    ])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    preview = solid_preview_gate(solid_build_gate(spec, candidate))

    assert preview["allowed"] is True
    assert preview["hard_blockers"] == []
    assert len(preview["excluded"]) == 2


def test_review_preview_still_refuses_an_evidence_reliability_note():
    """Doubt about the READ ITSELF (geometry vs annotations not separable by
    colour) is not a scoped placement gap — stays a hard blocker."""
    spec = _shaft_spec(unresolved=[
        "малые элементы: evidence: геометрия не отделена от аннотаций по цвету",
    ])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    preview = solid_preview_gate(solid_build_gate(spec, candidate))

    assert preview["allowed"] is False
    assert preview["hard_blockers"] == [
        "малые элементы: evidence: геометрия не отделена от аннотаций по цвету",
    ]


def test_review_preview_still_refuses_a_dimension_chain_failure():
    spec = _shaft_spec(unresolved=["размерная цепочка не сходится"])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None

    preview = solid_preview_gate(solid_build_gate(spec, candidate))

    assert preview["allowed"] is False
    assert preview["hard_blockers"] == ["размерная цепочка не сходится"]


def test_raster_redraw_requires_localized_geometry_evidence_before_kernel():
    spec = _shaft_spec()
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    gate = solid_build_gate(spec, candidate, require_source_evidence=True)
    assert gate["allowed"] is False
    assert any("без локализованного evidence" in item for item in gate["blockers"])


def test_bore_wider_than_the_body_is_refused_not_repaired():
    spec = _shaft_spec(main_view={"bore": [{"diameter_mm": 80, "length_mm": 50}]})
    assert feature_tree_from_spec(spec) is None


def test_incomplete_profile_builds_a_flagged_guess_not_nothing():
    """A2: was test_incomplete_profile_builds_nothing — a stated diameter
    with no length used to refuse the whole part; it now compiles with a
    provisional length (here: the one other known length, 40, since there
    is nothing else to average) and an explicit "guessed" flag."""
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50}],
        }
    }
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    revolve = next(f for f in candidate.features if f.kind == "revolve")
    assert revolve.param_provenance["profile_points"].origin == "guessed"
    assert revolve.params["profile_points"][-1]["z"] == 40.0 + 40.0


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


def test_verification_rejects_multiple_or_non_manifold_solids():
    spec = _shaft_spec()
    report = _report(100.0, 50.0, volume=1000.0)
    report["solid_count"] = 2
    assert not verify_solid_against_spec(report, spec).ok
    report["solid_count"] = 1
    report["manifold"] = False
    assert not verify_solid_against_spec(report, spec).ok


def test_hollow_spec_rejects_solid_outer_profile_volume():
    spec = _shaft_spec(main_view={
        "bore": [{"diameter_mm": 20, "length_mm": 100}],
    })
    solid_outer_volume = math.pi * 15**2 * 40 + math.pi * 25**2 * 60
    report = _report(100.0, 50.0, volume=solid_outer_volume)
    result = verify_solid_against_spec(report, spec)
    assert not result.ok
    assert result.checks["volume_not_above_profile"] is False


def test_verification_fails_when_kernel_rolled_back_a_requested_feature():
    spec = _shaft_spec(
        main_view={
            "cross_holes": [
                {"diameter_mm": 9, "axial_position_mm": 50, "through": True}
            ]
        }
    )
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    report = _report(100.0, 50.0)
    report["warnings"] = [
        "cross hole Ø9 @ 50 not built: OpenCascade returned invalid geometry"
    ]
    result = verify_solid_against_spec(report, spec, candidate)
    assert not result.ok
    assert result.checks["feature_complete"] is False
    assert result.checks["requested_features"] == ["revolve", "hole"]


def test_verification_rejects_a_built_feature_without_brep_localization():
    spec = _shaft_spec()
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    report = _report(100.0, 50.0)
    report["feature_results"] = [
        {
            "feature_index": 0,
            "kind": "revolve",
            "status": "built",
            "requested_params": candidate.features[0].params,
        }
    ]

    result = verify_solid_against_spec(report, spec, candidate)

    assert not result.ok
    assert result.checks["feature_complete"] is False
    assert result.checks["unlocalized_features"] == [
        "revolve[0]: изменение B-Rep не локализовано"
    ]


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


def test_a_rounded_plate_keeps_its_read_corner_radius_in_the_kernel_payload():
    candidate = feature_tree_from_spec(_plate_spec(corner_radius_mm=8))

    assert candidate is not None
    base = candidate.features[0]
    assert base.params["corner_radius_mm"] == 8
    assert base.param_provenance["corner_radius_mm"].origin == "stated"


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


def _sketch_plate_spec(sketch, **profile_extra) -> dict:
    profile = {"shape": "sketch", "sketch": sketch, "thickness_mm": 10}
    profile.update(profile_extra)
    return {"part": "Пластина произвольного контура", "main_view": {"profile": profile}}


# A closed square: (0,0)->(40,0)->(40,30)->(0,30)->(0,0) — the implicit first
# vertex is (0,0), so only the 4 "to" points after it need listing.
_SKETCH_SQUARE = [
    {"kind": "line", "to": [40.0, 0.0]},
    {"kind": "line", "to": [40.0, 30.0]},
    {"kind": "line", "to": [0.0, 30.0]},
    {"kind": "line", "to": [0.0, 0.0]},
]


def test_a_closed_sketch_profile_becomes_an_extrude_with_sketch_profile_param():
    candidate = feature_tree_from_spec(_sketch_plate_spec(_SKETCH_SQUARE))
    assert candidate is not None
    base = candidate.features[0]
    assert base.kind == "extrude"
    assert base.params["sketch_profile"] == _SKETCH_SQUARE
    assert base.params["depth_mm"] == 10
    assert base.param_provenance["sketch_profile"].origin == "stated"


def test_an_open_sketch_loop_builds_no_solid():
    open_loop = [
        {"kind": "line", "to": [40.0, 0.0]},
        {"kind": "line", "to": [40.0, 30.0]},
        # never returns to (0, 0)
    ]
    assert feature_tree_from_spec(_sketch_plate_spec(open_loop)) is None


def test_a_sketch_loop_off_by_more_than_tolerance_builds_no_solid():
    almost_closed = [
        {"kind": "line", "to": [40.0, 0.0]},
        {"kind": "line", "to": [40.0, 30.0]},
        {"kind": "line", "to": [0.0, 30.0]},
        {"kind": "line", "to": [0.05, 0.05]},  # 0.07mm short of (0, 0)
    ]
    assert feature_tree_from_spec(_sketch_plate_spec(almost_closed)) is None


def test_sketch_hole_centres_stay_in_the_profiles_own_frame_no_corner_shift():
    spec = _sketch_plate_spec(
        _SKETCH_SQUARE, holes=[{"center_x_mm": 20, "center_y_mm": 15, "diameter_mm": 6}],
    )
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    hole = next(f for f in candidate.features if f.kind == "hole")
    # Unlike a rectangle (corner-anchored), a sketch profile has no natural
    # corner to shift to — the hole keeps the exact coordinates it was read
    # with, the same frame the sketch segments themselves are given in.
    assert hole.params["center_x_mm"] == 20
    assert hole.params["center_y_mm"] == 15


def test_sketch_without_thickness_builds_nothing():
    spec = _sketch_plate_spec(_SKETCH_SQUARE)
    spec["main_view"]["profile"].pop("thickness_mm")
    assert feature_tree_from_spec(spec) is None


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


def test_a_linear_pattern_expands_into_evenly_spaced_holes():
    spec = _plate_spec(hole_patterns=[{
        "kind": "linear", "count": 4, "hole_diameter_mm": 6,
        "spacing_mm": 20, "direction_deg": 0, "start_x_mm": -30, "start_y_mm": 0,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = sorted(
        (f for f in candidate.features if f.kind == "hole"),
        key=lambda f: f.params["center_x_mm"],
    )
    assert len(holes) == 4
    xs = [round(h.params["center_x_mm"], 3) for h in holes]
    # width_mm=120 → to_base offset +60; spec x -30,-10,10,30 → base 30,50,70,90.
    assert xs == [30.0, 50.0, 70.0, 90.0]
    assert all(h.params["diameter_mm"] == 6 for h in holes)


def test_a_rectangular_pattern_expands_into_a_grid():
    spec = _plate_spec(hole_patterns=[{
        "kind": "rectangular", "rows": 2, "columns": 3, "hole_diameter_mm": 5,
        "spacing_x_mm": 20, "spacing_y_mm": 15, "start_x_mm": -20, "start_y_mm": -7.5,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    holes = [f for f in candidate.features if f.kind == "hole"]
    assert len(holes) == 6
    assert all(h.params["diameter_mm"] == 5 for h in holes)
    # 2 rows x 3 columns → 2 distinct y's, 3 distinct x's.
    assert len({round(h.params["center_y_mm"], 3) for h in holes}) == 2
    assert len({round(h.params["center_x_mm"], 3) for h in holes}) == 3


def test_an_incomplete_linear_pattern_refuses_the_whole_candidate():
    # Missing direction_deg — the whole prismatic candidate must refuse,
    # exactly like an incomplete bolt_circle already does (no partial guess).
    spec = _plate_spec(hole_patterns=[{
        "kind": "linear", "count": 4, "hole_diameter_mm": 6,
        "spacing_mm": 20, "start_x_mm": -30, "start_y_mm": 0,
    }])
    assert feature_tree_from_spec(spec) is None


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


def test_a_rotated_slot_is_built_as_one_sketch_profile_pocket():
    spec = _plate_spec(slots=[{
        "center_x_mm": 0, "center_y_mm": 0, "length_mm": 40, "width_mm": 12,
        "rotation_deg": 30,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    pockets = [f for f in candidate.features if f.kind == "pocket"]
    assert len(pockets) == 1
    pocket = pockets[0]
    assert pocket.params["profile"] == "sketch"
    assert not [f for f in candidate.features if f.kind == "hole"]
    assert not any("повёрнут" in item and "не построен" in item for item in candidate.missing_data)
    # The sketch chain must close exactly (0 error) — rotation is applied to
    # every vertex, not to a kernel-side parameter that does not exist.
    sketch = pocket.params["sketch_profile"]
    x = y = 0.0
    for segment in sketch:
        x, y = segment["to"]
    assert (x, y) == (0.0, 0.0)


def test_a_round_slot_rotation_is_a_no_op_but_still_builds():
    # length_mm == width_mm (straight == 0) is legitimately a round slot —
    # rotating a circle changes nothing geometrically, but must not refuse.
    spec = _plate_spec(slots=[{
        "center_x_mm": 5, "center_y_mm": -5, "length_mm": 12, "width_mm": 12,
        "rotation_deg": 45,
    }])
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    pocket = next(f for f in candidate.features if f.kind == "pocket")
    assert pocket.params["profile"] == "sketch"
    assert all(segment["kind"] == "arc" for segment in pocket.params["sketch_profile"])


@pytest.mark.parametrize("rotation_deg", [0.0, 15.0, 60.0, 90.0, 200.0])
def test_a_rotated_slots_centre_offset_keeps_a_constant_radius(rotation_deg):
    # cad_solid.py's own centre (cx, cy) plus the sketch's local-origin
    # OFFSET must land the tool's local (0, 0) — one straight-side corner —
    # at a point exactly as far from the slot's true centre as before any
    # rotation was applied: rotation preserves distance from its own pivot,
    # so this holds for every angle without hand-deriving each one's trig.
    from app.ai.cad_solid import _rotated_capsule_sketch

    straight, radius = 28.0, 6.0
    offset_x, offset_y, _sketch = _rotated_capsule_sketch(straight, radius, rotation_deg)
    unrotated_corner_distance = math.hypot(straight / 2.0, radius)
    assert math.hypot(offset_x, offset_y) == pytest.approx(unrotated_corner_distance)


def test_plate_verification_checks_all_three_read_extents():
    spec = _plate_spec()
    report = {
        "bounds_mm": {"x": 120.0, "y": 60.0, "z": 10.0},
        "brep_valid": True, "manifold": True, "solid_count": 1,
        "volume_mm3": 72_000.0,
    }
    assert verify_solid_against_spec(report, spec).ok
    report["bounds_mm"]["z"] = 12.0
    assert not verify_solid_against_spec(report, spec).ok
