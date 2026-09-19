"""Вид под планом корпуса: разрез или вид спереди (Ф5, E9)."""

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.stage import _hatched

BOX = (100, 100, 700, 500)


def _view() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle(BOX, outline=0, width=4)
    return image, draw


def test_a_front_view_with_a_boss_and_a_diagonal_diameter_is_not_a_section():
    # Корпус seed 6: окружность прилива, размерная Ø под 45° со стрелками —
    # 8–11 коротких штрихов, прежний счёт видел в этом штриховку.
    image, draw = _view()
    draw.ellipse((330, 230, 510, 410), outline=0, width=4)
    draw.line((350, 390, 490, 250), fill=0, width=2)
    for x, y in ((350, 390), (490, 250)):
        draw.polygon([(x, y), (x + 14, y - 4), (x + 4, y - 14)], fill=0)
    draw.ellipse((260, 160, 340, 240), outline=0, width=2)
    assert _hatched(np.asarray(image), BOX) is False


def test_a_hatched_wall_is_a_section():
    image, draw = _view()
    for offset in range(-400, 400, 12):
        draw.line((100 + offset, 500, 500 + offset, 100), fill=0, width=1)
    draw.rectangle((180, 180, 620, 500), fill=255, outline=0, width=4)
    assert _hatched(np.asarray(image), BOX) is True
