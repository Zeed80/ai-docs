"""Контур фланца по виду с торца: круг с лысками проверяется покрытием."""

from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.flange_outline import outline_sketch, propose_flange_outline

PX = 30.0
CENTRE = (1100.0, 1000.0)
LINE = 7


def _outline_points(radius, flats, distance, phase_deg, n=2000):
    points = []
    for k in range(n):
        t = 2 * math.pi * k / n
        reach = radius
        for j in range(flats):
            c = math.cos(t - math.radians(phase_deg + 360.0 * j / flats))
            if c > 1e-9:
                reach = min(reach, distance / c)
        points.append((CENTRE[0] + reach * PX * math.cos(t), CENTRE[1] - reach * PX * math.sin(t)))
    return points


def _end_view(flats=3, distance=9.5, phase=90.0, diameter=29.0) -> np.ndarray:
    image = Image.new("L", (2200, 2000), 255)
    draw = ImageDraw.Draw(image)
    points = _outline_points(diameter / 2, flats, distance, phase)
    draw.line(points + points[:1], fill=0, width=LINE, joint="curve")
    for d in (15.0, 13.0, 11.0):  # ступица и расточка
        r = d / 2 * PX
        draw.ellipse(
            [CENTRE[0] - r, CENTRE[1] - r, CENTRE[0] + r, CENTRE[1] + r], outline=0, width=LINE
        )
    for k in range(3):  # отверстия Ø2,5 на Ø22
        a = math.radians(90 + 120 * k + 60)
        x, y = CENTRE[0] + 11 * PX * math.cos(a), CENTRE[1] - 11 * PX * math.sin(a)
        r = 1.25 * PX
        draw.ellipse([x - r, y - r, x + r, y + r], outline=0, width=LINE)
    # Тонкие осевые и размерная линия, пересекающие контур.
    draw.line([(CENTRE[0] - 600, CENTRE[1]), (CENTRE[0] + 600, CENTRE[1])], fill=0, width=2)
    draw.line([(CENTRE[0], CENTRE[1] - 600), (CENTRE[0], CENTRE[1] + 600)], fill=0, width=2)
    draw.line(
        [(CENTRE[0] + 700, CENTRE[1] - 290), (CENTRE[0] + 700, CENTRE[1] + 440)], fill=0, width=2
    )
    return np.asarray(image)


SPEC = {
    "dimensions": [
        {"value": v} for v in ("Ø22±0,1", "Ø29", "Ø15", "Ø13", "Ø11", "Ø2,5", "24", "4", "2", "6")
    ]
}


def test_a_circle_with_three_flats_is_confirmed_by_the_sheet():
    outline, why = propose_flange_outline(_end_view(), SPEC)

    assert outline is not None, why
    assert (outline.diameter_mm, outline.flats, outline.flat_distance_mm) == (29.0, 3, 9.5)
    assert abs(outline.phase_deg - 90.0) <= 1.0
    assert abs(outline.px_per_mm - PX) / PX < 0.01
    assert outline.coverage >= 0.9 and outline.contrast >= 0.5


def test_a_round_flange_has_no_flats():
    outline, why = propose_flange_outline(_end_view(flats=0, distance=None), SPEC)

    assert outline is not None, why
    assert (outline.diameter_mm, outline.flats) == (29.0, 0)


def test_a_sheet_without_concentric_circles_gives_no_outline():
    image = np.full((1200, 1200), 255, np.uint8)
    image[300:307, 200:1000] = 0

    outline, why = propose_flange_outline(image, SPEC)

    assert outline is None and why


@pytest.mark.parametrize("flats, distance", [(3, 9.5), (4, 12.0), (2, 10.0), (0, None)])
def test_the_outline_sketch_closes_and_matches_the_area(flats, distance):
    segments, origin = outline_sketch(14.5, flats, distance, 90.0)

    assert segments[-1]["to"] == (0.0, 0.0)
    from app.ai.cad_dimension_graph import sketch_outline

    polygon = sketch_outline(segments, step_deg=0.5)
    area = 0.5 * abs(
        sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1]))
    )
    points = _outline_points(14.5, flats, distance or 0.0, 90.0, n=4000)
    mm = [((x - CENTRE[0]) / PX, (CENTRE[1] - y) / PX) for x, y in points]
    expected = 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(mm, mm[1:] + mm[:1])))
    assert abs(area - expected) / expected < 0.01
