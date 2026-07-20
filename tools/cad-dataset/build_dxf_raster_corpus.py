#!/usr/bin/env python3
"""Build source-grouped raster/CadIR pairs from human-authored DXF files.

The DXF entity graph is the target.  A raster is only an observation generated
from that graph, so circles remain circles, dimensions remain dimensions and
text is not replaced by thousands of PDF path fragments.

Only semantically complete files are admitted.  A file containing an entity
that the CadIR importer cannot represent is rejected instead of silently
turning an incomplete import into "ground truth".
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import random
import sys
from collections import Counter


SUPPORTED_DXF_TYPES = {
    "LINE",
    "CIRCLE",
    "ARC",
    "LWPOLYLINE",
    "POLYLINE",
    "TEXT",
    "MTEXT",
    "DIMENSION",
    "HATCH",
    "INSERT",
}


def _source_entity_types(document) -> tuple[Counter, list[str]]:
    """Count recursively expanded entities and report incomplete constructs."""
    counts: Counter = Counter()
    issues: list[str] = []

    def visit(entity, depth: int = 0) -> None:
        kind = entity.dxftype()
        if kind not in SUPPORTED_DXF_TYPES:
            issues.append(f"unsupported:{kind}")
            return
        if kind != "INSERT":
            counts[kind] += 1
            return
        if depth >= 16:
            issues.append("insert_depth")
            return
        try:
            children = list(entity.virtual_entities())
        except Exception:  # noqa: BLE001 - rejection is the desired behavior
            issues.append(f"broken_insert:{getattr(entity.dxf, 'name', '?')}")
            return
        for child in children:
            visit(child, depth + 1)

    for entity in document.modelspace():
        visit(entity)
    return counts, sorted(set(issues))


def _degrade(png: bytes, *, seed: int, clean: bool) -> bytes:
    if clean:
        return png
    import cv2
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    scale = float(rng.uniform(0.65, 1.0))
    if scale < 0.98:
        small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        image = cv2.resize(
            small, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR
        )
    if rng.random() < 0.8:
        image = cv2.GaussianBlur(image, (3, 3), float(rng.uniform(0.2, 1.1)))
    image = np.clip(
        image.astype(np.float32)
        + rng.normal(0, rng.uniform(0.7, 4.0), image.shape),
        0,
        255,
    ).astype(np.uint8)
    if rng.random() < 0.35:
        kernel = np.ones((2, 2), np.uint8)
        image = (
            cv2.erode(image, kernel, iterations=1)
            if rng.random() < 0.5
            else cv2.dilate(image, kernel, iterations=1)
        )
    stream = io.BytesIO()
    Image.fromarray(image).save(
        stream, format="JPEG", quality=int(rng.integers(70, 96))
    )
    output = io.BytesIO()
    Image.open(io.BytesIO(stream.getvalue())).convert("L").save(output, format="PNG")
    return output.getvalue()


def build(
    assets_path: pathlib.Path,
    out: pathlib.Path,
    *,
    source_id: str = "qcad_open_library",
    train_variants: int = 4,
    eval_variants: int = 2,
    long_side: int = 2048,
    min_long_side: int = 1024,
    repo: pathlib.Path | None = None,
) -> dict:
    import ezdxf

    repo = repo or pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "backend"))
    from app.ai.cad_ir.adapters.from_dxf import dxf_to_ir
    from app.ai.cad_ir.png_render import render_ir_to_png
    from app.ai.cad_ir.resize import ensure_min_long_side, fit_ir_to_long_side

    if train_variants < 1 or eval_variants < 1 or long_side < 256:
        raise ValueError("variant counts must be positive and long_side >= 256")
    if not 256 <= min_long_side <= long_side:
        raise ValueError("min_long_side must be in [256, long_side]")
    assets = [
        json.loads(line)
        for line in assets_path.read_text().splitlines()
        if line.strip()
    ]
    assets = [
        row
        for row in assets
        if row.get("source_id") == source_id and row.get("asset_format") == "dxf"
    ]
    for folder in ("ir", "clean", "control"):
        (out / folder).mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    rejected: list[dict] = []
    entity_counts: Counter = Counter()
    for asset in assets:
        path = pathlib.Path(asset["output_path"])
        try:
            document = ezdxf.readfile(path)
            source_counts, issues = _source_entity_types(document)
            if issues:
                rejected.append({"path": str(path), "issues": issues})
                continue
            ir = fit_ir_to_long_side(dxf_to_ir(path.read_bytes()), long_side)
            ir = ensure_min_long_side(ir, min_long_side)
        except Exception as exc:  # noqa: BLE001 - corpus records every rejection
            rejected.append(
                {"path": str(path), "issues": [f"import:{type(exc).__name__}"]}
            )
            continue
        if len(ir.entities) < 3:
            rejected.append({"path": str(path), "issues": ["too_few_entities"]})
            continue

        ir.source.kind = "scan"
        ir.recognizer_used = "human-authored-dxf-ground-truth"
        ir.digitization_status = "exact_candidate"
        exact_png = render_ir_to_png(ir)
        digest = asset["sha256"]
        base = pathlib.Path(asset["relative_path"]).stem
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)
        safe = f"{safe[:80]}_{digest[:12]}"
        ir_path = out / "ir" / f"{safe}.json"
        control_path = out / "control" / f"{safe}.png"
        ir_path.write_text(ir.model_dump_json())
        control_path.write_bytes(exact_png)
        entity_counts.update(entity.type for entity in ir.entities)

        split = asset["split"]
        variants = train_variants if split == "train" else eval_variants
        for variant in range(variants):
            stem = f"{safe}__v{variant:02d}"
            image_path = out / "clean" / f"{stem}.png"
            seed = int(hashlib.sha256(f"{digest}:{variant}".encode()).hexdigest()[:8], 16)
            image_path.write_bytes(
                _degrade(exact_png, seed=seed, clean=variant == 0)
            )
            rows.append(
                {
                    "id": stem,
                    "profile": asset["profile"],
                    "kind": "human_authored_dxf_exact_pair",
                    "truth_kind": "native_dxf_entities",
                    "source_id": source_id,
                    "source_group_id": asset["source_group_id"],
                    "split": split,
                    "image": str(image_path.resolve()),
                    "control_images": [str(control_path.resolve())],
                    "ir": str(ir_path.resolve()),
                    "source_dxf": str(path.resolve()),
                    "source_sha256": digest,
                    "license": asset["license"],
                    "attribution": asset.get("attribution"),
                    "variant": variant,
                    "source_entity_counts": dict(source_counts),
                    "ir_entity_count": len(ir.entities),
                }
            )

    random.Random(29).shuffle(rows)
    with (out / "manifest.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "source_id": source_id,
        "assets_seen": len(assets),
        "accepted_source_groups": len({row["source_group_id"] for row in rows}),
        "rejected_source_groups": len(rejected),
        "pairs": len(rows),
        "splits": {
            split: sum(row["split"] == split for row in rows)
            for split in ("train", "val", "holdout")
        },
        "entity_types": dict(sorted(entity_counts.items())),
        "long_side": long_side,
        "semantic_ground_truth": True,
        "rejected": rejected,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--assets",
        type=pathlib.Path,
        default=pathlib.Path("cad-dataset-out/open-sources/assets.jsonl"),
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("cad-dataset-out/web-dxf-corpus"),
    )
    parser.add_argument("--source-id", default="qcad_open_library")
    parser.add_argument("--train-variants", type=int, default=4)
    parser.add_argument("--eval-variants", type=int, default=2)
    parser.add_argument("--long-side", type=int, default=2048)
    parser.add_argument("--min-long-side", type=int, default=1024)
    args = parser.parse_args()
    summary = build(
        args.assets,
        args.out,
        source_id=args.source_id,
        train_variants=args.train_variants,
        eval_variants=args.eval_variants,
        long_side=args.long_side,
        min_long_side=args.min_long_side,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["accepted_source_groups"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
