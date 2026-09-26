"""Полупрофиль тела вращения по изображению разреза (этап D1, прототип)."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_views.revolve_profile import half_profile

PX, AXIS, X0 = 10.0, 300, 100
# Полая деталь: Ø30×20, Ø40×30 (расточка Ø20 насквозь), штриховка стенок.
STEPS = [(30.0, 20.0), (40.0, 30.0)]
BORE = 20.0


def _section() -> np.ndarray:
    image = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(image)
    x = X0
    for diameter, length in STEPS:
        r, xe = diameter / 2 * PX, x + length * PX
        for sign in (-1, 1):
            draw.line([(x, AXIS + sign * r), (xe, AXIS + sign * r)], fill=0, width=6)
            for hx in range(int(x), int(xe), 24):  # штриховка 45° в стенке
                draw.line(
                    [(hx, AXIS + sign * r), (hx + 20, AXIS + sign * (BORE / 2 * PX))],
                    fill=0,
                    width=2,
                )
        draw.line([(x, AXIS - r), (x, AXIS + r)], fill=0, width=6)
        x = xe
    draw.line(
        [(x, AXIS - STEPS[-1][0] / 2 * PX), (x, AXIS + STEPS[-1][0] / 2 * PX)], fill=0, width=6
    )
    for sign in (-1, 1):
        draw.line(
            [(X0, AXIS + sign * BORE / 2 * PX), (x, AXIS + sign * BORE / 2 * PX)], fill=0, width=6
        )
    return np.asarray(image)


def test_outer_steps_bore_and_axis_are_read_from_a_hatched_section():
    profile = half_profile(_section())

    assert profile is not None
    assert abs(profile.axis_y - AXIS) <= 2
    assert abs((profile.x1 - profile.x0) / PX - 50.0) <= 1.0
    radii = sorted({round(2 * r / PX) for _x, r in profile.outer})
    assert 30 in radii and 40 in radii, radii
    assert {round(2 * r / PX) for _x, r in profile.inner} == {20}
