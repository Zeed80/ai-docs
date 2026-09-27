"""Полупрофиль тела вращения по изображению разреза (этап D1, прототип)."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

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
            wall = r - BORE / 2 * PX
            for hx in range(int(x), int(xe - wall), 24):  # штриховка 45° в стенке
                draw.line(
                    [(hx, AXIS + sign * r), (hx + wall, AXIS + sign * (BORE / 2 * PX))],
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


def test_section_material_gives_outer_and_bore_in_mm_with_both_scales():
    """Материал разреза по штриховке → полупрофиль → масштабы по надписям."""
    from app.ai.cad_views.revolve_body import revolve_points
    from app.ai.cad_views.revolve_profile import fit_axial_scale, fit_scale, profile_from_material
    from app.ai.cad_views.section_material import ink_mask, section_material

    image = _section()
    material, axis = section_material(image, 6.0)
    profile = profile_from_material(material, axis, 6.0, ink=ink_mask(image, 6.0))

    assert profile is not None and abs(axis - AXIS) <= 2
    radial, hits = fit_scale(profile, [30.0, 40.0], [20.0])
    axial, _ = fit_axial_scale(profile, [50.0, 20.0, 30.0], near=radial)
    assert hits >= 2 and abs(radial - 1 / PX) < 0.01 / PX
    assert abs(axial - 1 / PX) < 0.02 / PX
    outer, bore = revolve_points(profile, axial, radial)
    assert abs(outer[-1]["z"] - 50.0) < 0.6
    assert {round(2 * p["r"]) for p in outer} >= {30, 40}
    import statistics

    assert bore and abs(2 * statistics.median(p["r"] for p in bore) - 20.0) < 0.6
