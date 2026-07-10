"""Title block (основная надпись) geometric presence detection (Ф4.4)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.tasks.cad_trace import _detect_title_block


def test_detects_title_block_with_ink_in_bottom_right():
    ink = np.zeros((300, 400), dtype=np.uint8)
    # Draw something in the bottom-right 30%x15% corner (a stamp grid + text stroke).
    cv2.rectangle(ink, (300, 260), (395, 295), 255, 2)
    cv2.line(ink, (310, 275), (380, 275), 255, 1)
    result = _detect_title_block(ink, 400, 300)
    assert result is not None
    assert result["detected"] is True
    assert result["region"] == {"x0": 280, "y0": 255, "x1": 400, "y1": 300}
    assert result["ink_fraction"] > 0


def test_no_title_block_when_corner_is_blank():
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.line(ink, (40, 40), (360, 40), 255, 4)  # ink elsewhere, corner untouched
    assert _detect_title_block(ink, 400, 300) is None
