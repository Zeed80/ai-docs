import pytest

from app.ai.cad_dimension_graph import build_dimension_graph


def test_dimension_graph_accepts_a_consistent_outer_chain():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 20, "length_mm": 40},
                    {"diameter_mm": 30, "length_mm": 60},
                ]
            },
            "dimensions": [{"value": "100", "applies_to": "габаритная длина"}],
        }
    )
    assert graph["status"] == "ok"
    assert graph["errors"] == []


def test_dimension_graph_blocks_a_mismatched_overall_dimension():
    graph = build_dimension_graph(
        {
            "main_view": {"outer": [{"diameter_mm": 20, "length_mm": 40}]},
            "dimensions": [{"value": "470", "applies_to": "габаритная длина"}],
        }
    )
    assert graph["status"] == "conflict"
    assert "470" in graph["errors"][0]


def test_dimension_graph_blocks_features_outside_the_body():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [{"diameter_mm": 20, "length_mm": 100}],
                "keyways": [{"axial_start_mm": 80, "length_mm": 30}],
            }
        }
    )
    assert graph["status"] == "conflict"
    assert "keyways[0]" in graph["errors"][0]


def test_internal_callout_cannot_be_silently_used_as_outer_geometry():
    graph = build_dimension_graph(
        {
            "main_view": {"outer": [{"diameter_mm": 56.55, "length_mm": 100}]},
            "dimensions": [{"value": "Ø56,55", "applies_to": "конус 7:24"}],
        }
    )
    assert graph["status"] == "conflict"
    assert "bore[] отсутствует" in graph["errors"][0]


def test_offset_bore_must_end_inside_the_part():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [{"diameter_mm": 80, "length_mm": 100}],
                "bore": [{"diameter_mm": 20, "length_mm": 80}],
                "bore_start_mm": 30,
            }
        }
    )
    assert graph["status"] == "conflict"
    assert "30..110" in graph["errors"][0]


def test_profile_hole_must_fit_inside_rectangle():
    graph = build_dimension_graph(
        {
            "main_view": {
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100,
                    "height_mm": 60,
                    "thickness_mm": 10,
                    "holes": [{"center_x_mm": 49, "center_y_mm": 0, "diameter_mm": 10}],
                }
            }
        }
    )
    assert graph["status"] == "conflict"
    assert "выходит за контур" in graph["errors"][0]


def test_cross_hole_must_fit_local_shaft_diameter():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 20, "length_mm": 50},
                    {"diameter_mm": 80, "length_mm": 50},
                ],
                "cross_holes": [{"diameter_mm": 30, "axial_position_mm": 20}],
            }
        }
    )
    assert graph["status"] == "conflict"
    assert "локальный диаметр" in graph["errors"][0]


def test_dimension_graph_exposes_derived_section_coordinates():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 20, "length_mm": 40},
                    {"diameter_mm": 30, "length_mm": 60},
                ]
            }
        }
    )
    nodes = {node["id"]: node["value_mm"] for node in graph["nodes"]}
    assert nodes["main_view.outer.1.z_start_mm"] == 40
    assert nodes["main_view.outer.1.z_end_mm"] == 100


def test_dimension_graph_resolves_fit_and_symmetric_tolerance_intervals():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 80, "length_mm": 40, "tolerance": "js6"},
                    {"diameter_mm": 30, "length_mm": 20, "tolerance": "±0,1"},
                ]
            }
        }
    )
    nodes = {node["id"]: node["value_mm"] for node in graph["nodes"]}

    assert graph["status"] == "ok"
    assert nodes["main_view.outer.0.diameter_min_mm"] == 79.9905
    assert nodes["main_view.outer.0.diameter_max_mm"] == 80.0095
    assert nodes["main_view.outer.1.diameter_min_mm"] == 29.9
    assert nodes["main_view.outer.1.diameter_max_mm"] == 30.1


def test_dimension_graph_blocks_invalid_tolerance_instead_of_ignoring_it():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 80, "length_mm": 40, "tolerance": "Zz99"},
                ]
            }
        }
    )

    assert graph["status"] == "conflict"
    assert "Zz99" in graph["errors"][0]


def test_tabulated_offset_fit_becomes_a_complete_interval():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 80, "length_mm": 40, "tolerance": "k6"},
                ]
            }
        }
    )
    tolerance = next(item for item in graph["constraints"] if item["kind"] == "tolerance_interval")

    assert graph["status"] == "ok"
    assert tolerance["source"] == "gost_25346"
    assert tolerance["lower_deviation_mm"] == pytest.approx(0.002)
    assert tolerance["upper_deviation_mm"] == pytest.approx(0.021)
    assert tolerance["interval_complete"] is True


def test_untabulated_offset_fit_stays_an_explicit_partial_interval():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 80, "length_mm": 40, "tolerance": "e7"},
                ]
            }
        }
    )
    tolerance = next(item for item in graph["constraints"] if item["kind"] == "tolerance_interval")

    assert graph["status"] == "ok"
    assert tolerance["source"] == "grade_only_it7"
    assert tolerance["interval_complete"] is False


def test_clearance_fit_is_a_complete_negative_interval():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 80, "length_mm": 40, "tolerance": "g6"},
                ]
            }
        }
    )
    tolerance = next(item for item in graph["constraints"] if item["kind"] == "tolerance_interval")

    assert graph["status"] == "ok"
    assert tolerance["source"] == "gost_25346"
    assert tolerance["lower_deviation_mm"] == pytest.approx(-0.029)
    assert tolerance["upper_deviation_mm"] == pytest.approx(-0.010)
    assert tolerance["interval_complete"] is True


def test_dimension_graph_blocks_a_fillet_larger_than_its_shoulder():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [
                    {"diameter_mm": 40, "length_mm": 20},
                    {"diameter_mm": 50, "length_mm": 30},
                ],
                "fillets": [
                    {
                        "radius_mm": 6,
                        "location": "shoulder",
                        "at_z_mm": 20,
                    }
                ],
            }
        }
    )

    assert graph["status"] == "conflict"
    assert "максимум R5" in graph["errors"][0]


def test_dimension_graph_checks_flat_profile_corner_radius():
    graph = build_dimension_graph(
        {
            "main_view": {
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100,
                    "height_mm": 40,
                    "corner_radius_mm": 21,
                }
            }
        }
    )

    assert graph["status"] == "conflict"
    assert "максимум R20" in graph["errors"][0]


def test_axial_thread_fit_uses_finished_nominal_when_tap_drill_is_absent():
    graph = build_dimension_graph(
        {
            "main_view": {
                "outer": [{"diameter_mm": 80, "length_mm": 40}],
                "axial_holes": [
                    {
                        "count": 2,
                        "bolt_circle_diameter_mm": 65,
                        "pilot_diameter_mm": None,
                        "thread": {"designation": "M8", "nominal_diameter_mm": 8},
                    }
                ],
            }
        }
    )
    constraint = next(
        item for item in graph["constraints"] if item["kind"] == "pitch_circle_inside"
    )

    assert graph["status"] == "ok"
    assert constraint["diameter_source"] == "thread_nominal"
    assert constraint["finished_hole_envelope_diameter_mm"] == 8


def test_hole_must_fit_inside_the_rounded_corner_not_only_the_bounding_box():
    graph = build_dimension_graph(
        {
            "main_view": {
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100,
                    "height_mm": 60,
                    "corner_radius_mm": 15,
                    "holes": [{"center_x_mm": 44, "center_y_mm": 24, "diameter_mm": 8}],
                }
            }
        }
    )

    assert graph["status"] == "conflict"
    assert "выходит за контур" in graph["errors"][0]
