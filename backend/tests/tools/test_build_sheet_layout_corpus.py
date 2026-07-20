import importlib.util
import json
from pathlib import Path

from PIL import Image

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "cad-dataset"
    / "build_sheet_layout_corpus.py"
)
SPEC = importlib.util.spec_from_file_location("build_sheet_layout_corpus", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_layout_corpus_preserves_source_group_split(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    rows = []
    for split, group in (("train", "part-a"), ("holdout", "part-b")):
        for view in ("front", "top", "side"):
            path = source / f"{group}-{view}.png"
            Image.new("L", (64, 64), 255).save(path)
            rows.append(
                {
                    "id": f"{group}__{view}",
                    "source_group_id": group,
                    "split": split,
                    "profile": "mechanical",
                    "view": view,
                    "image": str(path),
                }
            )
    (source / "manifest.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    summary = module.build(source, tmp_path / "out", train_variants=2, eval_variants=1)
    output = [
        json.loads(line)
        for line in (tmp_path / "out" / "manifest.jsonl").read_text().splitlines()
    ]

    assert summary["sheets"] == 3
    assert {row["split"] for row in output if row["source_group_id"] == "part-a"} == {
        "train"
    }
    assert {row["split"] for row in output if row["source_group_id"] == "part-b"} == {
        "holdout"
    }
    assert all(len(row["targets"]) == 3 for row in output)
    assert {target["kind"] for row in output for target in row["targets"]} == {"view"}
    assert {target["source_view"] for row in output for target in row["targets"]} == {
        "front",
        "top",
        "side",
    }
