from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "build_dxf_raster_corpus.py"
SPEC = importlib.util.spec_from_file_location("build_dxf_raster_corpus", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write_dxf(path: Path, *, unsupported: bool = False) -> None:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 4
    modelspace = document.modelspace()
    modelspace.add_line((0, 0), (20, 0))
    modelspace.add_circle((10, 10), 4)
    modelspace.add_text("Ø8", dxfattribs={"height": 2.5}).set_placement((4, 16))
    if unsupported:
        modelspace.add_ellipse((10, 10), major_axis=(5, 0), ratio=0.5)
    stream = io.StringIO()
    document.write(stream)
    path.write_text(stream.getvalue())


def test_native_dxf_types_are_preserved_and_split_is_source_grouped(
    tmp_path: Path,
) -> None:
    dxf_path = tmp_path / "part.dxf"
    _write_dxf(dxf_path)
    import hashlib

    digest = hashlib.sha256(dxf_path.read_bytes()).hexdigest()
    assets = tmp_path / "assets.jsonl"
    assets.write_text(
        json.dumps(
            {
                "source_id": "qcad_open_library",
                "source_group_id": "qcad:part",
                "profile": "mechanical",
                "relative_path": "part.dxf",
                "output_path": str(dxf_path),
                "license": "CC BY 3.0",
                "sha256": digest,
                "entity_count": 3,
                "split": "holdout",
                "asset_format": "dxf",
            }
        )
        + "\n"
    )

    summary = MODULE.build(assets, tmp_path / "out", eval_variants=2, repo=ROOT)
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "manifest.jsonl").read_text().splitlines()
    ]

    assert summary["accepted_source_groups"] == 1
    assert summary["entity_types"] == {"circle": 1, "segment": 1, "text": 1}
    assert len(rows) == 2
    assert {row["source_group_id"] for row in rows} == {"qcad:part"}
    assert {row["split"] for row in rows} == {"holdout"}
    assert all(row["truth_kind"] == "native_dxf_entities" for row in rows)


def test_unsupported_dxf_is_rejected_instead_of_becoming_partial_truth(
    tmp_path: Path,
) -> None:
    dxf_path = tmp_path / "ellipse.dxf"
    _write_dxf(dxf_path, unsupported=True)
    import hashlib

    assets = tmp_path / "assets.jsonl"
    assets.write_text(
        json.dumps(
            {
                "source_id": "qcad_open_library",
                "source_group_id": "qcad:ellipse",
                "profile": "mechanical",
                "relative_path": "ellipse.dxf",
                "output_path": str(dxf_path),
                "license": "CC BY 3.0",
                "sha256": hashlib.sha256(dxf_path.read_bytes()).hexdigest(),
                "entity_count": 4,
                "split": "train",
                "asset_format": "dxf",
            }
        )
        + "\n"
    )

    summary = MODULE.build(assets, tmp_path / "out", repo=ROOT)

    assert summary["accepted_source_groups"] == 0
    assert summary["rejected_source_groups"] == 1
    assert summary["rejected"][0]["issues"] == ["unsupported:ELLIPSE"]


def test_overlay_text_glyphs_draws_readable_ink_at_position():
    """render_ir_to_png omits text (production gets it from keep_raster), so a
    synthetic corpus must draw its own glyphs or OCR has nothing to read."""
    import numpy as np
    from PIL import Image

    sys.path.insert(0, str(ROOT / "backend"))
    from app.ai.cad_ir.png_render import render_ir_to_png
    from app.ai.cad_ir.schema import CadIR, Point, SourceInfo, TextEntity

    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=200),
        entities=[TextEntity(position=Point(x=60, y=120), text="M20", height=40)],
    )
    base = render_ir_to_png(ir)
    base_ink = int((np.array(Image.open(io.BytesIO(base)).convert("L")) < 128).sum())
    assert base_ink == 0  # nothing drawn yet — text is not stroke geometry

    with_text = MODULE._overlay_text_glyphs(base, ir)
    painted = np.array(Image.open(io.BytesIO(with_text)).convert("L")) < 128
    assert int(painted.sum()) > 0  # glyphs are now on the raster
    # The ink lands around the entity position (baseline-left), not elsewhere.
    ys, xs = np.nonzero(painted)
    assert 55 <= xs.min() <= 120
    assert 75 <= ys.min() and ys.max() <= 130
