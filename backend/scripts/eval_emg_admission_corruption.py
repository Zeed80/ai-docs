#!/usr/bin/env python3
"""Run deterministic dev corruptions against every implemented generator gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.emg_corruption_regression import load_and_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=BACKEND_ROOT / "tests/fixtures/emg_domain_golden.json",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = load_and_run(args.manifest.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
