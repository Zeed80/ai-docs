from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_nist_pmi_locator import summarize


def test_summary_is_explicitly_diagnostic_without_bbox_truth():
    report = summarize([
        {
            "reference_record_count": 3,
            "geometric_tolerance_reference_record_count": 2,
            "detected_region_count": 0,
        },
        {
            "reference_record_count": 4,
            "geometric_tolerance_reference_record_count": 3,
            "detected_region_count": 6,
        },
    ])

    assert report["pages"] == 2
    assert report["reference_records"] == 7
    assert report["geometric_tolerance_reference_records"] == 5
    assert report["detected_regions"] == 6
    assert report["pages_with_zero_regions"] == 1
    assert report["regions_per_page"] == {"minimum": 0, "median": 3.0, "maximum": 6}
    assert report["bbox_truth_available"] is False
    assert report["promotion_eligible"] is False
    assert "recall" not in report
    assert "precision" not in report
