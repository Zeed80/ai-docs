#!/usr/bin/env python3
"""Render DWG/DXF drawings to clean white-background PNG targets for the
cleanup-LoRA dataset (see README.md in this directory).

DWG files are converted with LibreDWG's ``dwg2dxf`` (built from source — not
in distro repos). Two conversion artifacts are repaired before rendering,
both found on real ЗС-* production DWGs:

- INSERTs referencing anonymous blocks (``*H``, ``*A``) the converter didn't
  carry over → pruned, everything else renders.
- MTEXT inline font codes (``{\\fArial|...;+2.400}``) → the matplotlib
  backend tries to resolve the named font and draws tofu boxes when it's not
  installed; flattening to plain text renders with the default (Cyrillic-
  capable) font instead.

Usage:
    python3 render_dwg.py --src <dir with .dwg/.dxf> --out <png dir>
                          [--long-side 2048] [--dpi 150]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import matplotlib

matplotlib.use("Agg")

import ezdxf  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from ezdxf import bbox  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import (  # noqa: E402
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
)
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend  # noqa: E402
from ezdxf.tools.text import plain_mtext  # noqa: E402

_FONT_CODE = re.compile(r"\\f[^;]*;")


def convert_dwg(dwg_path: pathlib.Path, tmp_dir: pathlib.Path) -> pathlib.Path | None:
    if shutil.which("dwg2dxf") is None:
        print("ERROR: dwg2dxf not found (build LibreDWG, see README)", file=sys.stderr)
        return None
    out = tmp_dir / (dwg_path.stem + ".dxf")
    proc = subprocess.run(
        ["dwg2dxf", "-y", "-o", str(out), str(dwg_path)],
        capture_output=True, text=True, timeout=300,
    )
    if not out.exists():
        print(f"FAIL convert {dwg_path.name}: {proc.stderr[:200]}", file=sys.stderr)
        return None
    return out


def prune_missing_blocks(doc) -> int:
    removed = 0
    layouts = [doc.modelspace()] + [doc.blocks.get(b.name) for b in doc.blocks]
    for layout in layouts:
        if layout is None:
            continue
        for ins in list(layout.query("INSERT")):
            if ins.dxf.name not in doc.blocks:
                layout.delete_entity(ins)
                removed += 1
    return removed


def flatten_mtext_fonts(doc) -> int:
    changed = 0
    layouts = [doc.modelspace()] + [doc.blocks.get(b.name) for b in doc.blocks]
    for layout in layouts:
        if layout is None:
            continue
        for e in layout.query("MTEXT"):
            if _FONT_CODE.search(e.text):
                e.text = plain_mtext(e.text)
                changed += 1
    return changed


def render(dxf_path: pathlib.Path, out_png: pathlib.Path, long_side: int, dpi: int) -> bool:
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL read {dxf_path.name}: {str(exc)[:160]}", file=sys.stderr)
        return False
    pruned = prune_missing_blocks(doc)
    flattened = flatten_mtext_fonts(doc)

    msp = doc.modelspace()
    try:
        extents = bbox.extents(msp, fast=True)
        ratio = (extents.size.y / extents.size.x) if extents.has_data and extents.size.x else 0.7
    except Exception:  # noqa: BLE001
        ratio = 0.7
    ratio = min(max(ratio, 0.1), 10.0)
    if ratio <= 1.0:
        fig_w, fig_h = long_side / dpi, long_side * ratio / dpi
    else:
        fig_w, fig_h = long_side / ratio / dpi, long_side / dpi

    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK
    )
    try:
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
        ax = fig.add_axes([0, 0, 1, 1])
        Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(
            msp, finalize=True
        )
        fig.savefig(out_png, dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.15)
        plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL render {dxf_path.name}: {str(exc)[:160]}", file=sys.stderr)
        return False
    print(f"OK {dxf_path.stem} (pruned {pruned} inserts, flattened {flattened} mtext)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--long-side", type=int, default=2048)
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    ok = fail = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = pathlib.Path(tmp)
        for path in sorted(args.src.iterdir()):
            suffix = path.suffix.lower()
            if suffix == ".dwg":
                dxf = convert_dwg(path, tmp_dir)
            elif suffix == ".dxf":
                dxf = path
            else:
                continue
            if dxf and render(dxf, args.out / (path.stem + ".png"), args.long_side, args.dpi):
                ok += 1
            else:
                fail += 1
    print(f"done: {ok} ok, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
