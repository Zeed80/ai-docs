"""Ф9: вырез листа вокруг элемента, проверенного по листу."""

import io

import pytest
from PIL import Image

from app.api.image_generation import verification_overlay_png


def _sheet() -> bytes:
    image = Image.new("RGB", (1000, 800), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_overlay_crops_around_the_box_and_draws_it():
    png = verification_overlay_png(_sheet(), [400, 300, 500, 360])
    crop = Image.open(io.BytesIO(png)).convert("RGB")
    # Рамка 100 px + по 60 px с каждой стороны.
    assert crop.size == (220, 180)
    assert crop.getpixel((60, 90)) == (220, 38, 38)
    assert crop.getpixel((110, 90)) == (255, 255, 255)


def test_overlay_is_clipped_to_the_sheet():
    png = verification_overlay_png(_sheet(), [0, 0, 20, 20])
    crop = Image.open(io.BytesIO(png))
    assert crop.size == (60, 60)


def test_box_outside_the_sheet_is_refused():
    with pytest.raises(ValueError):
        verification_overlay_png(_sheet(), [2000, 2000, 2100, 2100])
