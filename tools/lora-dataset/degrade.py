#!/usr/bin/env python3
"""Simulate a phone photo of a printed drawing, then run the PRODUCTION
pre-diffusion enhancement over it — producing the control image for one
cleanup-LoRA training pair.

Design note (why production enhancement is simulated inside dataset
generation): at inference time the studio's cleanup task never feeds the raw
photo to diffusion — it dewarps/deskews/CLAHEs it first (drawing_cleanup.
enhance_source_for_diffusion). Training on raw photos would teach the model
distortions it will never see, and teach it to fight our own dewarp. So a
pair is:

    target  = clean render (render_dwg.py / techdraw synthetic)
    control = unwarp_exact(simulate_photo(target)) + CLAHE

v2 alignment note: v1 ran the PRODUCTION dewarp over the simulated photo and
trusted its detected quad for the crop. The detected quad differs from the
true one by a few percent (perforation bumps, hull smoothing), which put a
small but SYSTEMATIC layout shift into every pair — and the v1 LoRA dutifully
learned to re-layout the sheet, breaking text_preserve's proportional paste
downstream (confirmed live on the v1-ckpt1500 A/B test). v2 unwarps with the
GROUND-TRUTH corners recorded during simulation, so control and target are
pixel-aligned by construction; a tiny RANDOM residual tilt stays in (real
dewarp isn't perfect either) but there is no systematic component for the
model to learn.

Degradations are randomized per seed, modeled on the real photos in
cleanup_test_files/: paper tint+texture, spiral/comb binding, perspective
onto a wooden desk, lighting gradient + soft shadows, defocus/motion blur,
sensor noise, JPEG artifacts.

Usage:
    python3 degrade.py --src <clean png dir> --out <control png dir>
                       [--per-image 3] [--seed 0] [--repo <repo root>]
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys

import cv2
import numpy as np
from PIL import Image


def _paper_tint(img: np.ndarray, rng) -> np.ndarray:
    tint = np.array([
        rng.uniform(0.94, 1.0), rng.uniform(0.93, 0.99), rng.uniform(0.88, 0.97)
    ])
    out = img.astype(np.float32) * tint
    h, w = img.shape[:2]
    grain = rng.normal(0, rng.uniform(1.0, 3.0), (h // 4, w // 4, 1)).astype(np.float32)
    grain = cv2.resize(grain, (w, h))[..., None]
    return np.clip(out + grain, 0, 255).astype(np.uint8)


def _add_binding(img: np.ndarray, rng) -> np.ndarray:
    """Spiral/comb binding teeth along the top edge, black or beige — both
    exist in the real photo set."""
    if rng.random() > 0.55:
        return img
    h, w = img.shape[:2]
    out = img.copy()
    tooth_w = int(w * rng.uniform(0.02, 0.035))
    tooth_h = int(h * rng.uniform(0.03, 0.05))
    gap = int(tooth_w * rng.uniform(0.6, 1.1))
    y0 = int(h * rng.uniform(0.0, 0.01))
    dark = rng.random() < 0.5
    color = (
        (rng.integers(10, 40),) * 3
        if dark
        else (int(rng.integers(180, 220)), int(rng.integers(160, 200)), int(rng.integers(110, 150)))
    )
    x = int(rng.integers(0, tooth_w + gap))
    while x + tooth_w < w:
        cv2.rectangle(out, (x, y0), (x + tooth_w, y0 + tooth_h), tuple(int(c) for c in color), -1)
        x += tooth_w + gap
    return out


def _wood_background(h: int, w: int, rng) -> np.ndarray:
    base = np.array([rng.integers(150, 190), rng.integers(105, 140), rng.integers(60, 95)])
    bg = np.tile(base.astype(np.float32), (h, w, 1))
    xs = np.arange(w, dtype=np.float32)
    stripes = (
        np.sin(xs / rng.uniform(15, 60) + rng.uniform(0, 6.28)) * rng.uniform(5, 15)
        + np.sin(xs / rng.uniform(120, 400) + rng.uniform(0, 6.28)) * rng.uniform(5, 12)
    )
    bg += stripes[None, :, None]
    bg += rng.normal(0, 3, (h, w, 1))
    return np.clip(bg, 0, 255).astype(np.uint8)


def _perspective_onto_desk(img: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Returns (photo, dst_quad): the warped sheet on a desk AND the ground-
    truth corner positions of the sheet within that photo — v2 uses them to
    unwarp exactly instead of trusting a detected quad."""
    h, w = img.shape[:2]
    margin = 0.10
    out_w, out_h = int(w * (1 + 2 * margin)), int(h * (1 + 2 * margin))
    bg = _wood_background(out_h, out_w, rng)

    def jitter(px, py):
        return (
            px + rng.uniform(-0.06, 0.06) * w + margin * w,
            py + rng.uniform(-0.06, 0.06) * h + margin * h,
        )

    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([jitter(0, 0), jitter(w, 0), jitter(w, h), jitter(0, h)])
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 255, 0))
    mask = (warped[..., 0] == 0) & (warped[..., 1] == 255) & (warped[..., 2] == 0)
    warped[mask] = bg[mask]
    return warped, dst


def _lighting(img: np.ndarray, rng) -> np.ndarray:
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    plane = (
        rng.uniform(0.85, 1.02)
        + (xx / w - 0.5) * rng.uniform(-0.18, 0.18)
        + (yy / h - 0.5) * rng.uniform(-0.18, 0.18)
    )
    for _ in range(rng.integers(0, 3)):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        sigma = rng.uniform(0.2, 0.5) * max(w, h)
        depth = rng.uniform(0.06, 0.16)
        plane -= depth * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2)))
    out = img.astype(np.float32) * plane[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def _blur_noise_jpeg(img: np.ndarray, rng) -> np.ndarray:
    sigma = rng.uniform(0.4, 1.6)
    out = cv2.GaussianBlur(img, (0, 0), sigma)
    if rng.random() < 0.3:
        k = int(rng.integers(3, 8))
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k
        angle = rng.uniform(0, 180)
        rot = cv2.getRotationMatrix2D((k / 2 - 0.5, k / 2 - 0.5), angle, 1.0)
        kernel = cv2.warpAffine(kernel, rot, (k, k))
        kernel /= max(kernel.sum(), 1e-6)
        out = cv2.filter2D(out, -1, kernel)
    out = np.clip(
        out.astype(np.float32) + rng.normal(0, rng.uniform(1.5, 5.0), out.shape), 0, 255
    ).astype(np.uint8)
    quality = int(rng.integers(55, 90))
    ok, enc = cv2.imencode(".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.cvtColor(cv2.imdecode(enc, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def simulate_photo(clean_rgb: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Returns (photo, quad): the degraded photo and the ground-truth sheet
    corners in ITS coordinate space (already rescaled)."""
    img = _paper_tint(clean_rgb, rng)
    img = _add_binding(img, rng)
    img, quad = _perspective_onto_desk(img, rng)
    img = _lighting(img, rng)
    long_side = int(rng.integers(1000, 1700))
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return _blur_noise_jpeg(img, rng), quad * scale


def unwarp_exact(photo: np.ndarray, quad: np.ndarray, out_w: int, out_h: int, rng) -> np.ndarray:
    """Unwarp the sheet back to the target's own frame using the KNOWN
    corners — pixel-aligned with the target by construction. A tiny RANDOM
    corner jitter stays in (the production dewarp isn't perfect either), but
    unlike v1's detected-quad crop it has no systematic component the model
    could learn as \"re-layout the sheet\"."""
    jitter = 0.004 * max(out_w, out_h)
    src = (quad + rng.uniform(-jitter, jitter, quad.shape)).astype(np.float32)
    dst = np.float32([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(photo, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def clahe_like_prod(img: np.ndarray) -> np.ndarray:
    """Mirror enhance_source_for_diffusion's post-dewarp look (median blur +
    CLAHE on L) without importing the backend."""
    img = cv2.medianBlur(img, 3)
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_ch)
    return cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2RGB)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--per-image", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--raw", action="store_true",
                    help="write the raw simulated photo (no unwarp/CLAHE)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    ok = 0
    for path in sorted(args.src.glob("*.png")):
        clean = np.asarray(Image.open(path).convert("RGB"))
        h, w = clean.shape[:2]
        for i in range(args.per_image):
            rng = np.random.default_rng(args.seed + hash(path.stem) % 10_000_000 + i)
            photo, quad = simulate_photo(clean, rng)
            if args.raw:
                out = photo
            else:
                out = clahe_like_prod(unwarp_exact(photo, quad, w, h, rng))
            buf = io.BytesIO()
            Image.fromarray(out).save(buf, format="PNG")
            (args.out / f"{path.stem}__v{i}.png").write_bytes(buf.getvalue())
            ok += 1
        print(f"{path.stem}: done")
    print(f"done: {ok} controls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
