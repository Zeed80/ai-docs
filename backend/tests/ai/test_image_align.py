"""Tests for affine registration of diffusion output (image_align.py).
Synthetic drawings, offline, deterministic."""

from __future__ import annotations

import io

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402
from PIL import Image  # noqa: E402

from app.ai.image_align import align_result_to_source  # noqa: E402


def _drawing(shift=(0, 0), scale=1.0, size=(600, 450)) -> bytes:
    """A recognizable asymmetric line drawing, optionally scaled/shifted —
    plays the roles of both 'source' and 'drifted diffusion output'."""
    w, h = size
    img = np.full((h, w, 3), 255, np.uint8)

    def pt(x, y):
        return (int(x * scale + shift[0]), int(y * scale + shift[1]))

    cv2.rectangle(img, pt(60, 50), pt(520, 380), (0, 0, 0), 2)
    cv2.line(img, pt(60, 200), pt(520, 200), (0, 0, 0), 2)
    cv2.circle(img, pt(180, 120), int(40 * scale), (0, 0, 0), 2)
    cv2.rectangle(img, pt(350, 250), pt(480, 350), (0, 0, 0), 3)
    for x in range(80, 300, 20):
        cv2.line(img, pt(x, 260), pt(x + 30, 360), (0, 0, 0), 1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def _ink_shift(a_png: bytes, b_png: bytes) -> tuple[float, float]:
    a = np.asarray(Image.open(io.BytesIO(a_png)).convert("L"), dtype=np.float32)
    b = np.asarray(Image.open(io.BytesIO(b_png)).convert("L"), dtype=np.float32)
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    (dx, dy), _ = cv2.phaseCorrelate((a < 128).astype(np.float32), (b < 128).astype(np.float32))
    return dx, dy


def test_shifted_output_is_pulled_back():
    source = _drawing()
    drifted = _drawing(shift=(18, -12))
    aligned = align_result_to_source(drifted, source)
    assert aligned != drifted, "alignment should have fired"
    dx, dy = _ink_shift(source, aligned)
    assert abs(dx) < 3 and abs(dy) < 3, f"residual shift too big: {dx:.1f},{dy:.1f}"


def test_scaled_output_is_pulled_back():
    source = _drawing()
    drifted = _drawing(shift=(25, 20), scale=0.92)  # sheet re-laid-out with margins
    aligned = align_result_to_source(drifted, source)
    dx, dy = _ink_shift(source, aligned)
    assert abs(dx) < 4 and abs(dy) < 4, f"residual shift too big: {dx:.1f},{dy:.1f}"


def test_identity_when_already_aligned():
    source = _drawing()
    aligned = align_result_to_source(source, source)
    dx, dy = _ink_shift(source, aligned)
    assert abs(dx) < 1.5 and abs(dy) < 1.5


def test_unrelated_images_keep_original():
    source = _drawing()
    # Blank page with a dot — nothing to correspond to.
    img = np.full((450, 600, 3), 255, np.uint8)
    cv2.circle(img, (300, 225), 4, (0, 0, 0), -1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    unrelated = buf.getvalue()
    out = align_result_to_source(unrelated, source)
    assert out == unrelated, "implausible/failed registration must be a no-op"
