"""Cross-checks: the sheet's own arithmetic, and proportions off the raster."""

from __future__ import annotations

from app.ai.cad_recognize.spec_crosscheck import (
    check_spec_against_raster,
    check_spec_arithmetic,
    cross_check_spec,
)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_a_step_longer_than_all_the_others_is_flagged():
    """Live: 150+78+240+470 read off a spindle whose overall is 470."""
    spec = {"main_view": {"outer": [
        {"diameter_mm": 102.6, "length_mm": 150},
        {"diameter_mm": 56.55, "length_mm": 78},
        {"diameter_mm": 51, "length_mm": 240},
        {"diameter_mm": 80, "length_mm": 470},
    ]}}
    findings = check_spec_arithmetic(spec)
    assert "section_longer_than_all_others" in _codes(findings)
    # Suspicion, not proof: a long plain shaft is legal.
    assert all(f.severity == "warn" for f in findings)


def test_an_ordinary_stepped_shaft_raises_nothing():
    spec = {"main_view": {"outer": [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 50, "length_mm": 60},
        {"diameter_mm": 36, "length_mm": 25},
    ]}}
    assert check_spec_arithmetic(spec) == []


def test_sections_may_not_exceed_a_stated_overall():
    spec = {
        "main_view": {"outer": [
            {"diameter_mm": 30, "length_mm": 300},
            {"diameter_mm": 50, "length_mm": 300},
        ]},
        "dimensions": [{"value": "470"}, {"value": "Ø80js6"}],
    }
    findings = check_spec_arithmetic(spec)
    assert "sections_exceed_stated_overall" in _codes(findings)
    assert any(f.severity == "error" for f in findings)


def test_a_bore_wider_than_its_body_is_an_error():
    spec = {"main_view": {
        "outer": [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50, "length_mm": 60}],
        "bore": [{"diameter_mm": 60, "length_mm": 50}],
    }}
    assert "bore_not_inside_body" in _codes(check_spec_arithmetic(spec))


def test_a_bore_longer_than_its_body_is_an_error():
    spec = {"main_view": {
        "outer": [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50, "length_mm": 60}],
        "bore": [{"diameter_mm": 16, "length_mm": 200}],
    }}
    assert "bore_longer_than_body" in _codes(check_spec_arithmetic(spec))


def test_a_hole_outside_the_outline_is_an_error():
    spec = {"main_view": {"profile": {
        "shape": "circle", "diameter_mm": 140, "thickness_mm": 20,
        "holes": [{"center_x_mm": 90, "center_y_mm": 0, "diameter_mm": 14}],
    }}}
    assert "hole_outside_profile" in _codes(check_spec_arithmetic(spec))


def test_a_bolt_circle_that_does_not_fit_is_an_error():
    spec = {"main_view": {"profile": {
        "shape": "circle", "diameter_mm": 140, "thickness_mm": 20,
        "hole_patterns": [{
            "kind": "bolt_circle", "count": 6,
            "bolt_circle_diameter_mm": 200, "hole_diameter_mm": 14,
        }],
    }}}
    assert "bolt_circle_outside_profile" in _codes(check_spec_arithmetic(spec))


def test_a_flange_that_fits_raises_nothing():
    spec = {"main_view": {"profile": {
        "shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
        "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 80}],
        "hole_patterns": [{
            "kind": "bolt_circle", "count": 4,
            "bolt_circle_diameter_mm": 200, "hole_diameter_mm": 18,
        }],
    }}}
    assert check_spec_arithmetic(spec) == []


# --- proportions against the image ------------------------------------------


def _flange(bore_mm: float) -> dict:
    return {"main_view": {"profile": {
        "shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
        "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": bore_mm}],
    }}}


def test_a_misread_bore_contradicts_the_measured_proportions():
    """Live: Ø80H7 came back as Ø18 — 31:1 where the sheet shows 7:1."""
    measured = [280.0, 40.0]  # px radii of Ø560 and Ø80 on the sheet
    assert check_spec_against_raster(_flange(80), measured) == []
    findings = check_spec_against_raster(_flange(18), measured)
    assert "circle_ratio_mismatch" in _codes(findings)


def test_proportions_are_scale_free():
    """No mm-per-pixel is known at this point; ratios need no calibration."""
    for scale in (0.5, 1.0, 3.7):
        measured = [280.0 * scale, 40.0 * scale]
        assert check_spec_against_raster(_flange(80), measured) == []


def test_too_few_circles_to_compare_yields_no_verdict():
    assert check_spec_against_raster(_flange(80), [100.0]) == []
    assert check_spec_against_raster({"main_view": {}}, [100.0, 20.0]) == []


def test_the_report_counts_errors_separately_from_warnings():
    spec = {"main_view": {"outer": [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 50, "length_mm": 60},
        {"diameter_mm": 36, "length_mm": 500},
    ]}}
    report = cross_check_spec(spec)
    assert report["errors"] == 0
    assert len(report["findings"]) == 1
    assert report["findings"][0]["severity"] == "warn"


def test_silence_from_the_raster_check_is_not_reported_as_agreement():
    """A flange once passed while the check had measured one 4 px hole."""
    spec = {"main_view": {"profile": {
        "shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
        "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 80}],
    }}}

    class _Ink:
        pass

    import app.ai.cad_recognize.spec_crosscheck as module

    original = module.measure_circle_radii
    module.measure_circle_radii = lambda _ink: [4.0]
    try:
        report = cross_check_spec(spec, _Ink())
    finally:
        module.measure_circle_radii = original
    assert report["errors"] == 0
    assert report["raster_check"] == "insufficient_measurements"


def test_no_image_says_the_check_was_not_attempted():
    assert cross_check_spec({"main_view": {}})["raster_check"] == "not_attempted"
