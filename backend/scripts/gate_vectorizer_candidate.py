#!/usr/bin/env python3
"""Fail-closed promotion gate for a scan-to-DXF recognizer candidate.

The gate intentionally ignores the legacy pixel coverage score. A candidate
can be promoted only on real, held-out drawings and only when entity-level
geometry/text matching, exact-sheet rate, DXF reopening and false-exact
behavior all satisfy the release contract.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def evaluate_candidate(
    baseline: dict,
    candidate: dict,
    *,
    min_precision: float,
    min_recall: float,
    min_exact_sheet_rate: float,
) -> list[str]:
    old = baseline.get("summary", {}).get("dwg", {})
    new = candidate.get("summary", {}).get("dwg", {})
    failures: list[str] = []

    required = (
        "entity_precision",
        "entity_recall",
        "entity_f1",
        "exact_sheet_rate",
        "false_exact_rate",
        "dxf_reopen_rate",
        "entity_evaluated_files",
    )
    missing = [key for key in required if key not in new]
    if missing:
        return [f"candidate is missing held-out metrics: {', '.join(missing)}"]
    if int(new["entity_evaluated_files"]) <= 0:
        failures.append("no held-out drawings were evaluated")
    if float(new["false_exact_rate"]) != 0.0:
        failures.append(
            f"false_exact_rate must be 0, got {new['false_exact_rate']}"
        )
    if float(new["dxf_reopen_rate"]) != 1.0:
        failures.append(
            f"dxf_reopen_rate must be 1, got {new['dxf_reopen_rate']}"
        )
    thresholds = {
        "entity_precision": min_precision,
        "entity_recall": min_recall,
        "exact_sheet_rate": min_exact_sheet_rate,
    }
    for metric, minimum in thresholds.items():
        if float(new[metric]) < minimum:
            failures.append(f"{metric} {new[metric]} is below {minimum}")

    # A replacement may not trade one semantic metric for another. Tiny
    # numeric drift is tolerated, but every primary metric must be at least
    # as good as the current production baseline.
    for metric in (
        "entity_precision",
        "entity_recall",
        "entity_f1",
        "exact_sheet_rate",
        "dxf_reopen_rate",
    ):
        if metric in old and float(new[metric]) + 1e-6 < float(old[metric]):
            failures.append(
                f"{metric} regressed: {old[metric]} -> {new[metric]}"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=pathlib.Path)
    parser.add_argument("--candidate", required=True, type=pathlib.Path)
    parser.add_argument("--min-precision", type=float, default=0.995)
    parser.add_argument("--min-recall", type=float, default=0.995)
    parser.add_argument("--min-exact-sheet-rate", type=float, default=0.99)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text())
    candidate = json.loads(args.candidate.read_text())
    failures = evaluate_candidate(
        baseline,
        candidate,
        min_precision=args.min_precision,
        min_recall=args.min_recall,
        min_exact_sheet_rate=args.min_exact_sheet_rate,
    )
    if failures:
        print("VECTOR MODEL PROMOTION REFUSED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("VECTOR MODEL PROMOTION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
