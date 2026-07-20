from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = _load(
    "generate_profile_corpus_for_tiles",
    ROOT / "tools" / "cad-dataset" / "generate_profile_corpus.py",
)
TILER = _load(
    "tile_ir_dataset",
    ROOT / "tools" / "cad-dataset" / "tile_ir_dataset.py",
)


def test_tiles_stay_with_source_group_and_fit_command_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "tiles"
    GENERATOR.generate(
        source,
        count=1,
        seed=11,
        profiles=["mechanical", "construction"],
        variants=0,
        repo=ROOT,
    )

    summary = TILER.tile_corpus(
        source,
        output,
        tile_size=640,
        overlap=160,
        max_commands=180,
        repo=ROOT,
    )

    rows = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text().splitlines()
    ]
    assert summary["tiles"] == len(rows)
    assert rows
    assert summary["max_commands"] <= 180
    assert all(Path(row["image"]).exists() for row in rows)
    assert all(Path(row["ir"]).exists() for row in rows)
    assert all(row["kind"] == "exact_geometry_tile" for row in rows)

    split_by_group: dict[str, set[str]] = {}
    for row in rows:
        split_by_group.setdefault(row["source_group_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in split_by_group.values())
