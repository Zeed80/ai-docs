#!/usr/bin/env python3
"""Compose source-grouped multi-view sheets for global layout recognition.

Every sheet contains orthographic views of one STEP model only.  The original
source-group split is preserved, so alternate views and layout augmentations
of a part can never leak between train, validation and holdout.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFilter

VIEW_NAMES = ("front", "top", "side")
SHEET_SIZE = (1024, 768)


def _layout_boxes(count: int, variant: int) -> list[tuple[int, int, int, int]]:
    layouts = (
        ((45, 55, 500, 365), (524, 55, 979, 365), (285, 403, 740, 713)),
        ((45, 55, 500, 713), (524, 55, 979, 365), (524, 403, 979, 713)),
        ((45, 55, 979, 360), (45, 398, 500, 713), (524, 398, 979, 713)),
    )
    return list(layouts[variant % len(layouts)][:count])


def _paste_fit(canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    max_width, max_height = x1 - x0, y1 - y0
    scale = min(max_width / source.width, max_height / source.height)
    width = max(1, round(source.width * scale))
    height = max(1, round(source.height * scale))
    resized = source.resize((width, height), Image.Resampling.LANCZOS)
    canvas.paste(
        resized,
        (x0 + (max_width - width) // 2, y0 + (max_height - height) // 2),
    )


def build(
    source: pathlib.Path,
    out: pathlib.Path,
    *,
    train_variants: int = 9,
    eval_variants: int = 3,
) -> dict:
    rows = [
        json.loads(line)
        for line in (source / "manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("view") in VIEW_NAMES:
            grouped[row["source_group_id"]].append(row)

    image_dir = out / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    output_rows: list[dict] = []
    for group_id, group_rows in sorted(grouped.items()):
        by_view = {row["view"]: row for row in group_rows}
        selected = [by_view[name] for name in VIEW_NAMES if name in by_view]
        if len(selected) < 2:
            continue
        split = selected[0]["split"]
        variants = train_variants if split == "train" else eval_variants
        group_seed = sum(ord(char) for char in group_id)
        for variant in range(variants):
            rng = random.Random(group_seed + variant * 1009)
            ordered = selected[:]
            rng.shuffle(ordered)
            boxes = _layout_boxes(len(ordered), variant)
            # Small global layout jitter without changing view identity.
            jittered = []
            for box in boxes:
                dx, dy = rng.randint(-12, 12), rng.randint(-10, 10)
                jittered.append((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))

            canvas = Image.new("L", SHEET_SIZE, 255)
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((8, 8, SHEET_SIZE[0] - 9, SHEET_SIZE[1] - 9), outline=220)
            targets = []
            for row, box in zip(ordered, jittered, strict=True):
                with Image.open(row["image"]) as image:
                    _paste_fit(canvas, image.convert("L"), box)
                # Localization and semantic orientation are deliberately
                # separate tasks.  A single projection can be visually
                # ambiguous without its cross-view relations, so the layout
                # detector learns only the honest "view region" target.
                targets.append(
                    {"kind": "view", "source_view": row["view"], "box": list(box)}
                )
            if rng.random() < 0.65:
                canvas = canvas.filter(ImageFilter.GaussianBlur(rng.uniform(0.15, 0.55)))
            stem = f"{selected[0]['id'].rsplit('__', 1)[0]}__layout{variant:02d}"
            image_path = image_dir / f"{stem}.png"
            canvas.save(image_path)
            output_rows.append(
                {
                    "id": stem,
                    "source_group_id": group_id,
                    "split": split,
                    "profile": selected[0]["profile"],
                    "image": str(image_path.resolve()),
                    "width": SHEET_SIZE[0],
                    "height": SHEET_SIZE[1],
                    "targets": targets,
                    "license": selected[0].get("license"),
                    "attribution": selected[0].get("attribution"),
                }
            )

    with (out / "manifest.jsonl").open("w") as stream:
        for row in output_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "sheets": len(output_rows),
        "source_groups": len({row["source_group_id"] for row in output_rows}),
        "splits": {
            split: sum(row["split"] == split for row in output_rows)
            for split in ("train", "val", "holdout")
        },
        "views": sum(len(row["targets"]) for row in output_rows),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--train-variants", type=int, default=9)
    parser.add_argument("--eval-variants", type=int, default=3)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.source,
                args.out,
                train_variants=args.train_variants,
                eval_variants=args.eval_variants,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
