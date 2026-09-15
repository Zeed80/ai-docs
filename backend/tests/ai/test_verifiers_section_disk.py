"""Сечения вала на листе: сплошной круг против кольца (полость)."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.section_disk import section_disks

PX = 10.0  # px/мм
VIEW = (100.0, 100.0, 1900.0, 500.0)  # главный вид сверху, сечения — ниже


def _hatched(draw, cx, cy, r, *, hole: float = 0.0) -> None:
    """Круг с контуром основной линией и штриховкой 45°; ``hole`` — радиус полости."""
    for offset in range(-2 * int(r), 2 * int(r), 12):
        # Штриховка: отрезок y = x + offset внутри круга (и вне полости).
        for x in range(int(cx - r), int(cx + r)):
            y = cy + (x - cx) + offset
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d < r and d > hole:
                draw.point((x, y), fill=0)
                draw.point((x + 1, y), fill=0)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=6)
    if hole:
        draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], outline=0, width=6)


def _sheet(*, hole: float = 0.0) -> np.ndarray:
    image = Image.new("L", (2000, 1400), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle(VIEW, outline=0, width=6)
    _hatched(draw, 600, 950, 15.0 * PX, hole=hole)  # сечение Ø30
    return np.asarray(image)


def test_a_fully_hatched_section_is_a_solid_shaft():
    disks = section_disks(_sheet(), VIEW, 1.0 / PX, [30.0, 22.0, 35.0])

    assert len(disks) == 1, disks
    assert disks[0]["step_diameter_mm"] == 30.0
    assert disks[0]["solid"] and not disks[0]["ring"], disks[0]


def test_a_hatched_ring_is_a_hollow_shaft():
    disks = section_disks(_sheet(hole=9.0 * PX), VIEW, 1.0 / PX, [30.0, 22.0, 35.0])

    assert len(disks) == 1, disks
    assert disks[0]["ring"] and not disks[0]["solid"], disks[0]


def test_a_circle_not_matching_any_step_is_not_a_section():
    assert section_disks(_sheet(), VIEW, 1.0 / PX, [50.0, 60.0]) == []
