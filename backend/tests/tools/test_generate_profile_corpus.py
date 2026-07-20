from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "generate_profile_corpus.py"
SPEC = importlib.util.spec_from_file_location("generate_profile_corpus", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_generated_profiles_have_exact_dxf_ir_pairs(tmp_path: Path) -> None:
    summary = MODULE.generate(
        tmp_path,
        count=2,
        seed=7,
        profiles=["mechanical", "construction"],
        variants=1,
        repo=ROOT,
    )
    assert summary["total"] == 4
    assert summary["profiles"] == {"mechanical": 2, "construction": 2}

    rows = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert len(rows) == 4
    assert all(Path(row["image"]).exists() for row in rows)
    assert all(len(row["control_images"]) == 1 for row in rows)
    assert all(Path(row["control_images"][0]).exists() for row in rows)
    assert all(Path(row["dxf"]).exists() for row in rows)
    assert all(Path(row["ir"]).exists() for row in rows)
    assert all(row["kind"] == "open_derived_synthetic" for row in rows)
    assert all(row["source_group_id"].startswith(MODULE.GENERATOR_VERSION) for row in rows)
