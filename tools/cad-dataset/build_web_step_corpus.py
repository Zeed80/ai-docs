#!/usr/bin/env python3
"""Build exact raster/CadIR pairs from web-acquired STEP projections."""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import random
import sys


def _degrade(png: bytes, seed: int) -> bytes:
    import cv2
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    sigma = float(rng.uniform(0.25, 1.0))
    image = cv2.GaussianBlur(image, (3, 3), sigma)
    noise = rng.normal(0, rng.uniform(1.5, 5.0), image.shape)
    image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    if rng.random() < 0.5:
        image = cv2.erode(image, np.ones((2, 2), np.uint8), iterations=1)
    pil = Image.fromarray(image)
    stream = io.BytesIO()
    pil.save(stream, format="JPEG", quality=int(rng.integers(72, 94)))
    jpeg = Image.open(io.BytesIO(stream.getvalue())).convert("L")
    output = io.BytesIO()
    jpeg.save(output, format="PNG")
    return output.getvalue()


def build(
    assets_path: pathlib.Path,
    projections: pathlib.Path,
    out: pathlib.Path,
    *,
    repo: pathlib.Path | None = None,
) -> dict:
    repo = repo or pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "backend"))

    from app.ai.cad_ir.png_render import render_ir_to_png
    from app.ai.cad_ir.schema import CadIR, Circle, Point, Segment, SourceInfo

    assets = {
        pathlib.Path(row["output_path"]).stem: row
        for row in (
            json.loads(line)
            for line in assets_path.read_text().splitlines()
            if line.strip()
        )
        if row.get("asset_format") == "step"
    }
    for folder in ("ir", "clean", "control"):
        (out / folder).mkdir(parents=True, exist_ok=True)

    rows = []
    rejected = []
    for projection_path in sorted(projections.glob("*.json")):
        if projection_path.name == "summary.json":
            continue
        asset = assets.get(projection_path.stem)
        if asset is None:
            continue
        payload = json.loads(projection_path.read_text())
        for view_name, primitives in payload["views"].items():
            coordinates = []
            for primitive in primitives:
                if primitive["type"] == "segment":
                    coordinates.extend((primitive["p1"], primitive["p2"]))
                else:
                    x, y = primitive["center"]
                    r = primitive["radius"]
                    coordinates.extend(((x - r, y - r), (x + r, y + r)))
            if not coordinates:
                continue
            min_x = min(point[0] for point in coordinates)
            max_x = max(point[0] for point in coordinates)
            min_y = min(point[1] for point in coordinates)
            max_y = max(point[1] for point in coordinates)
            span_x, span_y = max_x - min_x, max_y - min_y
            if min(span_x, span_y) <= 1e-6:
                rejected.append(f"{projection_path.stem}:{view_name}:degenerate")
                continue
            width = height = 1024
            scale = min(900 / span_x, 900 / span_y)
            offset_x = (width - span_x * scale) / 2
            offset_y = (height - span_y * scale) / 2

            def point(raw):
                return Point(
                    x=offset_x + (raw[0] - min_x) * scale,
                    y=height - (offset_y + (raw[1] - min_y) * scale),
                )

            entities = []
            for primitive in primitives:
                common = {
                    "confidence": 1.0,
                    "origin": "spec",
                    "assurance": "constraint_validated",
                    "evidence": [f"step-topology:{view_name}", asset["sha256"]],
                }
                if primitive["type"] == "segment":
                    p1, p2 = point(primitive["p1"]), point(primitive["p2"])
                    if ((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2) ** 0.5 >= 1.0:
                        entities.append(Segment(p1=p1, p2=p2, **common))
                else:
                    entities.append(
                        Circle(
                            center=point(primitive["center"]),
                            radius=primitive["radius"] * scale,
                            **common,
                        )
                    )
            if len(entities) < 3 or len(entities) > 1200:
                rejected.append(
                    f"{projection_path.stem}:{view_name}:entity-count-{len(entities)}"
                )
                continue
            stem = f"{projection_path.stem}__{view_name}"
            ir = CadIR(
                source=SourceInfo(kind="spec", image_width=width, image_height=height),
                entities=entities,
                scale=1 / scale,
                scale_source="calibration",
                recognizer_used="step-topology-projection",
                digitization_status="exact_candidate",
            )
            exact_png = render_ir_to_png(ir)
            source_png = _degrade(
                exact_png,
                seed=int(asset["sha256"][:8], 16) ^ sum(map(ord, view_name)),
            )
            ir_path = out / "ir" / f"{stem}.json"
            image_path = out / "clean" / f"{stem}.png"
            control_path = out / "control" / f"{stem}.png"
            ir_path.write_text(ir.model_dump_json())
            image_path.write_bytes(source_png)
            control_path.write_bytes(exact_png)
            rows.append(
                {
                    "id": stem,
                    "profile": asset["profile"],
                    "kind": "web_step_exact_projection",
                    "source_group_id": asset["source_group_id"],
                    "split": asset["split"],
                    "image": str(image_path.resolve()),
                    "control_images": [str(control_path.resolve())],
                    "ir": str(ir_path.resolve()),
                    "license": asset["license"],
                    "attribution": asset.get("attribution"),
                    "view": view_name,
                    "entity_count": len(entities),
                }
            )
    random.Random(17).shuffle(rows)
    with (out / "manifest.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "sheets": len(rows),
        "source_groups": len({row["source_group_id"] for row in rows}),
        "rejected_views": len(rejected),
        "profiles": {
            profile: sum(row["profile"] == profile for row in rows)
            for profile in ("mechanical", "construction")
        },
        "splits": {
            split: sum(row["split"] == split for row in rows)
            for split in ("train", "val", "holdout")
        },
        "entities": sum(row["entity_count"] for row in rows),
    }
    (out / "summary.json").write_text(json.dumps({**summary, "rejected": rejected}, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", required=True, type=pathlib.Path)
    parser.add_argument("--projections", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(build(args.assets, args.projections, args.out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
