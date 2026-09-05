"""Sheet layout: the drawing versus the paperwork around it."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.ai.cad_recognize.sheet_layout import detect_sheet_layout


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _sheet(width=1200, height=850, *, stamp_rulings=True, notes=True):
    canvas = np.full((height, width), 255, np.uint8)
    cv2.rectangle(canvas, (30, 30), (width - 30, height - 30), 0, 3)  # ГОСТ frame
    cv2.circle(canvas, (380, 380), 200, 0, 4)  # a view
    cv2.line(canvas, (180, 640), (580, 640), 0, 1)  # its dimension
    image = Image.fromarray(canvas).convert("RGB")
    draw = ImageDraw.Draw(image)

    stamp_x0, stamp_y0 = width - 430, height - 190
    draw.rectangle([stamp_x0, stamp_y0, width - 32, height - 32], outline=0, width=3)
    if stamp_rulings:
        for offset in (40, 80, 120):
            draw.line([stamp_x0, stamp_y0 + offset, width - 32, stamp_y0 + offset], fill=0, width=2)
        for offset in (120, 240):
            draw.line(
                [stamp_x0 + offset, stamp_y0, stamp_x0 + offset, height - 32], fill=0, width=2
            )
    else:
        draw.rectangle([stamp_x0 + 4, stamp_y0 + 4, width - 36, height - 36], fill=210)

    if notes:
        font = _font(22)
        for index, line in enumerate(
            [
                "1. Острые кромки притупить.",
                "2. Точность конуса AT6.",
                "3. Неуказанные отклонения H14.",
            ]
        ):
            draw.text((680, 300 + index * 34), line, fill=0, font=font)
    return image


def test_frame_and_title_block_are_measured():
    layout = detect_sheet_layout(_sheet())

    assert layout.frame is not None
    assert layout.frame.width > 1000 and layout.frame.height > 700
    assert layout.title_block is not None
    # The stamp sits in the bottom-right corner and is a modest part of the sheet.
    assert layout.title_block.x1 >= layout.frame.x1 - 40
    assert layout.title_block.y1 >= layout.frame.y1 - 40
    assert layout.title_block.width < layout.frame.width * 0.6


def test_title_block_without_rulings_is_still_found():
    """Not every sheet draws the ГОСТ grid — an exported one may be a panel.

    The ruling search returns a sliver there, which is worse than nothing: a
    stamp reported as 5 px wide leaves the stamp inside the view crop.
    """
    layout = detect_sheet_layout(_sheet(stamp_rulings=False))

    assert layout.title_block is not None
    assert layout.title_block.width > 100, layout.title_block


def test_notes_block_is_text_without_strokes():
    """The requirements column is found by what it is, not where it sits.

    On a real sheet it is left of the stamp rather than above it, so a
    position rule put the box in the middle of the drawing. What actually
    separates it from a view is that it carries lettering and no long strokes.
    """
    layout = detect_sheet_layout(_sheet())

    assert layout.notes_column is not None
    # It must land on the lettering, not on the circle view beside it.
    assert layout.notes_column.x0 > 600, layout.notes_column
    assert layout.notes_column.height >= 60, layout.notes_column


def test_sheet_without_notes_reports_none():
    layout = detect_sheet_layout(_sheet(notes=False))

    assert layout.notes_column is None


def test_layout_serializes_for_the_pipeline_manifest():
    payload = detect_sheet_layout(_sheet()).to_dict()

    assert set(payload) >= {"frame", "title_block", "notes_column", "views_region"}
    assert payload["frame"]["x1"] > payload["frame"]["x0"]
