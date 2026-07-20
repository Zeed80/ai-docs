from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "tools" / "cad-dataset" / "generate_profile_corpus.py"
BUILD = ROOT / "tools" / "cad-dataset" / "build_dataset.py"


def _generator_module():
    spec = importlib.util.spec_from_file_location("generate_profile_corpus_for_build", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_split_manifest_prevents_synthetic_holdout_from_training(tmp_path: Path) -> None:
    source = tmp_path / "source"
    holdout = tmp_path / "real-holdout"
    packed = tmp_path / "packed"
    (holdout / "ir").mkdir(parents=True)
    (holdout / "clean").mkdir()
    generator = _generator_module()
    generator.generate(
        source,
        count=3,
        seed=11,
        profiles=["mechanical", "construction"],
        variants=1,
        repo=ROOT,
    )
    manifest_path = source / "manifest.jsonl"
    manifest = [json.loads(line) for line in manifest_path.read_text().splitlines()]
    manifest[0]["split"] = "holdout"
    manifest_path.write_text("\n".join(json.dumps(row) for row in manifest) + "\n")

    subprocess.run(
        [
            sys.executable,
            str(BUILD),
            "--synth",
            str(source),
            "--holdout",
            str(holdout),
            "--out",
            str(packed),
            "--split-manifest",
            str(manifest_path),
            "--repo",
            str(ROOT),
        ],
        check=True,
    )
    optimized = [
        json.loads(line)
        for split in ("train", "val")
        for line in (packed / f"{split}.jsonl").read_text().splitlines()
    ]
    assert len(optimized) == len(manifest) - 1
    assert manifest[0]["source_group_id"] not in {row["source_group_id"] for row in optimized}
    assert all(row["profile"] in {"mechanical", "construction"} for row in optimized)
