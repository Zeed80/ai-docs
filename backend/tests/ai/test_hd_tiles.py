"""Tests for HD tile splitting/stitching (pure geometry, no GPU)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from app.ai.hd_tiles import TILE_OVERLAP, TILE_SIDE, split_tiles, stitch_tiles  # noqa: E402


def test_split_covers_whole_image_with_overlap():
    boxes = split_tiles(2500, 1800)
    assert all((x1 - x0) == TILE_SIDE and (y1 - y0) == TILE_SIDE for x0, y0, x1, y1 in boxes)
    covered = np.zeros((1800, 2500), dtype=bool)
    for x0, y0, x1, y1 in boxes:
        covered[y0:y1, x0:x1] = True
    assert covered.all(), "tiles must cover every pixel"
    # Neighbouring tiles genuinely overlap.
    xs = sorted({b[0] for b in boxes})
    assert xs[1] - xs[0] == TILE_SIDE - TILE_OVERLAP


def test_split_small_image_single_tile():
    assert split_tiles(800, 600) == [(0, 0, 800, 600)]


def test_stitch_identical_tiles_reproduces_content():
    """If every tile is the exact crop of one source image, stitching must
    reproduce that image (up to blending float error) — the seam blending
    must not invent or shift anything."""
    rng = np.random.default_rng(0)
    src = (rng.random((1400, 2100, 3)) * 255).astype("uint8")
    src_img = Image.fromarray(src)
    tiles = []
    for box in split_tiles(2100, 1400):
        x0, y0, x1, y1 = box
        tiles.append((box, src_img.crop(box)))
    out = np.asarray(stitch_tiles(2100, 1400, tiles), dtype=np.int16)
    diff = np.abs(out - src.astype(np.int16))
    assert diff.max() <= 2, f"stitching altered content (max diff {diff.max()})"
