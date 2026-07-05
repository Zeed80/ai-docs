"""Tiled HD diffusion for cleanup — "Максимальное качество" mode.

Why tiles: the model's resolution buckets cap a whole sheet at ~1MP, so
small dimension text lands at 8-15px — below what the VAE can render
legibly (8x latent compression). Upscaling the sheet 2x and running
diffusion per ~1024px tile makes every glyph and thin line 2x bigger for
the model; stitched back, the sheet gets detail no single pass can produce.
Cost: N diffusions per sheet (minutes each) — an explicit opt-in.

This module holds the pure geometry/blending parts (unit-testable without a
GPU); the ComfyUI orchestration lives in tasks/image_generation.py.
"""

from __future__ import annotations

TILE_SIDE = 1024
TILE_OVERLAP = 160


def split_tiles(width: int, height: int, tile: int = TILE_SIDE,
                overlap: int = TILE_OVERLAP) -> list[tuple[int, int, int, int]]:
    """Cover (width, height) with equal tiles of ``tile`` px overlapping by
    ``overlap``. Edge tiles are shifted inwards (never resized), so every
    tile is exactly tile×tile as long as the image is at least that big."""
    def steps(total: int) -> list[int]:
        if total <= tile:
            return [0]
        stride = tile - overlap
        out = list(range(0, total - tile, stride))
        out.append(total - tile)
        return out

    return [
        (x, y, min(x + tile, width), min(y + tile, height))
        for y in steps(height) for x in steps(width)
    ]


def stitch_tiles(width: int, height: int,
                 tiles: list[tuple[tuple[int, int, int, int], "object"]]):
    """Blend rendered tiles back into a full canvas. Per-pixel weights ramp
    linearly to zero towards each tile edge that has a neighbour, so seams
    dissolve inside the overlap zone."""
    import numpy as np
    from PIL import Image

    acc = np.zeros((height, width, 3), dtype=np.float32)
    weight = np.zeros((height, width, 1), dtype=np.float32)

    for (x0, y0, x1, y1), img in tiles:
        tw, th = x1 - x0, y1 - y0
        arr = np.asarray(img.convert("RGB").resize((tw, th)), dtype=np.float32)
        w = _tile_weight(tw, th, x0 > 0, y0 > 0, x1 < width, y1 < height)
        acc[y0:y1, x0:x1] += arr * w
        weight[y0:y1, x0:x1] += w

    weight[weight == 0] = 1.0
    return Image.fromarray(np.clip(acc / weight, 0, 255).astype("uint8"))


def _tile_weight(w: int, h: int, fade_left: bool, fade_top: bool,
                 fade_right: bool, fade_bottom: bool):
    import numpy as np

    ramp = TILE_OVERLAP
    wx = np.ones(w, dtype=np.float32)
    if fade_left:
        n = min(ramp, w)
        wx[:n] = np.minimum(wx[:n], np.linspace(0.0, 1.0, n, dtype=np.float32))
    if fade_right:
        n = min(ramp, w)
        wx[-n:] = np.minimum(wx[-n:], np.linspace(1.0, 0.0, n, dtype=np.float32))
    wy = np.ones(h, dtype=np.float32)
    if fade_top:
        n = min(ramp, h)
        wy[:n] = np.minimum(wy[:n], np.linspace(0.0, 1.0, n, dtype=np.float32))
    if fade_bottom:
        n = min(ramp, h)
        wy[-n:] = np.minimum(wy[-n:], np.linspace(1.0, 0.0, n, dtype=np.float32))
    # Keep a small floor so fully-faded corners still contribute where no
    # other tile covers (shifted edge tiles overlap irregularly).
    grid = np.outer(wy, wx)[..., None] + 1e-4
    return grid
