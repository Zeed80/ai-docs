#!/usr/bin/env python3
"""B7: build a fine-tuning dataset for the technical-vectorizer line model
from OUR drawings (DXF ground truth and/or accepted CAD IR revisions).

The model sees binarized ink AFTER the pipeline's dewarp/normalize, so the
degradation here reproduces what survives that preprocessing on a real
photo: wavy strokes, varying width, stroke gaps, salt noise — NOT
perspective (dewarp removes it before vectorization).

Output format = the vendored PreprocessedDataset memmap layout the training
code loads: meta.pkl + images float storage [N,3,64,64] (channels exactly as
serve._preprocess_patches builds them: inverted ink, xs*mask, ys*mask) +
targets [N,10,6] rows (x1,y1,x2,y2,width)/64 + presence, zero-padded, in the
same normalized convention the checkpoint was trained with (inference
multiplies outputs by 64).

Usage (inside the backend container — ezdxf + dwg2dxf live there):
  python tools/vectorizer-finetune/make_dataset.py \
      --dwg-dir cleanup_test_files --out data/vectorizer-finetune/ours
"""

from __future__ import annotations

import argparse
import pathlib
import pickle
import struct
import subprocess
import sys
import tempfile

import numpy as np

PATCH = 64
MAX_LINES = 10
TARGET_LEN = 6  # x1,y1,x2,y2,width + presence


# ── ground-truth segment extraction ──────────────────────────────────────────


def segments_from_dxf(path: pathlib.Path) -> list[tuple[float, float, float, float]]:
    import ezdxf
    from ezdxf import recover

    try:
        doc = ezdxf.readfile(path)
    except Exception:  # noqa: BLE001
        doc, _ = recover.readfile(path)
    segments: list[tuple[float, float, float, float]] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            segments.append((s.x, s.y, e.x, e.y))
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            try:
                points = [(p[0], p[1]) for p in entity.get_points()] if kind == "LWPOLYLINE" else [
                    (v.dxf.location.x, v.dxf.location.y) for v in entity.vertices
                ]
            except Exception:  # noqa: BLE001
                continue
            for a, b in zip(points, points[1:]):
                segments.append((a[0], a[1], b[0], b[1]))
        elif kind in ("CIRCLE", "ARC"):
            import math

            c, r = entity.dxf.center, entity.dxf.radius
            a0 = getattr(entity.dxf, "start_angle", 0.0)
            a1 = getattr(entity.dxf, "end_angle", 360.0)
            sweep = (a1 - a0) % 360 or 360
            n = max(int(sweep / 10), 4)
            angles = [math.radians(a0 + sweep * i / n) for i in range(n + 1)]
            pts = [(c.x + r * math.cos(t), c.y + r * math.sin(t)) for t in angles]
            for a, b in zip(pts, pts[1:]):
                segments.append((a[0], a[1], b[0], b[1]))
    return segments


def _strip_sortentstable(dxf_path: pathlib.Path) -> None:
    """dwg2dxf emits SORTENTSTABLE objects (group code 331) that ezdxf 1.4+
    rejects outright; draw order is irrelevant here — drop the object (same
    workaround as backend/scripts/eval_vectorize.py)."""
    lines = dxf_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i + 1 < len(lines):
        code, value = lines[i], lines[i + 1]
        if code.strip() == "0" and value.strip() == "SORTENTSTABLE":
            i += 2
            while i + 1 < len(lines) and lines[i].strip() != "0":
                i += 2
            continue
        out.append(code)
        out.append(value)
        i += 2
    out.extend(lines[i:])
    dxf_path.write_text("".join(out), encoding="utf-8")


def segments_from_dwg(path: pathlib.Path) -> list[tuple[float, float, float, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / (path.stem + ".dxf")
        result = subprocess.run(
            ["dwg2dxf", "-y", "-o", str(out), str(path)],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0 or not out.exists():
            return []
        _strip_sortentstable(out)
        return segments_from_dxf(out)


def segments_from_cad_ir(path: pathlib.Path) -> list[tuple[float, float, float, float]]:
    import json

    ir = json.loads(path.read_text())
    segments = []
    for e in ir.get("entities", []):
        if e.get("type") == "segment":
            segments.append((e["p1"]["x"], e["p1"]["y"], e["p2"]["x"], e["p2"]["y"]))
        elif e.get("type") == "polyline":
            pts = [(p["x"], p["y"]) for p in e.get("points", [])]
            for a, b in zip(pts, pts[1:]):
                segments.append((a[0], a[1], b[0], b[1]))
    return segments


def normalize_to_sheet(
    segments: list[tuple[float, float, float, float]], long_side: int, rng
) -> list[tuple[float, float, float, float]]:
    """Scale model/px space onto a raster sheet with the long side given,
    y-down. Degenerate/empty inputs return []."""
    xs = [v for s in segments for v in (s[0], s[2])]
    ys = [v for s in segments for v in (s[1], s[3])]
    if not xs:
        return []
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w < 1e-6 or h < 1e-6:
        return []
    scale = (long_side - 40) / max(w, h)
    x0, y1 = min(xs), max(ys)
    return [
        (
            (s[0] - x0) * scale + 20,
            (y1 - s[1]) * scale + 20,  # flip y: DXF is y-up, raster y-down
            (s[2] - x0) * scale + 20,
            (y1 - s[3]) * scale + 20,
        )
        for s in segments
    ]


# ── photographic-ink degradation (post-dewarp realism) ───────────────────────


def render_degraded(
    segments: list[tuple[float, float, float, float]], size: tuple[int, int], rng
) -> tuple[np.ndarray, list[tuple[float, float, float, float, float]]]:
    """Draw the segments as degraded ink; return (grayscale image white=paper,
    per-segment (x1,y1,x2,y2,width) with the width actually drawn)."""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = size
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    truth: list[tuple[float, float, float, float, float]] = []
    for x1, y1, x2, y2 in segments:
        width = float(rng.choice([1, 1, 2, 2, 3, 4]))
        # wavy stroke: draw in short sub-strokes with point jitter
        length = float(np.hypot(x2 - x1, y2 - y1))
        if length < 2:
            continue
        n = max(int(length / 12), 1)
        jitter = rng.uniform(0.0, 1.2)
        prev = (x1 + rng.normal(0, jitter), y1 + rng.normal(0, jitter))
        for i in range(1, n + 1):
            t = i / n
            nx = x1 + (x2 - x1) * t + (rng.normal(0, jitter) if i < n else 0)
            ny = y1 + (y2 - y1) * t + (rng.normal(0, jitter) if i < n else 0)
            if rng.random() > 0.06:  # 6% sub-stroke dropout → broken strokes
                draw.line([prev, (nx, ny)], fill=0, width=int(width))
            prev = (nx, ny)
        truth.append((x1, y1, x2, y2, width))
    # salt noise (binarization speckle)
    arr = np.asarray(img, dtype=np.uint8).copy()
    speck = rng.random(arr.shape) < 0.0015
    arr[speck] = 0
    # slight blur + re-threshold (photo softness surviving binarization)
    img = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(radius=float(rng.uniform(0.0, 0.7))))
    arr = np.asarray(img)
    arr = np.where(arr < 200, 0, 255).astype(np.uint8)
    return arr, truth


# ── patching with targets ────────────────────────────────────────────────────


def _clip_segment(x1, y1, x2, y2, x0, y0, size):
    """Liang-Barsky clip of a segment to the [x0,x0+size)×[y0,y0+size) patch;
    returns patch-local coordinates or None."""
    dx, dy = x2 - x1, y2 - y1
    p = [-dx, dx, -dy, dy]
    q = [x1 - x0, x0 + size - x1, y1 - y0, y0 + size - y1]
    t0, t1 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-12:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t1 - t0 < 1e-6:
        return None
    return (
        x1 + t0 * dx - x0, y1 + t0 * dy - y0,
        x1 + t1 * dx - x0, y1 + t1 * dy - y0,
    )


def make_patches(arr: np.ndarray, truth, rng):
    """Slice the sheet into 64×64 patches; keep inked patches with ≤MAX_LINES
    ground-truth lines. Returns (images [N,3,64,64] float32, targets
    [N,10,6] float32) in the serve/_preprocess_patches convention."""
    h, w = arr.shape
    images, targets = [], []
    for y0 in range(0, h - PATCH + 1, PATCH):
        for x0 in range(0, w - PATCH + 1, PATCH):
            tile = arr[y0:y0 + PATCH, x0:x0 + PATCH]
            ink_fraction = float((tile < 128).mean())
            if ink_fraction < 0.002:
                # keep a few empty patches so the model learns to stay silent
                if rng.random() > 0.03:
                    continue
            lines = []
            for x1, y1, x2, y2, width in truth:
                clipped = _clip_segment(x1, y1, x2, y2, x0, y0, PATCH)
                if clipped is None:
                    continue
                cx1, cy1, cx2, cy2 = clipped
                if np.hypot(cx2 - cx1, cy2 - cy1) < 2.0:
                    continue
                lines.append((cx1, cy1, cx2, cy2, width))
            if len(lines) > MAX_LINES:
                continue  # denser than the model's output budget — skip
            image = tile.astype(np.float32) / 255.0
            inv = 1.0 - image
            mask = (inv > 0).astype(np.float32)
            xs = (np.arange(1, PATCH + 1, dtype=np.float32)[None].repeat(PATCH, 0) / PATCH)
            ys = (np.arange(1, PATCH + 1, dtype=np.float32)[:, None].repeat(PATCH, 1) / PATCH)
            images.append(np.stack([inv, xs * mask, ys * mask], axis=0))
            target = np.zeros((MAX_LINES, TARGET_LEN), dtype=np.float32)
            for i, (cx1, cy1, cx2, cy2, width) in enumerate(sorted(
                lines, key=lambda l: -np.hypot(l[2] - l[0], l[3] - l[1])
            )):
                target[i] = (cx1 / PATCH, cy1 / PATCH, cx2 / PATCH, cy2 / PATCH, width / PATCH, 1.0)
            targets.append(target)
    if not images:
        return np.zeros((0, 3, PATCH, PATCH), np.float32), np.zeros((0, MAX_LINES, TARGET_LEN), np.float32)
    return np.stack(images), np.stack(targets)


def save_preprocessed(out_dir: pathlib.Path, images: np.ndarray, targets: np.ndarray) -> None:
    """Write the vendored PreprocessedDataset layout (meta.pkl + float32
    little-endian storages readable via torch.FloatStorage.from_file)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "samples_n": int(images.shape[0]),
        "patch_height": PATCH,
        "patch_width": PATCH,
        "target_shape": [MAX_LINES, TARGET_LEN],
    }
    with open(out_dir / "meta.pkl", "wb") as f:
        pickle.dump(meta, f)
    images.astype("<f4").tofile(out_dir / "images.bin")
    targets.astype("<f4").tofile(out_dir / "targets.bin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dwg-dir", default="", help="directory with .dwg ground truth")
    parser.add_argument("--dxf-dir", default="", help="directory with .dxf ground truth")
    parser.add_argument("--ir-dir", default="", help="directory with accepted CAD IR *.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--variants", type=int, default=4, help="degraded renders per sheet")
    parser.add_argument("--long-side", type=int, default=1600)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    sources: list[tuple[str, list]] = []
    if args.dwg_dir:
        for path in sorted(pathlib.Path(args.dwg_dir).glob("*.dwg")):
            segs = segments_from_dwg(path)
            if segs:
                sources.append((path.name, segs))
    if args.dxf_dir:
        for path in sorted(pathlib.Path(args.dxf_dir).glob("*.dxf")):
            segs = segments_from_dxf(path)
            if segs:
                sources.append((path.name, segs))
    if args.ir_dir:
        for path in sorted(pathlib.Path(args.ir_dir).glob("*.json")):
            segs = segments_from_cad_ir(path)
            if segs:
                sources.append((path.name, segs))
    if not sources:
        print("no ground-truth sources found", file=sys.stderr)
        return 1

    all_images, all_targets = [], []
    for name, raw in sources:
        for variant in range(args.variants):
            segs = normalize_to_sheet(raw, args.long_side, rng)
            if not segs:
                continue
            size = (args.long_side, int(args.long_side * 0.72))
            arr, truth = render_degraded(segs, size, rng)
            images, targets = make_patches(arr, truth, rng)
            all_images.append(images)
            all_targets.append(targets)
            print(f"{name} v{variant}: {images.shape[0]} patches")
    images = np.concatenate(all_images)
    targets = np.concatenate(all_targets)
    # shuffle once so train/val splits are homogeneous
    order = rng.permutation(images.shape[0])
    images, targets = images[order], targets[order]
    save_preprocessed(pathlib.Path(args.out), images, targets)
    filled = (targets[..., 5] > 0).sum(axis=1)
    print(f"TOTAL {images.shape[0]} patches; lines/patch mean {filled.mean():.2f}, "
          f"empty {(filled == 0).mean():.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
