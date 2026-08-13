#!/usr/bin/env python3
"""Aggregate dev-only CAD/BIM pipeline observations with macro class gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.class_balanced_regression import evaluate_class_balanced_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--safety-report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--require-promotion", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.manifest.read_text())
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    safety_report = (
        json.loads(args.safety_report.read_text()) if args.safety_report else None
    )
    report = evaluate_class_balanced_manifest(
        payload, baseline=baseline, safety_report=safety_report
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)
    if not report["accepted"]:
        return 1
    if args.require_promotion and not report["promotion_eligible"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
