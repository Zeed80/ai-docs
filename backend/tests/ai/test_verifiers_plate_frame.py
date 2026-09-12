"""Система координат плана пластины по самому листу."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import ViewFrame
from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame

# План 80×50 мм: 400 px по x (5 px/мм), 240 px по y — выпрямленное фото
# сжимает оси по-разному. Левый нижний угол — (300, 700).
LEFT, BOTTOM, RIGHT, TOP = 300, 700, 700, 460
MAIN, THIN = 4, 2


def _sheet(*, with_decoys: bool = True) -> np.ndarray:
    image = Image.new("L", (1400, 1000), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle([LEFT, TOP, RIGHT, BOTTOM], outline=0, width=MAIN)
    if with_decoys:
        # Размер ширины над планом и выносные, продолжающие кромки: вместе с
        # верхней кромкой — прямоугольник ТОЧНО 80:50 из тонких линий
        # (plate-8 корпуса v7), тогда как сам план из-за сжатия — 80:48.
        dim_y = TOP - 250
        draw.line([(LEFT, dim_y), (RIGHT, dim_y)], fill=0, width=THIN)
        draw.line([(LEFT, dim_y - 10), (LEFT, TOP)], fill=0, width=THIN)
        draw.line([(RIGHT, dim_y - 10), (RIGHT, TOP)], fill=0, width=THIN)
        for tip, sign in ((LEFT, 1), (RIGHT, -1)):
            draw.polygon(
                [(tip, dim_y), (tip + sign * 18, dim_y - 3), (tip + sign * 18, dim_y + 3)], fill=0
            )
        # Вид сбоку на тех же строках и крошечный прямоугольник 80:50 из
        # основных линий (штрихи подписи при 75 dpi — plate-19).
        draw.rectangle([800, TOP, 840, BOTTOM], outline=0, width=MAIN)
        draw.rectangle([1000, 100, 1032, 120], outline=0, width=MAIN)
    return np.asarray(image)


def test_the_plan_is_found_with_a_scale_per_axis():
    frame = locate_plate_frame(_sheet(with_decoys=False), 80.0, 50.0)

    # PIL кладёт обводку внутрь рамки: середины линий — на (MAIN − 1)/2 от краёв.
    inset = (MAIN - 1) / 2
    assert frame is not None
    assert abs(frame.origin_px[0] - (LEFT + inset)) <= 0.5
    assert abs(frame.origin_px[1] - (BOTTOM - inset)) <= 0.5
    assert abs(frame.mm_per_px - 80.0 / (RIGHT - LEFT - 2 * inset)) <= 1e-3
    assert abs(frame.scale_v - 50.0 / (BOTTOM - TOP - 2 * inset)) <= 1e-3


def test_thin_dimension_lines_in_the_plan_proportion_do_not_win_over_the_contour():
    """Пропорция — только фильтр; контур детали отличает основная линия."""
    frame = locate_plate_frame(_sheet(), 80.0, 50.0)

    assert frame is not None
    assert abs(frame.origin_px[1] - BOTTOM) <= 3, frame
    assert abs(frame.to_px(0.0, 50.0)[1] - TOP) <= 3, frame


def test_no_rectangle_of_the_plan_proportion_gives_none_not_a_guess():
    image = Image.new("L", (800, 600), 255)
    ImageDraw.Draw(image).ellipse([200, 200, 400, 400], outline=0, width=4)

    assert locate_plate_frame(np.asarray(image), 80.0, 50.0) is None


def test_an_anisotropic_view_frame_round_trips_each_axis_with_its_own_scale():
    frame = ViewFrame(
        bbox_px=(0, 0, 1000, 1000), mm_per_px=0.2, origin_px=(100.0, 900.0), mm_per_px_v=0.25
    )

    assert frame.to_px(20.0, 50.0) == (200.0, 700.0)
    assert frame.to_mm(200.0, 700.0) == (20.0, 50.0)
    assert abs(frame.scale_mean - (0.2 * 0.25) ** 0.5) < 1e-12
