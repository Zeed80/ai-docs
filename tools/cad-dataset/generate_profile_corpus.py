#!/usr/bin/env python3
"""Generate exact, profile-balanced raster/DXF/CadIR training pairs.

This corpus does not pretend to be real customer data. It is reproducible
open-derived synthetic data used for training, while real licensed/local
drawings remain source-grouped holdout. Every target contains editable
geometry, dimensions, annotations, hatches, frame and title block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import sys
from collections.abc import Callable

GENERATOR_VERSION = "profile-corpus-v1"
WIDTH = 1600
HEIGHT = 1100
SCALE_MM_PER_PX = 0.25


def _split(group: str) -> str:
    bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else ("val" if bucket < 90 else "holdout")


def _base_entities():
    from app.ai.cad_ir.schema import Point, Segment, TextEntity

    p = Point
    lines = [
        Segment(p1=p(x=35, y=30), p2=p(x=1565, y=30), origin="spec", assurance="constraint_validated"),
        Segment(p1=p(x=1565, y=30), p2=p(x=1565, y=1070), origin="spec", assurance="constraint_validated"),
        Segment(p1=p(x=1565, y=1070), p2=p(x=35, y=1070), origin="spec", assurance="constraint_validated"),
        Segment(p1=p(x=35, y=1070), p2=p(x=35, y=30), origin="spec", assurance="constraint_validated"),
    ]
    # Compact A3-like title block.
    for y in (900, 955, 1010):
        lines.append(
            Segment(
                p1=p(x=1120, y=y),
                p2=p(x=1565, y=y),
                line_class="thin",
                width_class="thin",
                origin="spec",
                assurance="constraint_validated",
            )
        )
    for x in (1220, 1390):
        lines.append(
            Segment(
                p1=p(x=x, y=900),
                p2=p(x=x, y=1070),
                line_class="thin",
                width_class="thin",
                origin="spec",
                assurance="constraint_validated",
            )
        )
    lines.extend(
        [
            TextEntity(position=p(x=1140, y=935), text="DIGITIZATION DATASET", height=18, origin="spec"),
            TextEntity(position=p(x=1140, y=990), text="SCALE 1:1", height=15, origin="spec"),
            TextEntity(position=p(x=1410, y=1045), text="A3", height=22, origin="spec"),
        ]
    )
    return lines


def _mechanical(rng: random.Random):
    from app.ai.cad_ir.schema import (
        AnnotationEntity,
        Circle,
        DimensionEntity,
        HatchRegion,
        Point,
        Segment,
        TextEntity,
    )

    p = Point
    entities = _base_entities()
    cx, cy = 460, 450
    width = rng.randint(360, 600)
    height = rng.randint(220, 380)
    radius = rng.randint(28, 65)
    x0, x1 = cx - width / 2, cx + width / 2
    y0, y1 = cy - height / 2, cy + height / 2
    for a, b in (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ):
        entities.append(
            Segment(p1=p(x=a[0], y=a[1]), p2=p(x=b[0], y=b[1]), origin="spec",
                    assurance="constraint_validated")
        )
    hole_centers = []
    for dx in (-width * 0.32, width * 0.32):
        for dy in (-height * 0.28, height * 0.28):
            hole_centers.append((cx + dx, cy + dy))
    for hx, hy in hole_centers:
        entities.append(
            Circle(center=p(x=hx, y=hy), radius=radius, origin="spec",
                   assurance="constraint_validated")
        )
        entities.extend(
            [
                Segment(
                    p1=p(x=hx - radius * 1.5, y=hy),
                    p2=p(x=hx + radius * 1.5, y=hy),
                    line_class="axis",
                    width_class="thin",
                    origin="spec",
                    assurance="constraint_validated",
                ),
                Segment(
                    p1=p(x=hx, y=hy - radius * 1.5),
                    p2=p(x=hx, y=hy + radius * 1.5),
                    line_class="axis",
                    width_class="thin",
                    origin="spec",
                    assurance="constraint_validated",
                ),
            ]
        )
    # Side/section view.
    sx0, sx1, sy0, sy1 = 940, 1240, 300, 640
    entities.append(
        HatchRegion(
            boundary=[p(x=sx0, y=sy0), p(x=sx1, y=sy0), p(x=sx1, y=sy1), p(x=sx0, y=sy1)],
            holes=[
                [
                    p(x=1060, y=410),
                    p(x=1120, y=410),
                    p(x=1120, y=530),
                    p(x=1060, y=530),
                ]
            ],
            pattern="ansi31",
            origin="spec",
            assurance="constraint_validated",
        )
    )
    for a, b in (
        ((sx0, sy0), (sx1, sy0)),
        ((sx1, sy0), (sx1, sy1)),
        ((sx1, sy1), (sx0, sy1)),
        ((sx0, sy1), (sx0, sy0)),
    ):
        entities.append(
            Segment(p1=p(x=a[0], y=a[1]), p2=p(x=b[0], y=b[1]), origin="spec",
                    assurance="constraint_validated")
        )
    entities.extend(
        [
            DimensionEntity(
                p1=p(x=x0, y=y1 + 70),
                p2=p(x=x1, y=y1 + 70),
                text=f"{width * SCALE_MM_PER_PX:.0f}",
                value_mm=width * SCALE_MM_PER_PX,
                origin="spec",
                assurance="constraint_validated",
            ),
            DimensionEntity(
                p1=p(x=x1 + 80, y=y0),
                p2=p(x=x1 + 80, y=y1),
                text=f"{height * SCALE_MM_PER_PX:.0f}",
                value_mm=height * SCALE_MM_PER_PX,
                origin="spec",
                assurance="constraint_validated",
            ),
            DimensionEntity(
                kind="diameter",
                p1=p(x=hole_centers[0][0] - radius, y=hole_centers[0][1]),
                p2=p(x=hole_centers[0][0] + radius, y=hole_centers[0][1]),
                text=f"Ø{2 * radius * SCALE_MM_PER_PX:.0f} H7",
                value_mm=2 * radius * SCALE_MM_PER_PX,
                tolerance="H7",
                origin="spec",
                assurance="constraint_validated",
            ),
            AnnotationEntity(
                kind="roughness",
                position=p(x=870, y=720),
                leader=p(x=1010, y=620),
                value=rng.choice(["1.6", "3.2", "6.3"]),
                origin="spec",
                assurance="constraint_validated",
            ),
            TextEntity(position=p(x=300, y=190), text="FRONT VIEW", height=18, origin="spec"),
            TextEntity(position=p(x=1010, y=190), text="SECTION A-A", height=18, origin="spec"),
        ]
    )
    return entities


def _construction(rng: random.Random):
    from app.ai.cad_ir.schema import Arc, DimensionEntity, HatchRegion, Point, Polyline, Segment, TextEntity

    p = Point
    entities = _base_entities()
    x0, y0, x1, y1 = 150, 150, 1400, 830
    wall = rng.randint(24, 38)
    # Outer and inner wall faces are editable polylines, not a filled bitmap.
    entities.extend(
        [
            Polyline(
                points=[p(x=x0, y=y0), p(x=x1, y=y0), p(x=x1, y=y1), p(x=x0, y=y1)],
                closed=True,
                origin="spec",
                assurance="constraint_validated",
            ),
            Polyline(
                points=[
                    p(x=x0 + wall, y=y0 + wall),
                    p(x=x1 - wall, y=y0 + wall),
                    p(x=x1 - wall, y=y1 - wall),
                    p(x=x0 + wall, y=y1 - wall),
                ],
                closed=True,
                origin="spec",
                assurance="constraint_validated",
            ),
        ]
    )
    verticals = sorted(rng.sample(range(460, 1120, 80), 2))
    horizontal = rng.choice(range(390, 650, 50))
    for x in verticals:
        entities.extend(
            [
                Segment(p1=p(x=x, y=y0 + wall), p2=p(x=x, y=y1 - wall), origin="spec",
                        assurance="constraint_validated"),
                Segment(p1=p(x=x + wall, y=y0 + wall), p2=p(x=x + wall, y=y1 - wall),
                        origin="spec", assurance="constraint_validated"),
            ]
        )
    entities.extend(
        [
            Segment(p1=p(x=x0 + wall, y=horizontal), p2=p(x=x1 - wall, y=horizontal),
                    origin="spec", assurance="constraint_validated"),
            Segment(p1=p(x=x0 + wall, y=horizontal + wall), p2=p(x=x1 - wall, y=horizontal + wall),
                    origin="spec", assurance="constraint_validated"),
        ]
    )
    # Door swings and windows.
    door_y = horizontal - 110
    for x in verticals:
        entities.append(
            Arc(
                center=p(x=x, y=door_y),
                radius=95,
                start_angle=270,
                end_angle=360,
                line_class="thin",
                width_class="thin",
                origin="spec",
                assurance="constraint_validated",
            )
        )
        entities.append(
            Segment(
                p1=p(x=x, y=door_y),
                p2=p(x=x + 95, y=door_y),
                line_class="thin",
                width_class="thin",
                origin="spec",
                assurance="constraint_validated",
            )
        )
    for x in (330, 720, 1210):
        entities.extend(
            [
                Segment(p1=p(x=x, y=y0), p2=p(x=x + 120, y=y0), line_class="thin",
                        width_class="thin", origin="spec", assurance="constraint_validated"),
                Segment(p1=p(x=x, y=y0 + wall), p2=p(x=x + 120, y=y0 + wall),
                        line_class="thin", width_class="thin", origin="spec",
                        assurance="constraint_validated"),
            ]
        )
    # Small hatched structural core.
    entities.append(
        HatchRegion(
            boundary=[
                p(x=verticals[0] + wall, y=horizontal + wall),
                p(x=verticals[1], y=horizontal + wall),
                p(x=verticals[1], y=y1 - wall),
                p(x=verticals[0] + wall, y=y1 - wall),
            ],
            pattern="ansi31",
            origin="spec",
            assurance="constraint_validated",
        )
    )
    room_centers = [
        ((x0 + verticals[0]) / 2, (y0 + horizontal) / 2, "ROOM 101"),
        ((verticals[0] + verticals[1]) / 2, (y0 + horizontal) / 2, "ROOM 102"),
        ((verticals[1] + x1) / 2, (y0 + horizontal) / 2, "ROOM 103"),
    ]
    for tx, ty, label in room_centers:
        entities.append(TextEntity(position=p(x=tx - 45, y=ty), text=label, height=18, origin="spec"))
    entities.extend(
        [
            DimensionEntity(
                p1=p(x=x0, y=880),
                p2=p(x=x1, y=880),
                text=f"{(x1 - x0) * SCALE_MM_PER_PX:.0f}",
                value_mm=(x1 - x0) * SCALE_MM_PER_PX,
                origin="spec",
                assurance="constraint_validated",
            ),
            DimensionEntity(
                p1=p(x=1450, y=y0),
                p2=p(x=1450, y=y1),
                text=f"{(y1 - y0) * SCALE_MM_PER_PX:.0f}",
                value_mm=(y1 - y0) * SCALE_MM_PER_PX,
                origin="spec",
                assurance="constraint_validated",
            ),
            TextEntity(position=p(x=650, y=100), text="FLOOR PLAN", height=24, origin="spec"),
        ]
    )
    return entities


GENERATORS: dict[str, Callable[[random.Random], list]] = {
    "mechanical": _mechanical,
    "construction": _construction,
}


def _render_with_labels(ir, path: pathlib.Path) -> None:
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    from app.ai.cad_ir.annotations import annotation_text
    from app.ai.cad_ir.png_render import rasterize_entities
    from app.ai.cad_ir.schema import AnnotationEntity, DimensionEntity, TextEntity

    canvas = rasterize_entities(ir.entities, WIDTH, HEIGHT, thin_px=2, thick_px=4)
    image = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_GRAY2RGB))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)
    for entity in ir.entities:
        if isinstance(entity, TextEntity):
            draw.text((entity.position.x, entity.position.y), entity.text, fill="black", font=font)
        elif isinstance(entity, DimensionEntity) and entity.text:
            x = (entity.p1.x + entity.p2.x) / 2
            y = (entity.p1.y + entity.p2.y) / 2
            draw.text((x, y - 22), entity.text, fill="black", font=font, anchor="mm")
        elif isinstance(entity, AnnotationEntity):
            label = annotation_text(entity.kind, entity.value, entity.symbol, entity.datum_refs)
            draw.text((entity.position.x, entity.position.y), label, fill="black", font=font)
    image.save(path)


def _degraded_variants(
    clean_path: pathlib.Path,
    out: pathlib.Path,
    stem: str,
    seed: int,
    variants: int,
    repo: pathlib.Path,
) -> list[str]:
    import numpy as np
    from PIL import Image

    degrade_root = repo / "tools" / "lora-dataset"
    if str(degrade_root) not in sys.path:
        sys.path.insert(0, str(degrade_root))
    from degrade import clahe_like_prod, simulate_photo, unwarp_exact

    clean = np.asarray(Image.open(clean_path).convert("RGB"))
    paths = []
    for variant in range(variants):
        variant_rng = np.random.default_rng(seed * 100 + variant)
        photo, quad = simulate_photo(clean, variant_rng)
        control = clahe_like_prod(unwarp_exact(photo, quad, WIDTH, HEIGHT, variant_rng))
        path = out / "control" / f"{stem}__v{variant}.png"
        Image.fromarray(control).save(path)
        paths.append(str(path.resolve()))
    return paths


def generate(
    out: pathlib.Path,
    count: int,
    seed: int,
    profiles: list[str],
    *,
    variants: int = 1,
    repo: pathlib.Path | None = None,
) -> dict:
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf
    from app.ai.cad_ir.schema import CadIR, SheetInfo, SourceInfo

    repo = repo or pathlib.Path(__file__).resolve().parents[2]
    for folder in ("clean", "control", "dxf", "ir"):
        (out / folder).mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    for profile in profiles:
        for index in range(count):
            sample_seed = seed + (0 if profile == "mechanical" else 1_000_000) + index
            rng = random.Random(sample_seed)
            ir = CadIR(
                scale=SCALE_MM_PER_PX,
                scale_source="sheet_format",
                source=SourceInfo(image_width=WIDTH, image_height=HEIGHT, kind="spec"),
                sheet=SheetInfo(
                    format="A3",
                    width_mm=400,
                    height_mm=275,
                    frame=True,
                    title_block={"dataset_profile": profile, "generator_version": GENERATOR_VERSION},
                ),
                entities=GENERATORS[profile](rng),
                digitization_status="exact_candidate",
                recognizer_used="spec",
            )
            stem = f"{profile}_{index:04d}"
            ir_path = out / "ir" / f"{stem}.json"
            dxf_path = out / "dxf" / f"{stem}.dxf"
            image_path = out / "clean" / f"{stem}.png"
            ir_path.write_text(ir.model_dump_json())
            dxf_path.write_bytes(render_ir_to_dxf(ir))
            _render_with_labels(ir, image_path)
            control_paths = _degraded_variants(
                image_path, out, stem, sample_seed, variants, repo
            ) if variants else []
            group = f"{GENERATOR_VERSION}:{profile}:{index:04d}"
            manifest_rows.append(
                {
                    "id": stem,
                    "profile": profile,
                    "kind": "open_derived_synthetic",
                    "source_group_id": group,
                    "split": _split(group),
                    "image": str(image_path.resolve()),
                    "control_images": control_paths,
                    "dxf": str(dxf_path.resolve()),
                    "ir": str(ir_path.resolve()),
                    "entities": ir.counts(),
                    "generator_version": GENERATOR_VERSION,
                    "seed": sample_seed,
                }
            )
    with (out / "manifest.jsonl").open("w") as stream:
        for row in manifest_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "total": len(manifest_rows),
        "profiles": {profile: sum(row["profile"] == profile for row in manifest_rows) for profile in profiles},
        "splits": {
            split: sum(row["split"] == split for row in manifest_rows)
            for split in ("train", "val", "holdout")
        },
        "generator_version": GENERATOR_VERSION,
        "truth_kind": "exact_open_derived_synthetic",
        "degraded_variants_per_sheet": variants,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("cad-dataset-out/profile-corpus"))
    parser.add_argument("--count", type=int, default=300, help="sheets per profile")
    parser.add_argument("--seed", type=int, default=20260719)
    parser.add_argument("--variants", type=int, default=2, help="aligned degraded inputs per sheet")
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=sorted(GENERATORS),
        default=sorted(GENERATORS),
    )
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / "backend"))
    summary = generate(
        args.out,
        args.count,
        args.seed,
        args.profiles,
        variants=args.variants,
        repo=args.repo,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
