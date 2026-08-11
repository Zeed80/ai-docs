#!/usr/bin/env python3
"""CLI wrapper around app.ai.ifc_reader.project_ifc.

Ф5.1: the actual projection logic moved to app/ai/ifc_reader.py so it is
importable from a service (FastAPI endpoint / Celery task), not just this
offline script. This file is now a thin CLI shell.
"""

from __future__ import annotations

import json
import pathlib
import sys

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.ifc_reader import project_ifc  # noqa: E402


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    payload = project_ifc(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False))
    print(json.dumps({
        "schema": payload["ifc_schema"],
        "elements": len(payload["elements"]),
        "geometry_failures": len(payload["geometry_failures"]),
        "view_segments": {key: len(value) for key, value in payload["views"].items()},
    }, ensure_ascii=False))
    return 0 if payload["elements"] else 1


if __name__ == "__main__":
    sys.exit(main())
