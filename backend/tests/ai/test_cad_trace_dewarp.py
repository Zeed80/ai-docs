"""Photo dewarp preprocessing in digitize (reuses drawing_cleanup._dewarp_sheet)."""

from __future__ import annotations

import io

import numpy as np
import pytest

pytest.importorskip("cv2")
pytest.importorskip("PIL")

from PIL import Image

from app.tasks.cad_trace import _dewarp_photo


def _png(arr: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(arr).save(buffer, format="PNG")
    return buffer.getvalue()


def test_dewarp_is_noop_on_a_full_frame_clean_scan():
    # A near-full-frame white sheet has no paper-vs-background quad → unchanged.
    clean = _png(np.full((400, 600, 3), 255, dtype=np.uint8))
    assert _dewarp_photo(clean) == clean


def test_dewarp_crops_a_bright_sheet_on_a_dark_desk():
    # Bright achromatic sheet (~55% area) on a coloured desk → warped/cropped.
    arr = np.zeros((700, 900, 3), dtype=np.uint8)
    arr[..., 0] = 150  # colored (saturated) desk background
    arr[120:560, 200:760] = 245  # near-white paper region
    out = _dewarp_photo(_png(arr))
    assert out != _png(arr)  # something changed
    warped = np.array(Image.open(io.BytesIO(out)).convert("RGB"))
    assert warped.shape[0] < 700 and warped.shape[1] < 900  # cropped to the sheet
