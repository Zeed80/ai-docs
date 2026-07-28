"""Stage 5: machining data derived from the compiled solid."""

from __future__ import annotations

import pytest

from app.ai.cad_machining import (
    blank_from_solid,
    deviations_from_fit,
    it_tolerance_mm,
    surface_specs_from_solid,
)


# --- ГОСТ 25346 tolerances --------------------------------------------------


def test_it_grades_match_the_standard_table():
    assert it_tolerance_mm(80, 6) == pytest.approx(0.019)
    assert it_tolerance_mm(30, 7) == pytest.approx(0.021)
    assert it_tolerance_mm(16, 7) == pytest.approx(0.018)
    assert it_tolerance_mm(500, 5) == pytest.approx(0.027)


def test_a_nominal_beyond_the_table_yields_nothing():
    assert it_tolerance_mm(900, 6) is None
    assert it_tolerance_mm(50, 99) is None


def test_symmetric_shaft_and_hole_fits_resolve_to_deviations():
    assert deviations_from_fit(80, "js6") == (
        pytest.approx(0.0095), pytest.approx(-0.0095), "gost_25346"
    )
    assert deviations_from_fit(30, "h7") == (0.0, pytest.approx(-0.021), "gost_25346")
    assert deviations_from_fit(16, "H7") == (pytest.approx(0.018), 0.0, "gost_25346")


def test_tabulated_offset_shaft_fits_resolve_without_guessing():
    assert deviations_from_fit(50, "k6") == (
        pytest.approx(0.018), pytest.approx(0.002), "gost_25346"
    )
    assert deviations_from_fit(80, "m6") == (
        pytest.approx(0.030), pytest.approx(0.011), "gost_25346"
    )
    assert deviations_from_fit(120, "n6") == (
        pytest.approx(0.045), pytest.approx(0.023), "gost_25346"
    )
    assert deviations_from_fit(180, "p6") == (
        pytest.approx(0.068), pytest.approx(0.043), "gost_25346"
    )


def test_an_untabulated_fit_direction_is_not_guessed():
    upper, lower, source = deviations_from_fit(50, "e7")
    assert upper is None and lower is None
    assert source == "grade_only_it7"


def test_tabulated_clearance_shaft_fits_resolve_to_negative_deviations():
    assert deviations_from_fit(25, "f7") == (
        pytest.approx(-0.020), pytest.approx(-0.041), "gost_25346"
    )
    assert deviations_from_fit(80, "g6") == (
        pytest.approx(-0.010), pytest.approx(-0.029), "gost_25346"
    )


def test_a_missing_fit_says_so_rather_than_defaulting():
    assert deviations_from_fit(50, None) == (None, None, "not_stated")
    assert deviations_from_fit(50, "мусор") == (None, None, "unparsed")


# --- surface specs ----------------------------------------------------------


def _semantics() -> dict:
    return {"bindings": [
        {"text": "Ø50js6", "nominal_mm": 50.0, "kind": "diameter", "fit": "js6",
         "deviation": None, "thread": None, "applies_to": None, "edge_keys": ["e7", "e8"]},
        {"text": "Ø16H7", "nominal_mm": 16.0, "kind": "diameter", "fit": "H7",
         "deviation": None, "thread": None, "applies_to": "расточка", "edge_keys": ["e2"]},
        {"text": "40", "nominal_mm": 40.0, "kind": "length", "fit": None,
         "deviation": None, "thread": None, "applies_to": None, "edge_keys": ["e4"]},
        {"text": "резьба M30x1,5", "nominal_mm": 30.0, "kind": "diameter", "fit": None,
         "deviation": None, "thread": "M30x1.5", "applies_to": None, "edge_keys": ["e1"]},
    ]}


def _properties(**extra) -> dict:
    properties = {
        "material": "Сталь 45 ГОСТ 1050-2013",
        "notes": {"roughness": ["Ra 1,6"]},
        "stock_envelope_mm": {"length": 100.0, "width": 50.0, "height": 50.0},
        "round_stock_diameter_mm": 50.0,
        "volume_mm3": 127988.0,
    }
    properties.update(extra)
    return properties


def test_an_axial_length_is_not_a_machined_surface():
    """A shaft's process sheet must not carry a fictitious milling operation."""
    specs = surface_specs_from_solid(_semantics(), _properties())
    assert all(spec["source_callout"] != "40" for spec in specs)
    assert {spec["surface_type"] for spec in specs} == {
        "external_cylindrical", "hole", "thread"
    }


def test_a_bore_is_internal_and_bored_not_drilled_at_this_roughness():
    specs = surface_specs_from_solid(_semantics(), _properties())
    bore = next(s for s in specs if s["source_callout"] == "Ø16H7")
    assert bore["is_internal"] is True
    assert bore["machining_method"] == "boring"
    assert bore["upper_tol"] == pytest.approx(0.018)


def test_every_tolerance_declares_where_it_came_from():
    specs = surface_specs_from_solid(_semantics(), _properties())
    sources = {s["source_callout"]: s["tolerance_source"] for s in specs}
    assert sources["Ø50js6"] == "gost_25346"
    assert sources["резьба M30x1,5"] == "not_stated"


def test_a_written_deviation_beats_the_table():
    semantics = {"bindings": [{
        "text": "Ø102h6(-0,022)", "nominal_mm": 102.0, "kind": "diameter",
        "fit": None, "deviation": "-0,022", "thread": None, "applies_to": None,
        "edge_keys": [],
    }]}
    spec = surface_specs_from_solid(semantics, _properties())[0]
    assert spec["lower_tol"] == pytest.approx(-0.022)
    assert spec["tolerance_source"] == "stated"


def test_surface_specs_keep_the_edges_they_belong_to():
    specs = surface_specs_from_solid(_semantics(), _properties())
    fit = next(s for s in specs if s["source_callout"] == "Ø50js6")
    assert fit["edge_keys"] == ["e7", "e8"]


# --- blank ------------------------------------------------------------------


def test_a_turned_part_gets_round_bar_with_allowance():
    blank = blank_from_solid(_properties())
    assert blank["kind"] == "round_bar"
    assert blank["dimensions"]["diameter_mm"] == pytest.approx(54.0)
    assert blank["dimensions"]["length_mm"] == pytest.approx(103.0)
    assert 0.0 < blank["material_utilisation"] < 1.0


def test_a_non_round_envelope_gets_plate_stock():
    blank = blank_from_solid(_properties(
        stock_envelope_mm={"length": 10.0, "width": 120.0, "height": 60.0},
        round_stock_diameter_mm=120.0,
    ))
    assert blank["kind"] == "plate"
    assert blank["dimensions"]["width_mm"] == pytest.approx(123.0)


def test_no_envelope_means_no_blank_rather_than_an_imagined_billet():
    assert blank_from_solid({"stock_envelope_mm": {}}) is None
