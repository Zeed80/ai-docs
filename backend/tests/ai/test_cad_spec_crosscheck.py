"""Cross-checks: the sheet's own arithmetic, and proportions off the raster."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.ai.cad_recognize.spec_crosscheck import (
    check_axial_hatching_against_bore,
    check_spec_against_raster,
    check_spec_arithmetic,
    cross_check_spec,
    detect_axial_hatching,
)

ROOT = Path(__file__).resolve().parents[3]


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


# --- is this a machined part at all -----------------------------------------


def test_a_building_plan_read_as_a_shaft_is_refused():
    """Live: a floor plan produced a 3-metre shaft with zero unresolved."""
    plan = {
        "part": "наименование",
        "main_view": {"type": "тело вращения (вал)", "outer": [
            {"diameter_mm": 3000, "length_mm": 4500},
            {"diameter_mm": 2000, "length_mm": 7500},
        ]},
        "title_block": {"material": "материал", "scale": "масштаб"},
    }
    codes = _codes(check_spec_arithmetic(plan))
    assert "stamp_placeholders_read_as_values" in codes
    assert "implausible_turned_diameter" in codes
    assert "implausible_part_length" in codes


def test_a_stamp_echoing_its_own_captions_is_an_error_on_its_own():
    spec = {
        "main_view": {"outer": [
            {"diameter_mm": 30, "length_mm": 40},
            {"diameter_mm": 50, "length_mm": 60},
        ]},
        "title_block": {"material": "материал"},
    }
    assert "stamp_placeholders_read_as_values" in _codes(check_spec_arithmetic(spec))


def test_a_real_part_passes_the_plausibility_gates():
    spec = {
        "main_view": {"outer": [
            {"diameter_mm": 102, "length_mm": 150},
            {"diameter_mm": 80, "length_mm": 240},
        ]},
        "title_block": {"material": "Сталь 45 ГОСТ 1050-2013", "scale": "1:2"},
    }
    assert check_spec_arithmetic(spec) == []


def test_the_measured_outline_rejects_an_impossible_hole():
    """Calibrated from the image: a hole cannot exceed the outline drawn."""
    from app.ai.cad_recognize.spec_crosscheck import check_outline_against_image

    def flange(bore: float) -> dict:
        return {"main_view": {"type": "фланец", "profile": {
            "shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
            "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": bore}],
        }}}

    measured_px = 277.4  # the flange outline as the detector measures it
    assert check_outline_against_image(flange(80), measured_px) == []
    codes = _codes(check_outline_against_image(flange(600), measured_px))
    assert "hole_larger_than_measured_outline" in codes


def test_no_measurement_means_no_verdict_from_the_image():
    from app.ai.cad_recognize.spec_crosscheck import check_outline_against_image

    spec = {"main_view": {"profile": {"shape": "circle", "diameter_mm": 560}}}
    assert check_outline_against_image(spec, None) == []
    assert check_outline_against_image(spec, 0) == []


# ── axial hatching evidence (Phase E, 2026-08-14: "разрез не прочитан") ────


def _lines(*segments: tuple[float, float, float, float]):
    """Shape HoughLinesP itself returns: (N, 1, 4) int32."""
    return np.array([[list(seg)] for seg in segments], dtype="int32")


def test_a_bounded_45_degree_band_is_detected_as_hatching():
    ink = np.zeros((200, 200), dtype="uint8")
    # 15 short 45°-ish parallel strokes inside a bounded band — enough to
    # clear the "real fill, not a stray diagonal line" threshold (12).
    segments = tuple(
        (40 + i * 4, 60, 40 + i * 4 + 20, 80) for i in range(15)
    )
    with patch("cv2.HoughLinesP", return_value=_lines(*segments)):
        result = detect_axial_hatching(ink)
    assert result is not None
    assert result["segment_count"] == 15


def test_a_handful_of_stray_diagonal_lines_is_not_hatching():
    ink = np.zeros((200, 200), dtype="uint8")
    segments = tuple((40 + i * 4, 60, 40 + i * 4 + 20, 80) for i in range(3))
    with patch("cv2.HoughLinesP", return_value=_lines(*segments)):
        assert detect_axial_hatching(ink) is None


def test_lines_that_are_not_roughly_45_degrees_are_not_hatching():
    """A frame border or a dimension line is axis-aligned, not diagonal —
    the detector must not mistake it for section fill."""
    ink = np.zeros((200, 200), dtype="uint8")
    horizontal = tuple((10, 10 + i * 4, 190, 10 + i * 4) for i in range(15))
    with patch("cv2.HoughLinesP", return_value=_lines(*horizontal)):
        assert detect_axial_hatching(ink) is None


def test_hatching_spanning_the_whole_sheet_is_treated_as_a_border_artifact():
    ink = np.zeros((200, 200), dtype="uint8")
    # Same 45° angle, but the bounding box covers (almost) the whole sheet —
    # a watermark/border pattern, not a bounded bore hatch.
    segments = tuple((i * 4, i * 4, i * 4 + 20, i * 4 + 20) for i in range(48))
    with patch("cv2.HoughLinesP", return_value=_lines(*segments)):
        assert detect_axial_hatching(ink) is None


def test_no_lines_found_at_all_is_not_hatching():
    ink = np.zeros((200, 200), dtype="uint8")
    with patch("cv2.HoughLinesP", return_value=None):
        assert detect_axial_hatching(ink) is None


def test_hatching_with_no_stated_bore_is_the_exact_live_bug():
    """detal_126.png, 2026-08-14: the reader said bore: [] on a part whose
    section view clearly shows one — cad_solid.py built it solid with no
    hard blocker because the reader never flagged a section view either.
    This is the independent, image-grounded signal that catches it."""
    spec = {"main_view": {
        "outer": [{"diameter_mm": 40, "length_mm": 100}], "bore": [],
    }}
    findings = check_axial_hatching_against_bore(
        spec, {"segment_count": 43, "bbox_px": [1, 1, 2, 2]},
    )
    codes = {f.code for f in findings}
    assert "axial_hatching_bore_mismatch" in codes
    assert all(f.severity == "error" for f in findings)


def test_a_correctly_stated_bore_with_hatching_raises_nothing():
    spec = {"main_view": {
        "outer": [{"diameter_mm": 40, "length_mm": 100}],
        "bore": [{"diameter_mm": 20, "length_mm": 100}],
    }}
    findings = check_axial_hatching_against_bore(
        spec, {"segment_count": 43, "bbox_px": [1, 1, 2, 2]},
    )
    assert findings == []


def test_a_stated_bore_with_no_detected_hatching_is_only_a_warning():
    """The detector's own false-negative rate is unmeasured — this must not
    read as proof the bore is wrong, only a softer prompt to double-check."""
    spec = {"main_view": {
        "outer": [{"diameter_mm": 40, "length_mm": 100}],
        "bore": [{"diameter_mm": 20, "length_mm": 100}],
    }}
    findings = check_axial_hatching_against_bore(spec, None)
    assert [f.code for f in findings] == ["no_axial_hatching_for_stated_bore"]
    assert findings[0].severity == "warn"


def test_hatching_without_any_outer_profile_is_not_evaluated():
    """A prismatic/unclassified part has nothing for this check to mean
    anything about — the check is scoped to bodies of revolution."""
    findings = check_axial_hatching_against_bore(
        {"main_view": {}}, {"segment_count": 43, "bbox_px": [1, 1, 2, 2]},
    )
    assert findings == []


def test_detect_axial_hatching_finds_the_live_spindle_bore():
    """Real image, not mocked: detal_126.png is a hollow spindle whose
    section view shows axial hatching — the true positive this whole
    detector exists for."""
    from app.tasks.cad_trace import _binarize

    ink, _w, _h = _binarize((ROOT / "test_vector_files" / "detal_126.png").read_bytes())
    assert detect_axial_hatching(ink) is not None


def test_detect_axial_hatching_is_silent_on_a_solid_shaft():
    """example-drawings/shaft_detail.png is a genuinely solid stepped shaft
    with no section view and no bore — the true negative."""
    from app.tasks.cad_trace import _binarize

    ink, _w, _h = _binarize(
        (ROOT / "example-drawings" / "shaft_detail.png").read_bytes()
    )
    assert detect_axial_hatching(ink) is None


# ── view correspondence (Фаза 1.1: cross-view feature linkage) ─────────────


def _spec_with_named_cross_hole() -> dict:
    return {
        "main_view": {
            "outer": [{"diameter_mm": 40, "length_mm": 100, "id": "0:outer:0"}],
            "cross_holes": [
                {"diameter_mm": 9, "axial_position_mm": 20, "id": "0:cross_holes:0"},
            ],
        },
        "views": [
            {"kind": "front", "view_id": "front", "body_index": 0,
             "features_shown": ["0:cross_holes:0"]},
            {"kind": "side", "view_id": "side", "body_index": 0,
             "features_shown": ["0:cross_holes:0"]},
        ],
    }


def test_spec_view_geometries_only_uses_features_shown():
    from app.ai.cad_recognize.spec_crosscheck import spec_view_geometries

    geometries = spec_view_geometries(_spec_with_named_cross_hole())

    assert len(geometries) == 2
    assert geometries[0].diameters_mm == [9]
    assert geometries[0].diameter_feature_ids == ["0:cross_holes:0"]


def test_spec_view_geometries_empty_without_features_shown():
    """Silence, not a guessed link — the honest default before a view names
    anything explicitly (features_shown is not populated by the reader yet)."""
    from app.ai.cad_recognize.spec_crosscheck import spec_view_geometries

    spec = _spec_with_named_cross_hole()
    for view in spec["views"]:
        view["features_shown"] = []

    assert spec_view_geometries(spec) == []


def test_check_view_correspondence_resolves_matching_feature_ids():
    from app.ai.cad_recognize.spec_crosscheck import check_view_correspondence

    result = check_view_correspondence(_spec_with_named_cross_hole())

    assert result["correspondences"], result
    match = result["correspondences"][0]
    assert match["kind"] == "diameter"
    assert match["feature_ids"] == ["0:cross_holes:0", "0:cross_holes:0"]
    assert result["issues"] == []


def test_cross_check_spec_carries_view_correspondence():
    report = cross_check_spec(_spec_with_named_cross_hole())

    assert "view_correspondence" in report
    assert report["view_correspondence"]["correspondences"]
