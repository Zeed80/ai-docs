"""Прямоугольный элемент грани корпуса: контур, а не клетка из чужих линий (Ф5)."""

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.wall_feature import measure_wall_feature

MM_PER_PX = 0.2  # 5 px на мм
BODY = (100.0, 100.0, 400.0, 400.0)  # грань 60 × 60 мм
FACE = (60.0, 60.0)
POCKET = {"profile": "rectangle", "on_plane": "top", "width_mm": 15.0, "height_mm": 15.0}


def _sheet() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("L", (500, 500), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle(BODY, outline=0, width=4)
    return image, draw


def test_a_cell_of_through_lines_is_not_the_pocket():
    # Две горизонтали и две вертикали через весь вид с шагом 15 мм — клетка
    # нужного размера, но стороны идут насквозь (корпус seed 4).
    image, draw = _sheet()
    for x in (200, 275):
        draw.line((x, 100, x, 400), fill=0, width=2)
    for y in (180, 255):
        draw.line((100, y, 400, y), fill=0, width=2)
    measured = measure_wall_feature(
        np.asarray(image),
        BODY,
        MM_PER_PX,
        FACE,
        {**POCKET, "center_u_mm": -3.0, "center_v_mm": 7.0},
    )
    assert measured is None


def test_a_closed_outline_is_found_even_when_one_side_is_extended():
    # Настоящий карман: контур кончается в углах; одну сторону продолжает
    # выносная размера — это не делает его клеткой.
    image, draw = _sheet()
    draw.rectangle((200, 180, 275, 255), outline=0, width=4)
    draw.line((200, 255, 200, 400), fill=0, width=1)
    measured = measure_wall_feature(
        np.asarray(image),
        BODY,
        MM_PER_PX,
        FACE,
        {**POCKET, "center_u_mm": -3.0, "center_v_mm": 7.0},
    )
    assert measured is not None
    assert abs(measured["center_u_mm"] - (-2.5)) < 1.0
    assert abs(measured["center_v_mm"] - 6.5) < 1.0
