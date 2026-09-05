#!/usr/bin/env python3
"""Run the four-domain EngineeringModelGraph golden regression."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.emg_regression import run_emg_regression


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="tests/fixtures/emg_domain_golden.json")
    parser.add_argument("--out")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    report = run_emg_regression(manifest, fixture_root=manifest_path.parent)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(rendered + "\n")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
