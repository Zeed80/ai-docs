#!/usr/bin/env python3
"""Run the exact mechanical EMG dev case through the live CAD kernel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.cad_solid import feature_tree_from_spec
from app.ai.emg_live_stage import mechanical_live_stage_report
from scripts.live_emg_stack_regression import _run_mechanical


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec",
        type=Path,
        default=BACKEND_ROOT / "tests/fixtures/detal_126_reference_spec_v2.json",
    )
    parser.add_argument("--case-id", default="mechanical-detal-126-v2")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    candidate = feature_tree_from_spec(json.loads(args.spec.read_text()))
    if candidate is None:
        raise ValueError("mechanical spec did not compile to a feature tree")
    runtime = _run_mechanical(args.case_id, candidate.model_dump(mode="json"))
    report = mechanical_live_stage_report(args.case_id, runtime)
    report["runtime"] = runtime
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
