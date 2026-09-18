"""Листовая деталь без верстака (X4, E14): сечение с гибами, выдавленное на ширину.

Живое ядро (2026-09-18): уголок, швеллер, Z-профиль и короб — объём совпал с
формулой до сотых, развёртка по нейтральному слою с 3D — до 0,0000 мм.
"""

from __future__ import annotations

import math

import pytest

from app.ai.sheet_metal import bent_section, developed_length, section_area


def _polygon_area(sketch: list[dict]) -> tuple[float, tuple[float, float]]:
    points = [(0.0, 0.0)]
    x0, y0 = 0.0, 0.0
    for segment in sketch:
        x1, y1 = segment["to"]
        if segment["kind"] == "arc":
            cx, cy = segment["center"]
            radius = math.hypot(x0 - cx, y0 - cy)
            a0 = math.atan2(y0 - cy, x0 - cx)
            sweep = math.atan2(y1 - cy, x1 - cx) - a0
            if segment["clockwise"]:
                while sweep >= 0:
                    sweep -= 2 * math.pi
            else:
                while sweep <= 0:
                    sweep += 2 * math.pi
            for step in range(1, 400):
                angle = a0 + sweep * step / 400
                points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        points.append((x1, y1))
        x0, y0 = x1, y1
    area = (
        abs(
            sum(
                points[i][0] * points[i + 1][1] - points[i + 1][0] * points[i][1]
                for i in range(len(points) - 1)
            )
        )
        / 2.0
    )
    return area, points[-1]


@pytest.mark.parametrize(
    ("flanges", "turns"),
    [
        ([40.0, 30.0], [1]),
        ([40.0, 30.0], [-1]),
        ([30.0, 50.0, 30.0], [1, -1]),
        ([20.0, 40.0, 60.0, 40.0], [1, 1, 1]),
    ],
)
def test_the_section_closes_and_its_area_is_the_formula(flanges, turns):
    sketch = bent_section(flanges, turns, 3.0, 2.0)

    area, last = _polygon_area(sketch)

    assert last == (0.0, 0.0)
    assert abs(area - section_area(flanges, len(turns), 3.0, 2.0)) <= 0.01


def test_the_developed_length_follows_the_neutral_layer():
    # Уголок 40 + 30, R3, t2: дуга по нейтральному слою K = 0,5 — π/2 · 4.
    assert abs(developed_length([40.0, 30.0], 1, 3.0, 2.0, 0.5) - (70.0 + math.pi * 2.0)) < 1e-9
    # Меньший K — меньшая дуга: K = 0,33 (часто для гиба R ≈ t).
    assert developed_length([40.0, 30.0], 1, 3.0, 2.0, 0.33) < developed_length(
        [40.0, 30.0], 1, 3.0, 2.0, 0.5
    )


def test_a_malformed_section_is_refused():
    with pytest.raises(ValueError, match="гибов"):
        bent_section([40.0, 30.0], [], 3.0, 2.0)
    with pytest.raises(ValueError, match="положительны"):
        bent_section([40.0, 0.0], [1], 3.0, 2.0)
