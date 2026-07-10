#!/usr/bin/env python3
"""KPI eval: does the pipeline ever silently miss a critical corruption?

Per the external-critique correction (2026-07-10), the headline metric for
this system is not "% of pretty models" — it's:

    the share of critical errors the system does NOT surface to the user.

This script targets the one place the pipeline can actually *know* something
changed: a diffusion-derived source vs its own original photo (Ф2.3,
``app/ai/pixel_provenance.py``). It takes real drawings from ``test-drawings/``,
injects three classes of critical, ground-truth-tracked corruption directly
into ink-space (as if a diffusion "cleanup" pass had produced this result from
the original photo), then checks whether ``diffusion_change_masks`` flags the
corrupted region — the same signal ``tasks/cad_trace.py`` turns into
``DIFFUSION_ADDED_INK`` / ``DIFFUSION_REMOVED_INK`` issues and a review item.

Corruption classes:
    digit_change  — an existing text/dimension region is erased and redrawn
                     with different digits (simulates a diffusion pass
                     misreading "40" as "48").
    added_line    — a stroke is drawn in empty space that never existed on
                     the original (hallucinated geometry).
    removed_stroke — an existing stroke component is erased entirely
                     (silently dropped geometry).

Detection criterion mirrors production's own ``entities_in_mask``: the
fraction of the corruption's OWN ink pixels (not its bounding-box area — a
thin diagonal stroke's bbox is mostly empty even under perfect detection)
that lie inside the added/removed mask must reach ``_HIT_FRACTION``. A miss
is a corruption class instance for which the pipeline's provenance layer
produced no signal at all — exactly the failure this KPI exists to drive to
zero.

Usage (host):
    python3 backend/scripts/eval_critical_corruption.py \
        --dir test-drawings --out test-results/eval_critical_corruption.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Same order of magnitude as production's entities_in_mask overlap gate
# (_ENTITY_CHANGED_OVERLAP=0.35) — a bit more lenient because this metric
# additionally absorbs ECC alignment jitter the entity-level check doesn't.
_HIT_FRACTION = 0.30
_DIGIT_TEXT = "48"  # deterministic stand-in "misread" digit
_LINE_LENGTH_PX = 90


def _prepare(image_bytes: bytes):
    from app.ai.drawing_cleanup import enhance_source_for_diffusion
    from app.tasks.cad_trace import _binarize

    enhanced = enhance_source_for_diffusion(image_bytes)
    ink, w, h = _binarize(enhanced)
    return ink, w, h


def _own_overlap(mask, own_ink) -> float:
    """Fraction of the ground-truth stroke's OWN ink pixels that fall inside
    ``mask`` — the same "does the detector actually cover the real geometry"
    measure production uses (``entities_in_mask``), not a bbox-area fraction
    that a thin diagonal stroke could never satisfy even under perfect
    detection."""
    import numpy as np

    own = np.asarray(own_ink) > 0
    total = int(own.sum())
    if total == 0:
        return 0.0
    return float((np.asarray(mask) & own).sum()) / total


def _corrupt_digit_change(ink, image_bytes: bytes):
    """Erase an existing text region and redraw a different digit string.

    Returns (corrupted_ink, old_text_mask, new_digit_mask) — the ground
    truth is the ACTUAL ink pixels removed/added, not the region's bbox.
    """
    import cv2
    import numpy as np

    from app.ai.text_preserve import detect_text_regions

    regions = [r for r in detect_text_regions(image_bytes) if r.w >= 20 and r.h >= 10]
    if not regions:
        return None
    region = max(regions, key=lambda r: r.w * r.h)
    x0, y0, x1, y1 = region.x, region.y, region.x + region.w, region.y + region.h

    old_text_mask = np.zeros_like(ink, dtype=bool)
    old_text_mask[y0:y1, x0:x1] = ink[y0:y1, x0:x1] > 0

    corrupted = ink.copy()
    corrupted[y0:y1, x0:x1] = 0
    new_digit_canvas = np.zeros_like(ink)
    cv2.putText(
        new_digit_canvas, _DIGIT_TEXT, (region.x, region.y + region.h - 2),
        cv2.FONT_HERSHEY_SIMPLEX, max(0.4, region.h / 30), 255, max(1, region.h // 12),
    )
    corrupted = cv2.bitwise_or(corrupted, new_digit_canvas)
    new_digit_mask = new_digit_canvas > 0
    return corrupted, old_text_mask, new_digit_mask


def _corrupt_added_line(ink, w: int, h: int):
    """Draw a stroke in the emptiest region of the sheet — pure hallucination.

    Returns (corrupted_ink, own_line_mask).
    """
    import cv2
    import numpy as np

    dist = cv2.distanceTransform((ink == 0).astype("uint8"), cv2.DIST_L2, 5)
    _min, _max, _minloc, maxloc = cv2.minMaxLoc(dist)
    cx, cy = maxloc
    half = _LINE_LENGTH_PX // 2
    x0, x1 = max(0, cx - half), min(w - 1, cx + half)
    line_canvas = np.zeros_like(ink)
    cv2.line(line_canvas, (x0, cy), (x1, cy), 255, 3)
    corrupted = cv2.bitwise_or(ink, line_canvas)
    return corrupted, line_canvas > 0


def _corrupt_removed_stroke(ink, exclusion_boxes: list[tuple[int, int, int, int]]):
    """Erase one existing mid-size stroke component entirely.

    Returns (corrupted_ink, own_removed_mask) — the actual erased pixels.
    """
    import cv2

    def _in_exclusion(x, y, w, h) -> bool:
        for ex0, ey0, ex1, ey1 in exclusion_boxes:
            if x >= ex0 and y >= ey0 and x + w <= ex1 and y + h <= ey1:
                return True
        return False

    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    candidates = [
        (i, *stats[i][:4])
        for i in range(1, n)
        if 150 <= stats[i][4] <= 8000 and not _in_exclusion(*stats[i][:4])
    ]
    if not candidates:
        return None
    label = max(candidates, key=lambda c: c[3] * c[4])[0]
    own_mask = labels == label
    corrupted = ink.copy()
    corrupted[own_mask] = 0
    return corrupted, own_mask


def evaluate_file(path: pathlib.Path) -> dict:
    from app.ai.pixel_provenance import diffusion_change_masks
    from app.ai.text_preserve import detect_text_regions

    original_bytes = path.read_bytes()
    ink, w, h = _prepare(original_bytes)
    text_boxes = [
        (r.x, r.y, r.x + r.w, r.y + r.h) for r in detect_text_regions(original_bytes)
    ]

    results: dict[str, dict] = {}

    digit = _corrupt_digit_change(ink, original_bytes)
    if digit is not None:
        corrupted_ink, old_text_mask, new_digit_mask = digit
        masks = diffusion_change_masks(corrupted_ink, original_bytes)
        added_hit = removed_hit = 0.0
        if masks is not None:
            added, removed = masks
            added_hit = _own_overlap(added, new_digit_mask)
            removed_hit = _own_overlap(removed, old_text_mask)
        caught = max(added_hit, removed_hit) >= _HIT_FRACTION
        results["digit_change"] = {
            "caught": caught, "added_hit": round(added_hit, 3), "removed_hit": round(removed_hit, 3),
        }

    corrupted_ink, own_line_mask = _corrupt_added_line(ink, w, h)
    masks = diffusion_change_masks(corrupted_ink, original_bytes)
    added_hit = _own_overlap(masks[0], own_line_mask) if masks is not None else 0.0
    results["added_line"] = {"caught": added_hit >= _HIT_FRACTION, "added_hit": round(added_hit, 3)}

    removed = _corrupt_removed_stroke(ink, text_boxes)
    if removed is not None:
        corrupted_ink, own_removed_mask = removed
        masks = diffusion_change_masks(corrupted_ink, original_bytes)
        removed_hit = _own_overlap(masks[1], own_removed_mask) if masks is not None else 0.0
        results["removed_stroke"] = {
            "caught": removed_hit >= _HIT_FRACTION, "removed_hit": round(removed_hit, 3),
        }

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="test-drawings")
    parser.add_argument("--out", default="test-results/eval_critical_corruption.json")
    args = parser.parse_args()

    root = pathlib.Path(args.dir)
    files = sorted([*root.glob("*.png"), *root.glob("*.jpg"), *root.glob("*.jpeg")])

    per_file: dict[str, dict] = {}
    totals: dict[str, list[bool]] = {"digit_change": [], "added_line": [], "removed_stroke": []}
    for path in files:
        print(f"[eval] {path.name}")
        try:
            result = evaluate_file(path)
        except Exception as exc:  # noqa: BLE001 — record and continue
            print(f"  ERROR: {exc}")
            per_file[path.name] = {"error": str(exc)}
            continue
        per_file[path.name] = result
        for kind, data in result.items():
            totals[kind].append(bool(data["caught"]))
            print(f"  {kind}: {'caught' if data['caught'] else 'MISSED'}")

    summary = {}
    total_instances = 0
    total_missed = 0
    for kind, caught_list in totals.items():
        n = len(caught_list)
        missed = sum(1 for c in caught_list if not c)
        total_instances += n
        total_missed += missed
        summary[kind] = {
            "instances": n,
            "caught": n - missed,
            "missed": missed,
            "missed_rate": round(missed / n, 3) if n else None,
        }
    summary["overall"] = {
        "instances": total_instances,
        "missed": total_missed,
        "missed_critical_rate": round(total_missed / total_instances, 4) if total_instances else None,
    }

    out = {"per_file": per_file, "summary": summary}
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if total_missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
