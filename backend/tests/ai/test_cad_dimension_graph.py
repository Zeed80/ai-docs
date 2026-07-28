from app.ai.cad_dimension_graph import build_dimension_graph


def test_dimension_graph_accepts_a_consistent_outer_chain():
    graph = build_dimension_graph({
        "main_view": {"outer": [
            {"diameter_mm": 20, "length_mm": 40},
            {"diameter_mm": 30, "length_mm": 60},
        ]},
        "dimensions": [{"value": "100", "applies_to": "габаритная длина"}],
    })
    assert graph["status"] == "ok"
    assert graph["errors"] == []


def test_dimension_graph_blocks_a_mismatched_overall_dimension():
    graph = build_dimension_graph({
        "main_view": {"outer": [{"diameter_mm": 20, "length_mm": 40}]},
        "dimensions": [{"value": "470", "applies_to": "габаритная длина"}],
    })
    assert graph["status"] == "conflict"
    assert "470" in graph["errors"][0]


def test_dimension_graph_blocks_features_outside_the_body():
    graph = build_dimension_graph({
        "main_view": {
            "outer": [{"diameter_mm": 20, "length_mm": 100}],
            "keyways": [{"axial_start_mm": 80, "length_mm": 30}],
        }
    })
    assert graph["status"] == "conflict"
    assert "keyways[0]" in graph["errors"][0]


def test_internal_callout_cannot_be_silently_used_as_outer_geometry():
    graph = build_dimension_graph({
        "main_view": {"outer": [{"diameter_mm": 56.55, "length_mm": 100}]},
        "dimensions": [{"value": "Ø56,55", "applies_to": "конус 7:24"}],
    })
    assert graph["status"] == "conflict"
    assert "bore[] отсутствует" in graph["errors"][0]


def test_offset_bore_must_end_inside_the_part():
    graph = build_dimension_graph({
        "main_view": {
            "outer": [{"diameter_mm": 80, "length_mm": 100}],
            "bore": [{"diameter_mm": 20, "length_mm": 80}],
            "bore_start_mm": 30,
        }
    })
    assert graph["status"] == "conflict"
    assert "30..110" in graph["errors"][0]


def test_profile_hole_must_fit_inside_rectangle():
    graph = build_dimension_graph({
        "main_view": {"profile": {
            "shape": "rectangle",
            "width_mm": 100,
            "height_mm": 60,
            "thickness_mm": 10,
            "holes": [{"center_x_mm": 49, "center_y_mm": 0, "diameter_mm": 10}],
        }}
    })
    assert graph["status"] == "conflict"
    assert "выходит за контур" in graph["errors"][0]


def test_cross_hole_must_fit_local_shaft_diameter():
    graph = build_dimension_graph({
        "main_view": {
            "outer": [
                {"diameter_mm": 20, "length_mm": 50},
                {"diameter_mm": 80, "length_mm": 50},
            ],
            "cross_holes": [{"diameter_mm": 30, "axial_position_mm": 20}],
        }
    })
    assert graph["status"] == "conflict"
    assert "локальный диаметр" in graph["errors"][0]


def test_dimension_graph_exposes_derived_section_coordinates():
    graph = build_dimension_graph({
        "main_view": {"outer": [
            {"diameter_mm": 20, "length_mm": 40},
            {"diameter_mm": 30, "length_mm": 60},
        ]}
    })
    nodes = {node["id"]: node["value_mm"] for node in graph["nodes"]}
    assert nodes["main_view.outer.1.z_start_mm"] == 40
    assert nodes["main_view.outer.1.z_end_mm"] == 100
