#!/usr/bin/env python3
"""Generate the public EMG v1 JSON Schemas from authoritative Pydantic models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.engineering_model_graph import EngineeringModelGraph, GraphPatch


SCHEMAS = {
    "engineering-model-graph-1.0.schema.json": (
        EngineeringModelGraph,
        "https://schemas.ptsai.local/emg/engineering-model-graph-1.0.schema.json",
    ),
    "graph-patch-1.0.schema.json": (
        GraphPatch,
        "https://schemas.ptsai.local/emg/graph-patch-1.0.schema.json",
    ),
}


def rendered_schemas() -> dict[str, str]:
    result = {}
    for filename, (model, schema_id) in SCHEMAS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = schema_id
        result[filename] = json.dumps(
            schema, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "schemas")
    args = parser.parse_args()
    expected = rendered_schemas()
    stale = []
    for filename, content in expected.items():
        path = args.output_dir / filename
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            print(path)
    if stale:
        print("Stale EMG schema files:\n" + "\n".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
