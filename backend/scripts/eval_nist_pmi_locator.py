#!/usr/bin/env python3
"""Diagnose the deterministic PMI-frame locator on truth-linked NIST pages.

The official NIST workbook identifies semantic records and drawing pages, but
does not contain exact frame bounding boxes.  Therefore this report deliberately
does not call region counts recall, coverage, or precision.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_nist_pmi_reader import load_jsonl, select_pages  # noqa: E402

from app.ai.cad_recognize.spec_fragments import (  # noqa: E402
    _detect_pmi_frame_regions,
    _main_view_crop_box,
    _overview,
)

GEOMETRIC_TOLERANCE_CATEGORY = "Geometric Tolerances"


def summarize(page_results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [int(page["detected_region_count"]) for page in page_results]
    return {
        "contract": "nist-pmi-frame-locator-diagnostic-v1",
        "pages": len(page_results),
        "reference_records": sum(int(page["reference_record_count"]) for page in page_results),
        "geometric_tolerance_reference_records": sum(
            int(page["geometric_tolerance_reference_record_count"]) for page in page_results
        ),
        "detected_regions": sum(counts),
        "pages_with_zero_regions": sum(count == 0 for count in counts),
        "regions_per_page": {
            "minimum": min(counts, default=0),
            "median": statistics.median(counts) if counts else 0,
            "maximum": max(counts, default=0),
        },
        "bbox_truth_available": False,
        "promotion_eligible": False,
        "interpretation": (
            "Region counts are operational diagnostics only. The official truth "
            "does not provide exact frame bounding boxes, so recall, precision, "
            "false-region rate, and semantic attachment cannot be calculated."
        ),
        "blockers": [
            "missing_exact_frame_bbox_truth",
            "missing_region_to_semantic_record_truth",
        ],
        "page_results": page_results,
    }


def evaluate_page(image_path: pathlib.Path, truth: list[dict[str, Any]]) -> dict[str, Any]:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    source_box = _main_view_crop_box(image)
    locator_view = _overview(image.crop(source_box), side=2200)
    frames = _detect_pmi_frame_regions(locator_view).get("frames") or []
    return {
        "image": image_path.name,
        "reference_record_count": len(truth),
        "geometric_tolerance_reference_record_count": sum(
            record.get("category") == GEOMETRIC_TOLERANCE_CATEGORY for record in truth
        ),
        "source_crop_box_pixels": list(source_box),
        "locator_image_size": list(locator_view.size),
        "detected_region_count": len(frames),
        "detected_regions": frames,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", type=pathlib.Path, required=True)
    parser.add_argument("--images-root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    pages = select_pages(load_jsonl(args.truth), args.images_root, args.limit)
    if not pages:
        raise ValueError("no NIST PMI pages with semantic truth were found")
    results = []
    for image_path, truth in pages:
        print(f"→ {image_path.name}", flush=True)
        results.append(evaluate_page(image_path, truth))
    report = summarize(results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "page_results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"NIST PMI locator evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
