#!/usr/bin/env python3
"""Build exact local-geometry tiles from a CadIR corpus.

The seq2seq model has a hard command budget and therefore must not be trained
on whole engineering sheets containing thousands of primitives.  This tool
creates overlapping, source-grouped tiles and keeps only complete,
sequence-encodable entities.  Long cross-tile strokes are deliberately left
to the global CV/line recognizer; inventing clipped fragments would make the
target semantically false.

Input is the corpus layout produced by ``generate_profile_corpus.py``:
``ir/*.json`` plus ``manifest.jsonl``.  Output has ``ir/``, ``clean/``,
``control/`` and a compatible source-group split manifest.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Iterable


def _origins(length: int, tile: int, overlap: int) -> list[int]:
    if length <= tile:
        return [0]
    stride = tile - overlap
    values = list(range(0, length - tile + 1, stride))
    last = length - tile
    if values[-1] != last:
        values.append(last)
    return values


def _bounds(entity) -> tuple[float, float, float, float] | None:
    from app.ai.cad_ir.schema import Arc, Circle, HatchRegion, Polyline, Segment

    if isinstance(entity, Segment):
        points = [entity.p1, entity.p2]
    elif isinstance(entity, (Arc, Circle)):
        return (
            entity.center.x - entity.radius,
            entity.center.y - entity.radius,
            entity.center.x + entity.radius,
            entity.center.y + entity.radius,
        )
    elif isinstance(entity, Polyline):
        points = entity.points
    elif isinstance(entity, HatchRegion):
        points = [*entity.boundary, *(point for hole in entity.holes for point in hole)]
    else:
        return None
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


def _contained(bounds: tuple[float, float, float, float], box: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = box
    bx0, by0, bx1, by1 = bounds
    return bx0 >= x0 and by0 >= y0 and bx1 <= x1 and by1 <= y1


def _translate(entity, x0: int, y0: int):
    from app.ai.cad_ir.schema import Arc, Circle, HatchRegion, Point, Polyline, Segment

    out = entity.model_copy(deep=True)

    def move(point):
        return Point(x=point.x - x0, y=point.y - y0)

    if isinstance(out, Segment):
        out.p1, out.p2 = move(out.p1), move(out.p2)
    elif isinstance(out, (Arc, Circle)):
        out.center = move(out.center)
    elif isinstance(out, Polyline):
        out.points = [move(point) for point in out.points]
    elif isinstance(out, HatchRegion):
        out.boundary = [move(point) for point in out.boundary]
        out.holes = [[move(point) for point in hole] for hole in out.holes]
    out.evidence = [*out.evidence, f"tile-source:{entity.id}"]
    return out


def tile_corpus(
    source: pathlib.Path,
    out: pathlib.Path,
    *,
    tile_size: int = 640,
    overlap: int = 160,
    max_commands: int = 180,
    repo: pathlib.Path | None = None,
) -> dict:
    if tile_size <= 0 or overlap < 0 or overlap >= tile_size:
        raise ValueError("require tile_size > overlap >= 0")
    repo = repo or pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "backend"))

    from app.ai.cad_ir.png_render import render_ir_to_png
    from app.ai.cad_ir.schema import CadIR, SheetInfo, SourceInfo
    from app.ai.cad_ir.sequence import encode
    from PIL import Image

    rows_by_id = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in (source / "manifest.jsonl").read_text().splitlines()
            if line.strip()
        )
    }
    for folder in ("ir", "clean", "control"):
        (out / folder).mkdir(parents=True, exist_ok=True)

    output_rows = []
    skipped_empty = skipped_budget = 0
    for ir_path in sorted((source / "ir").glob("*.json")):
        parent = CadIR.model_validate_json(ir_path.read_text())
        meta = rows_by_id[ir_path.stem]
        xs = _origins(parent.source.image_width, tile_size, overlap)
        ys = _origins(parent.source.image_height, tile_size, overlap)
        for row_index, y0 in enumerate(ys):
            for col_index, x0 in enumerate(xs):
                width = min(tile_size, parent.source.image_width - x0)
                height = min(tile_size, parent.source.image_height - y0)
                box = (x0, y0, x0 + width, y0 + height)
                entities = [
                    _translate(entity, x0, y0)
                    for entity in parent.entities
                    if (bounds := _bounds(entity)) is not None and _contained(bounds, box)
                ]
                if not entities:
                    skipped_empty += 1
                    continue
                tile_ir = parent.model_copy(
                    deep=True,
                    update={
                        "source": SourceInfo(image_width=width, image_height=height, kind="spec"),
                        "sheet": SheetInfo(
                            title_block={
                                "tile_parent": ir_path.stem,
                                "tile_box": list(box),
                            }
                        ),
                        "entities": entities,
                        "review": [],
                        "unresolved_regions": [],
                    },
                )
                command_count = len(encode(tile_ir)) - 1
                if command_count > max_commands:
                    skipped_budget += 1
                    continue
                stem = f"{ir_path.stem}__r{row_index:02d}c{col_index:02d}"
                tile_ir_path = out / "ir" / f"{stem}.json"
                image_path = out / "clean" / f"{stem}.png"
                control_path = out / "control" / f"{stem}__v0.png"
                tile_ir_path.write_text(tile_ir.model_dump_json())
                exact_png = render_ir_to_png(tile_ir)
                source_image_path = pathlib.Path(meta["image"])
                if source_image_path.exists():
                    with Image.open(source_image_path) as source_image:
                        crop = source_image.convert("L").crop(box)
                        crop.save(image_path, format="PNG")
                else:
                    image_path.write_bytes(exact_png)
                control_path.write_bytes(exact_png)
                output_rows.append(
                    {
                        "id": stem,
                        "profile": meta["profile"],
                        "kind": "exact_geometry_tile",
                        "source_group_id": meta["source_group_id"],
                        "split": meta["split"],
                        "image": str(image_path.resolve()),
                        "control_images": [str(control_path.resolve())],
                        "ir": str(tile_ir_path.resolve()),
                        "parent_id": ir_path.stem,
                        "tile_box": list(box),
                        "command_count": command_count,
                    }
                )

    with (out / "manifest.jsonl").open("w") as stream:
        for row in output_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "tiles": len(output_rows),
        "skipped_empty": skipped_empty,
        "skipped_command_budget": skipped_budget,
        "max_commands": max((row["command_count"] for row in output_rows), default=0),
        "source_groups": len({row["source_group_id"] for row in output_rows}),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--tile-size", type=int, default=640)
    parser.add_argument("--overlap", type=int, default=160)
    parser.add_argument("--max-commands", type=int, default=180)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            tile_corpus(
                args.source,
                args.out,
                tile_size=args.tile_size,
                overlap=args.overlap,
                max_commands=args.max_commands,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
