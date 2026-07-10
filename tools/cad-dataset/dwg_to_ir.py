#!/usr/bin/env python3
"""Extract ground-truth CadIR from real DWG files — HOLDOUT ONLY.

Per the plan: "train/val/holdout split (holdout — только реальные чертежи,
не синтетика)". The neural vectorizer is trained exclusively on
``synth_ir.py`` output; these real drawings are held out and used only to
measure whether it generalizes to actual production geometry (and, per the
Dual-AI rule, never leave local-only storage/training).

Entity extraction is deliberately conservative about geometric conventions:
LINE → Segment (a 2-point transform has no angle-sense ambiguity), while
CIRCLE/ARC/LWPOLYLINE/SPLINE/ELLIPSE are all flattened to point sequences via
ezdxf's own ``.flattening()`` and stored as Polyline — this sidesteps any
risk of getting the DXF (y-up, CCW) vs CV (y-down, cv2-clockwise) arc-angle
convention backwards, which pixel-coverage-based eval (recall/precision, not
exact command-type matching) doesn't need anyway.

The clean PNG is rendered from these SAME extracted+transformed entities via
the project's own ``cad_ir.png_render`` — not a separate matplotlib pass —
so ground truth and raster agree pixel-for-pixel by construction.

Usage:
    python3 dwg_to_ir.py --src <dir with .dwg> --out <holdout dir>
                         [--long-side 1200] [--repo <repo root>]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

_FONT_CODE = re.compile(r"\\f[^;]*;")
_FLATTEN_SAGITTA = 0.3  # DXF units (mm) — fine enough for pixel-space GT


def convert_dwg(dwg_path: pathlib.Path, tmp_dir: pathlib.Path) -> pathlib.Path | None:
    if shutil.which("dwg2dxf") is None:
        print("ERROR: dwg2dxf not found (build LibreDWG)", file=sys.stderr)
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


def _prune_missing_blocks(doc) -> int:
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


def _extract_entities(msp, transform) -> list[dict]:
    """(x,y in DXF units) -> {x,y} in pixel space via ``transform``."""
    out: list[dict] = []

    def pt(x: float, y: float) -> dict:
        px, py = transform(x, y)
        return {"x": px, "y": py}

    for e in msp:
        try:
            dxftype = e.dxftype()
            if dxftype == "LINE":
                out.append({
                    "type": "segment",
                    "p1": pt(e.dxf.start.x, e.dxf.start.y),
                    "p2": pt(e.dxf.end.x, e.dxf.end.y),
                })
            elif dxftype in ("CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE"):
                points = [pt(p[0], p[1]) for p in e.flattening(_FLATTEN_SAGITTA)]
                if len(points) < 2:
                    continue
                closed = dxftype == "CIRCLE" or bool(getattr(e, "closed", False)) or (
                    hasattr(e.dxf, "flags") and dxftype == "LWPOLYLINE" and e.closed
                )
                out.append({"type": "polyline", "points": points, "closed": closed})
        except Exception:  # noqa: BLE001 — skip entities ezdxf can't flatten
            continue
    return out


def process_one(dwg_path: pathlib.Path, tmp_dir: pathlib.Path, long_side: int) -> tuple[dict, "object"] | None:
    import ezdxf
    from ezdxf import bbox

    dxf_path = convert_dwg(dwg_path, tmp_dir)
    if dxf_path is None:
        return None
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception:  # noqa: BLE001
        try:
            from ezdxf import recover

            doc, _aud = recover.readfile(dxf_path)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL read {dwg_path.name}: {str(exc)[:160]}", file=sys.stderr)
            return None

    msp = doc.modelspace()
    _prune_missing_blocks(doc)
    try:
        extents = bbox.extents(msp, fast=True)
    except Exception:  # noqa: BLE001
        extents = None
    if extents is None or not extents.has_data or extents.size.x <= 0 or extents.size.y <= 0:
        print(f"SKIP {dwg_path.name}: empty/degenerate extents", file=sys.stderr)
        return None

    min_x, min_y = extents.extmin.x, extents.extmin.y
    ex_w, ex_h = extents.size.x, extents.size.y
    scale = long_side / max(ex_w, ex_h)
    px_w, px_h = max(1, round(ex_w * scale)), max(1, round(ex_h * scale))

    def transform(x: float, y: float) -> tuple[float, float]:
        return (x - min_x) * scale, (ex_h - (y - min_y)) * scale  # DXF y-up -> image y-down

    raw_entities = _extract_entities(msp, transform)
    if len(raw_entities) < 3:
        print(f"SKIP {dwg_path.name}: too few entities ({len(raw_entities)})", file=sys.stderr)
        return None

    for e in raw_entities:
        e["line_class"] = "contour"
        e["width_class"] = "main"
        e["confidence"] = 1.0
        e["origin"] = "spec"
        e["assurance"] = "observed"  # exact DWG geometry — as good as ground truth gets

    ir = {
        "schema_version": 2,
        "units": "mm",
        "scale": round(1.0 / scale, 6),
        "source": {"image_width": px_w, "image_height": px_h, "kind": "scan"},
        "entities": raw_entities,
    }
    return ir, (px_w, px_h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--long-side", type=int, default=1200)
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    args = ap.parse_args()

    (args.out / "clean").mkdir(parents=True, exist_ok=True)
    (args.out / "ir").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(args.repo / "backend"))
    from PIL import Image  # noqa: E402

    from app.ai.cad_ir.png_render import rasterize_entities  # noqa: E402
    from app.ai.cad_ir.schema import Polyline, Segment  # noqa: E402

    ok = fail = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = pathlib.Path(tmp)
        for dwg_path in sorted(args.src.glob("*.dwg")):
            print(f"[dwg] {dwg_path.name}")
            result = process_one(dwg_path, tmp_dir, args.long_side)
            if result is None:
                fail += 1
                continue
            ir, (w, h) = result
            entities_obj = [
                Segment(**{k: v for k, v in e.items() if k != "type"})
                if e["type"] == "segment"
                else Polyline(**{k: v for k, v in e.items() if k != "type"})
                for e in ir["entities"]
            ]
            canvas = rasterize_entities(entities_obj, w, h, thin_px=2, thick_px=3)
            import numpy as np

            stem = dwg_path.stem.replace(" ", "_")
            Image.fromarray(np.stack([canvas] * 3, axis=-1)).save(args.out / "clean" / f"{stem}.png")
            (args.out / "ir" / f"{stem}.json").write_text(json.dumps(ir, ensure_ascii=False))
            print(f"  OK: {len(ir['entities'])} entities, {w}x{h}px")
            ok += 1
    print(f"done: {ok} holdout pairs, {fail} skipped/failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
