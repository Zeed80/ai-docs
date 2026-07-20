from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "build_web_step_corpus.py"
SPEC = importlib.util.spec_from_file_location("build_web_step_corpus", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_builds_degraded_source_with_exact_ir_and_group_split(tmp_path: Path) -> None:
    source = tmp_path / "part.step"
    source.write_text("ISO-10303-21;")
    assets = tmp_path / "assets.jsonl"
    assets.write_text(
        json.dumps(
            {
                "asset_format": "step",
                "output_path": str(source),
                "source_group_id": "freecad:part",
                "profile": "mechanical",
                "split": "train",
                "sha256": "1" * 64,
                "license": "CC BY 3.0",
                "attribution": "test",
            }
        )
        + "\n"
    )
    projections = tmp_path / "projections"
    projections.mkdir()
    (projections / "part.json").write_text(
        json.dumps(
            {
                "views": {
                    "front": [
                        {"type": "segment", "p1": [0, 0], "p2": [20, 0]},
                        {"type": "segment", "p1": [20, 0], "p2": [20, 10]},
                        {"type": "segment", "p1": [20, 10], "p2": [0, 10]},
                        {"type": "segment", "p1": [0, 10], "p2": [0, 0]},
                        {"type": "circle", "center": [10, 5], "radius": 2},
                    ]
                }
            }
        )
    )

    summary = MODULE.build(assets, projections, tmp_path / "out", repo=ROOT)

    assert summary["sheets"] == 1
    row = json.loads((tmp_path / "out" / "manifest.jsonl").read_text())
    assert row["source_group_id"] == "freecad:part"
    assert row["split"] == "train"
    assert Path(row["image"]).read_bytes() != Path(row["control_images"][0]).read_bytes()
