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
