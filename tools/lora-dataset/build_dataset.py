#!/usr/bin/env python3
"""Assemble (target, control, caption) triples into an ai-toolkit dataset
for Qwen-Image-Edit cleanup-LoRA training.

Inputs:
    --targets   clean renders (render_dwg.py / techdraw synthetics), with
                .txt captions beside them (caption.py)
    --controls  degraded+enhanced controls (degrade.py), named
                <target_stem>__vN.png

Output layout (ai-toolkit convention: control image found by matching
filename in the sibling ``control/`` folder; caption is the .txt next to the
target):

    <out>/
      images/<stem>__vN.png   target (copied once per control variant)
      images/<stem>__vN.txt   training prompt = instruction + VLM caption
      control/<stem>__vN.png  control

Deterministic QA on every pair (cheap, no models): target must actually
contain linework (ink fraction sane), control must not be blank, aspect
ratios must roughly agree (a dewarp that grabbed the wrong quad produces a
mismatched aspect — such a pair would teach the model to crop/stretch).
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

import numpy as np
from PIL import Image

DEFAULT_INSTRUCTION = (
    "convert this into a clean black and white technical line drawing, "
    "crisp sharp uniform lines, remove background, remove noise and shadows, "
    "remove binding, white background. Содержимое чертежа: {caption}"
)


def _ink_fraction(path: pathlib.Path) -> float:
    img = np.asarray(Image.open(path).convert("L"))
    return float((img < 128).mean())


def _aspect(path: pathlib.Path) -> float:
    with Image.open(path) as img:
        return img.width / img.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True, type=pathlib.Path)
    ap.add_argument("--controls", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    args = ap.parse_args()

    images_dir = args.out / "images"
    control_dir = args.out / "control"
    images_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)

    ok = rejected = 0
    for control_path in sorted(args.controls.glob("*__v*.png")):
        stem = control_path.stem.rsplit("__v", 1)[0]
        target_path = args.targets / f"{stem}.png"
        caption_path = args.targets / f"{stem}.txt"
        if not target_path.exists() or not caption_path.exists():
            print(f"REJECT {control_path.name}: no target/caption for {stem!r}")
            rejected += 1
            continue

        ink = _ink_fraction(target_path)
        if not 0.005 <= ink <= 0.35:
            print(f"REJECT {control_path.name}: target ink fraction {ink:.3f}")
            rejected += 1
            continue
        if _ink_fraction(control_path) < 0.001:
            print(f"REJECT {control_path.name}: control is blank")
            rejected += 1
            continue
        # v2: controls are unwarped with ground-truth corners into the
        # target's own frame — aspect must match almost exactly. The loose
        # v1 window (0.75-1.33) let systematically shifted pairs through,
        # and the v1 LoRA learned to re-layout the sheet from them.
        ratio = _aspect(control_path) / _aspect(target_path)
        if not 0.95 <= ratio <= 1.05:
            print(f"REJECT {control_path.name}: aspect mismatch {ratio:.2f}")
            rejected += 1
            continue

        name = control_path.stem
        shutil.copyfile(target_path, images_dir / f"{name}.png")
        shutil.copyfile(control_path, control_dir / f"{name}.png")
        caption = caption_path.read_text(encoding="utf-8").strip()
        (images_dir / f"{name}.txt").write_text(
            args.instruction.format(caption=caption), encoding="utf-8"
        )
        ok += 1

    print(f"done: {ok} pairs, {rejected} rejected -> {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
