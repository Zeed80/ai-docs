from pathlib import Path

import pytest
from PIL import Image

from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions
from app.ai.cad_recognize.turned_features import localize_turned_features


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "test_vector_files" / "detal_126.png"


def test_real_spindle_localizes_both_keyway_outlines():
    image = Image.open(FIXTURE).convert("RGB")
    axial = localize_axial_dimensions(
        image, [470, 270, 240, 150, 99, 85, 78, 50, 35, 26, 25, 20, 18, 15, 14, 12, 8, 5, 4, 3]
    )

    evidence = localize_turned_features(
        image,
        axial,
        [
            470, 270, 240, 150, 99, 85, 78, 50, 35, 26, 25, 20, 18,
            15, 14, 12, 8, 5, 4, 3,
        ],
        profile_center_y_px=593,
        known_diameter_values=[102, 80, 72, 65, 56.55, 56, 55, 51, 50, 44, 24, 14, 10, 9],
        outer_diameter_values=[102, 80, 72],
    )

    assert evidence["status"] == "ok"
    slots = evidence["keyway_candidates"]
    assert len(slots) == 2
    assert [(slot["stated_length_mm"], slot["stated_width_mm"]) for slot in slots] == [
        (85.0, 12.0),
        (35.0, 8.0),
    ]
    assert slots[0]["axial_start_mm"] == pytest.approx(277.0, abs=0.5)
    assert slots[1]["axial_start_mm"] == pytest.approx(372.0, abs=1.5)
    assert slots[0]["depth_observation"]["value_mm"] == 5.0
    assert slots[1]["depth_observation"]["value_mm"] == 3.5
    assert slots[1]["depth_observation"]["section_outer_diameter_mm"] == 72.0
    radial = evidence["radial_opening_candidates"]
    assert any(
        item["axial_position_mm"] == pytest.approx(410, abs=0.5)
        and item["supported_diameters_mm"] == [14.0]
        for item in radial
    )
    assert any(
        item["axial_position_mm"] == pytest.approx(455, abs=0.7)
        and set(item["supported_diameters_mm"]) == {9.0, 10.0}
        for item in radial
    )
    labels = {
        (item["value_mm"], item["side"])
        for item in evidence["diameter_label_observations"]
    }
    assert {(14.0, "top"), (24.0, "top"), (10.0, "top"), (9.0, "bottom")} <= labels
    assert any("несколько" in item for item in evidence["blockers"])
    axial_pattern, = evidence["axial_hole_patterns"]
    assert axial_pattern["count"] == 2
    assert axial_pattern["view_outer_diameter_mm"] == 80.0
    assert axial_pattern["bolt_circle_diameter_mm"] == 65.0
    assert axial_pattern["measured_bolt_circle_diameter_mm"] == pytest.approx(64.054)
    assert axial_pattern["view_center_px"] == pytest.approx([2136.5, 592.5])
    assert axial_pattern["hole_centers_px"] == [
        [2136.5, 475.5], [2136.5, 712.5]
    ]


def test_monochrome_feature_detection_fails_closed():
    image = Image.open(FIXTURE).convert("L").convert("RGB")
    axial = {
        "overall_mm": 470.0,
        "datum_line": [276.0, 1662.0],
    }

    evidence = localize_turned_features(image, axial, [85, 35, 12, 8])

    assert evidence["status"] == "unresolved"
    assert evidence["keyway_candidates"] == []
