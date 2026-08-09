#!/usr/bin/env python3
"""Validate exported EngineeringModelGraph and GraphPatch JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from app.domain.engineering_model_graph import EngineeringModelGraph, GraphPatch


def validate(path: Path, *, allow_unsealed: bool) -> str:
    payload = json.loads(path.read_text())
    version = payload.get("schema_version")
    if version == "emg/1.0":
        graph = EngineeringModelGraph.model_validate(payload)
        calculated = graph.calculated_sha256()
        if not allow_unsealed and graph.canonical_sha256 != calculated:
            raise ValueError(
                f"canonical_sha256 mismatch: stored={graph.canonical_sha256!r} calculated={calculated}"
            )
        return f"EMG {graph.graph_id} r{graph.revision} {calculated}"
    if version == "emg-patch/1.0":
        patch = GraphPatch.model_validate(payload)
        return f"GraphPatch {patch.patch_id} base-r{patch.base_revision}"
    raise ValueError(f"unsupported schema_version: {version!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--allow-unsealed", action="store_true")
    args = parser.parse_args()
    failed = False
    for path in args.paths:
        try:
            print(f"OK {path}: {validate(path, allow_unsealed=args.allow_unsealed)}")
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            failed = True
            print(f"ERROR {path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
