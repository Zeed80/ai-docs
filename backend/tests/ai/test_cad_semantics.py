"""Stage 3: drawing semantics bound to the edges the kernel actually built."""

from __future__ import annotations

import math

from app.ai.cad_semantics import (
    bind_spec_to_solid,
    collect_part_properties,
    parse_dimension,
)


def _circle_edge(index: int, diameter: float) -> dict:
    return {
        "key": f"edge-d{diameter}-{index}",
        "index": index,
        "curve": "Circle",
        "length_mm": math.pi * diameter,
    }


def _line_edge(index: int, length: float) -> dict:
    return {
        "key": f"edge-l{length}-{index}",
        "index": index,
        "curve": "Line",
        "length_mm": length,
    }


def _report() -> dict:
    return {
        "edges": [
            _circle_edge(1, 30.0),
            _circle_edge(2, 16.0),
            _circle_edge(3, 30.0),
            _line_edge(4, 40.0),
            _circle_edge(5, 16.0),
            _line_edge(6, 90.0),
            _circle_edge(7, 50.0),
            _circle_edge(8, 50.0),
            _line_edge(9, 60.0),
        ],
        "bounds_mm": {"x": 50.0, "y": 50.0, "z": 100.0},
        "volume_mm3": 127988.0,
        "surface_area_mm2": 21645.0,
    }


# --- callout parsing --------------------------------------------------------


def test_fit_and_deviation_are_kept_verbatim():
    parsed = parse_dimension("Ø102h6(-0,022)")
    assert parsed["nominal_mm"] == 102.0
    assert parsed["is_diameter"] is True
    assert parsed["fit"] == "h6"
    assert parsed["deviation"] == "-0,022"


def test_thread_pitch_is_not_mistaken_for_a_fit_class():
    parsed = parse_dimension("M75x1,5")
    assert parsed["thread"] == "M75x1.5"
    assert parsed["fit"] is None
    assert parsed["is_diameter"] is True


def test_roughness_and_hardness_are_not_dimensions():
    """Binding "Ra 1,6" to a 1.6 mm edge would tag the wrong surface."""
    assert parse_dimension("Ra 1,6") is None
    assert parse_dimension("Rz 20") is None
    assert parse_dimension("HRC 58...62") is None
    assert parse_dimension("Твёрдость 45") is None


def test_a_ratio_callout_is_a_slope_not_a_size():
    assert parse_dimension("конус 7:24") is None
    assert parse_dimension("1:5") is None


def test_plain_length_and_diameter_still_parse():
    assert parse_dimension("78")["is_diameter"] is False
    assert parse_dimension("Ø26")["is_diameter"] is True
    assert parse_dimension("не число") is None


# --- binding ----------------------------------------------------------------


def _spec(**extra) -> dict:
    spec = {
        "part": "Втулка",
        "main_view": {
            "type": "вал",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40, "note": "резьба M30x1,5"},
                {"diameter_mm": 50, "length_mm": 60},
            ],
            "bore": [{"diameter_mm": 16, "length_mm": 90}],
        },
        "title_block": {"material": "Сталь 45 ГОСТ 1050-2013"},
    }
    spec.update(extra)
    return spec


def test_a_fit_lands_on_the_edges_that_measure_it():
    spec = _spec(dimensions=[{"value": "Ø50js6", "applies_to": "шейка"}])
    result = bind_spec_to_solid(spec, _report())
    assert result["bound_count"] == 2  # the callout plus the section thread note
    fit = next(b for b in result["bindings"] if b["text"] == "Ø50js6")
    assert fit["fit"] == "js6"
    assert sorted(fit["edge_indices"]) == [7, 8]
    assert fit["applies_to"] == "шейка"


def test_a_bore_fit_binds_to_the_bore_edges():
    spec = _spec(dimensions=[{"value": "Ø16H7"}])
    result = bind_spec_to_solid(spec, _report())
    bore = next(b for b in result["bindings"] if b["text"] == "Ø16H7")
    assert sorted(bore["edge_indices"]) == [2, 5]


def test_a_length_binds_to_a_line_not_to_a_circle_of_the_same_size():
    spec = _spec(dimensions=[{"value": "40"}])
    result = bind_spec_to_solid(spec, _report())
    length = next(b for b in result["bindings"] if b["text"] == "40")
    assert length["kind"] == "length"
    assert length["edge_indices"] == [4]


def test_a_thread_note_binds_to_its_own_diameter():
    result = bind_spec_to_solid(_spec(), _report())
    thread = next(b for b in result["bindings"] if b["thread"])
    assert thread["thread"] == "M30x1.5"
    assert sorted(thread["edge_indices"]) == [1, 3]


def test_a_value_the_model_does_not_contain_is_reported_not_attached():
    """A tolerance on the wrong diameter is worse than one nobody applied."""
    spec = _spec(dimensions=[{"value": "Ø999h6"}])
    result = bind_spec_to_solid(spec, _report())
    assert result["unmatched_count"] == 1
    assert result["unmatched"][0]["nominal_mm"] == 999.0
    assert all(b["text"] != "Ø999h6" for b in result["bindings"])


def test_roughness_never_enters_the_dimension_bindings():
    spec = _spec(dimensions=[{"value": "Ra 1,6"}])
    result = bind_spec_to_solid(spec, _report())
    assert all("Ra" not in b["text"] for b in result["bindings"])
    assert all("Ra" not in b["text"] for b in result["unmatched"])


# --- part properties --------------------------------------------------------


def test_part_properties_carry_what_cam_needs():
    spec = _spec(
        annotations=[
            {"kind": "roughness", "text": "Ra 1,6"},
            {"kind": "hardness", "text": "HRC 58...62"},
        ]
    )
    properties = collect_part_properties(spec, _report())
    assert properties["material"] == "Сталь 45 ГОСТ 1050-2013"
    assert properties["round_stock_diameter_mm"] == 50.0
    assert properties["stock_envelope_mm"]["length"] == 100.0
    assert properties["notes"]["roughness"] == ["Ra 1,6"]
    assert properties["notes"]["hardness"] == ["HRC 58...62"]


def test_missing_material_is_absent_rather_than_guessed():
    spec = _spec()
    spec["title_block"] = {}
    assert collect_part_properties(spec, _report())["material"] is None
