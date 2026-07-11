"""Title block (основная надпись) geometric presence detection (Ф4.4)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_ir.schema import Point, TextEntity
from app.tasks.cad_trace import _detect_title_block, _extract_stamp_scale


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


# ── _extract_stamp_scale (fix #6: the real producer of title_block["scale"]) ──

_REGION = {"x0": 280, "y0": 255, "x1": 400, "y1": 300}


def test_extracts_scale_with_m_prefix_inside_region():
    entities = [TextEntity(position=Point(x=300, y=270), text="М 1:2", height=4)]
    assert _extract_stamp_scale(entities, _REGION) == "1:2"


def test_extracts_scale_latin_m_and_no_spaces():
    entities = [TextEntity(position=Point(x=300, y=270), text="M1:1", height=4)]
    assert _extract_stamp_scale(entities, _REGION) == "1:1"


def test_ignores_bare_ratio_without_m_prefix():
    """A bare "N:M" without the М/M prefix is NOT trusted as a scale — the
    stamp also carries "Лист 1 Листов 2"-style fields that could coincide
    with a ratio-looking pattern; a wrong guess would just create a false
    ESKD_SCALE_NONSTANDARD warning."""
    entities = [TextEntity(position=Point(x=300, y=270), text="1:2", height=4)]
    assert _extract_stamp_scale(entities, _REGION) is None


def test_ignores_text_outside_the_region():
    entities = [TextEntity(position=Point(x=10, y=10), text="М 1:2", height=4)]
    assert _extract_stamp_scale(entities, _REGION) is None


def test_returns_none_when_no_text_entities():
    assert _extract_stamp_scale([], _REGION) is None


def test_handles_comma_decimal_ratio():
    entities = [TextEntity(position=Point(x=300, y=270), text="М 2,5:1", height=4)]
    assert _extract_stamp_scale(entities, _REGION) == "2.5:1"
