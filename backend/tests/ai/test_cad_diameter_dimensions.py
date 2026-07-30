from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions
from app.ai.cad_recognize.diameter_dimensions import localize_diameter_dimensions
from app.ai.cad_recognize.diameter_dimensions import outer_sections_from_diameter_evidence
from app.ai.cad_recognize.diameter_dimensions import bore_sections_from_diameter_evidence


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_detal_126_diameters_are_classified_against_the_main_profile():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"
    image = Image.open(source).convert("RGB")
    linear = [14, 15, 17, 18, 20, 25, 26, 35, 50, 78, 85, 99, 150, 240, 270, 470]
    diameters = [9, 10, 14, 24, 26, 44, 50, 51, 55, 56, 56.55, 65, 70, 71, 72, 75, 79, 80, 98, 102]

    axial = localize_axial_dimensions(image, linear)
    result = localize_diameter_dimensions(image, diameters, axial, linear)

    assert result["status"] == "ok"
    assert result["profile_center_y_px"] == 593
    roles = {(item["value_mm"], item["role"]) for item in result["observations"]}
    assert {(102, "outer"), (80, "outer"), (72, "outer")} <= roles
    assert {(56.55, "bore"), (51, "bore"), (44, "bore"), (56, "bore"), (55, "bore")} <= roles
    callout = next(
        item for item in result["observations"]
        if item["value_mm"] == 44 and item["role"] == "bore"
        and item["source"] == "vertical_callout_and_vector_contour"
    )
    assert callout["label_bbox"]
    assert callout["profile_measurement_line"]
    assert callout["source"] == "vertical_callout_and_vector_contour"
    transitions = {
        item["station_from_left_mm"] for item in result["outer_transition_stations"]
    }
    assert {14, 470} <= transitions
    sections = outer_sections_from_diameter_evidence(result)
    assert [
        (item["diameter_mm"], item["length_mm"]) for item in sections
    ] == [(102.0, 14.0), (80.0, 357.0), (72.0, 99.0)]
    assert all(item["evidence"][0]["bbox"] for item in sections)
    assert all("vector outer contour" in item["evidence"][0]["raw_text"] for item in sections)
    bore = bore_sections_from_diameter_evidence(result)
    assert [(item["diameter_mm"], item["length_mm"]) for item in bore] == [
        (56.55, 78.0),
        (51.0, 72.0),
        (44.0, 50.0),
        (51.0, 152.0),
        (50.0, 43.0),
        (56.0, 50.0),
        (55.0, 25.0),
    ]
    assert bore[0]["taper"]["ratio"] == "7:24"
    assert all(item["evidence"][0]["bbox"] for item in bore)
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec

    validated = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": sections,
            "bore": bore,
        }
    })
    assert validated.main_view.bore[0].taper is not None


def test_monochrome_sheet_is_not_assigned_diameter_roles():
    image = Image.new("RGB", (600, 400), "white")
    axial = {"datum_line": [100, 500], "mm_per_px": 0.5}

    result = localize_diameter_dimensions(image, [20, 40], axial)

    assert result["status"] == "unresolved"
    assert result["observations"] == []
    assert "не разделены" in result["blockers"][0]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_profile_recovers_nominals_even_when_upstream_callout_list_missed_them():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"
    image = Image.open(source).convert("RGB")
    linear = [15, 17, 18, 20, 25, 26, 35, 50, 78, 85, 99, 150, 240, 270, 470]
    axial = localize_axial_dimensions(image, linear)

    # This is the incomplete list observed on the production replay: the VLM
    # omitted Ø102 and Ø80. Spatial OCR + contour evidence must not depend on
    # that failed read in order to recover them.
    result = localize_diameter_dimensions(
        image, [9, 14, 26, 44, 51, 55, 56, 65, 71, 72, 75], axial, linear
    )

    outer = {
        item["value_mm"] for item in result["observations"]
        if item["role"] == "outer" and item["confidence"] >= 0.6
    }
    bore = {
        item["value_mm"] for item in result["observations"]
        if item["role"] == "bore" and item["confidence"] >= 0.6
    }
    assert {102, 80, 72} <= outer
    assert {56.55, 51, 44, 56, 55} <= bore
