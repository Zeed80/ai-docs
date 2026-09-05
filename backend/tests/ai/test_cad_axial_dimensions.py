from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_detal_126_axial_lines_are_localized_against_both_datums():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"
    known = [
        0.008,
        0.8,
        1,
        1.6,
        3,
        3.2,
        4,
        5,
        6,
        7.24,
        8,
        12,
        14,
        15,
        16,
        17,
        18,
        20,
        25,
        26,
        27,
        35,
        50,
        78,
        85,
        99,
        150,
        240,
        270,
        470,
    ]

    result = localize_axial_dimensions(Image.open(source).convert("RGB"), known)

    assert result["status"] == "ok"
    assert result["overall_mm"] == 470
    by_value = {item["value_mm"]: item for item in result["observations"]}
    assert by_value[150]["relation"] == "from_left_datum"
    assert by_value[150]["station_from_left_mm"] == 150
    assert by_value[150]["ocr_value_mm"] == 50
    assert by_value[150]["ocr_corrected"] is True
    assert by_value[78]["station_from_left_mm"] == 78
    assert by_value[240]["station_from_left_mm"] == 240
    assert by_value[270]["relation"] == "from_right_datum"
    assert by_value[270]["station_from_left_mm"] == 200
    assert by_value[99]["station_from_left_mm"] == 371
    assert by_value[470]["relation"] == "overall"
    assert all(item["label_bbox"] and item["dimension_line"] for item in by_value.values())


def test_no_dimension_lines_fail_closed():
    image = Image.new("RGB", (400, 200), "white")

    result = localize_axial_dimensions(image, [50, 100])

    assert result["status"] == "unresolved"
    assert result["observations"] == []
    assert result["blockers"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_detal_126_dimension_lines_recover_values_missing_from_vlm_callouts():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"

    result = localize_axial_dimensions(
        Image.open(source).convert("RGB"),
        # Simulate the failed live pass: only the overall survived callout VLM.
        [470],
    )

    by_value = {item["value_mm"]: item for item in result["observations"]}
    assert {78, 99, 150, 240, 270, 470} <= set(by_value)
    assert by_value[150]["ocr_value_mm"] == 50
    assert by_value[150]["value_source"] == "dimension_span_ocr_correction"
    assert by_value[78]["value_source"] == "dimension_line_ocr"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_direct_dimension_line_ocr_wins_over_nearby_unrelated_callout():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"

    result = localize_axial_dimensions(
        Image.open(source).convert("RGB"),
        [36, 470],
    )

    local_values = {
        item["value_mm"]: item
        for item in result["observations"]
        if item["relation"] == "local_interval"
    }
    assert 35.0 in local_values
    assert local_values[35.0]["raw_text"] == "35"
    assert local_values[35.0]["value_source"] == "dimension_line_ocr"
