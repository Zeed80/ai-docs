"""Phone-photo simulation for LoRA-dataset controls — v2 exact-alignment
port of tools/lora-dataset/degrade.py (that CLI stays the research tool).

The critical contract: ``unwarp_exact`` uses the GROUND-TRUTH corners
recorded by ``simulate_photo``, so control and target are pixel-aligned by
construction. A random ±0.4% corner jitter stays in (the production dewarp
isn't perfect either) but there is no systematic layout component for the
model to learn (the v1 lesson — see project memory project-lora-v2-and-align).
"""

from __future__ import annotations

import cv2
import numpy as np


def simulate_photo(clean_rgb: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Returns (photo, quad): degraded desk photo + ground-truth sheet
    corners in its coordinate space."""
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
    jitter = 0.004 * max(out_w, out_h)
    src = (quad + rng.uniform(-jitter, jitter, quad.shape)).astype(np.float32)
    dst = np.float32([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(photo, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def post_unwarp_defects(img: np.ndarray, rng) -> np.ndarray:
    """Defects the PRODUCTION dewarp does NOT remove — so they belong in the
    control (the model must learn to fix them), applied AFTER the exact
    unwarp: residual trapezoid (real dewarp corners are a few px off), page
    curl (a bound sheet bulges; planar dewarp can't flatten it) and fold
    creases with their shading. Amplitudes are kept small (~1% of the sheet):
    these are local distortions to learn away, not a systematic layout shift
    (the v1 lesson)."""
    img = _residual_trapezoid(img, rng)
    if rng.random() < 0.45:
        img = _page_curl(img, rng)
    if rng.random() < 0.35:
        img = _fold_crease(img, rng)
    return img


def _residual_trapezoid(img: np.ndarray, rng) -> np.ndarray:
    h, w = img.shape[:2]
    amp = 0.008
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + rng.uniform(-amp, amp, src.shape).astype(np.float32) * np.float32([w, h])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _page_curl(img: np.ndarray, rng) -> np.ndarray:
    """Cylindrical bulge of a bound page: vertical displacement follows a
    half-sine across the sheet + a soft shading band along the curl."""
    h, w = img.shape[:2]
    amp = h * rng.uniform(0.004, 0.012)
    phase = rng.uniform(0, np.pi)
    xs = np.arange(w, dtype=np.float32)
    dy = (amp * np.sin(np.pi * xs / w + phase)).astype(np.float32)
    map_x, map_y = np.meshgrid(xs, np.arange(h, dtype=np.float32))
    map_y = (map_y + dy[None, :]).astype(np.float32)
    out = cv2.remap(img, map_x.astype(np.float32), map_y, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    shade = 1.0 - 0.08 * np.abs(np.sin(np.pi * xs / w + phase))
    return np.clip(out.astype(np.float32) * shade[None, :, None], 0, 255).astype(np.uint8)


def _fold_crease(img: np.ndarray, rng) -> np.ndarray:
    """A crease line: small displacement kink across it + a light/dark
    shading stripe (how a fold actually photographs)."""
    h, w = img.shape[:2]
    angle = rng.uniform(0, np.pi)
    cx, cy = rng.uniform(0.3, 0.7) * w, rng.uniform(0.3, 0.7) * h
    nx, ny = np.cos(angle), np.sin(angle)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = (xs - cx) * nx + (ys - cy) * ny
    kink = np.clip(dist / rng.uniform(30, 80), -1, 1) * rng.uniform(0.5, 2.0)
    map_x = (xs + kink * nx).astype(np.float32)
    map_y = (ys + kink * ny).astype(np.float32)
    out = cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    stripe = 1.0 - rng.uniform(0.05, 0.12) * np.exp(-(dist / rng.uniform(6, 15)) ** 2)
    return np.clip(out.astype(np.float32) * stripe[..., None], 0, 255).astype(np.uint8)


def clahe_like_prod(img: np.ndarray) -> np.ndarray:
    """Mirror enhance_source_for_diffusion's post-dewarp look."""
    img = cv2.medianBlur(img, 3)
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_ch)
    return cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2RGB)


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
    _ok, enc = cv2.imencode(".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.cvtColor(cv2.imdecode(enc, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
