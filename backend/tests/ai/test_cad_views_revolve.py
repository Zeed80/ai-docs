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


def test_fit_scale_counts_distinct_labels_not_plateaus():
    from app.ai.cad_views.revolve_profile import HalfProfile, fit_scale

    # Две шейки одного Ø и третья другого: одна надпись Ø25 не должна
    # «объяснять» две площадки.
    profile = HalfProfile(
        axis_y=0.0,
        line_px=2.0,
        x0=0,
        x1=300,
        outer=[(0, 50), (100, 50), (100, 80), (200, 80), (200, 50), (300, 50)],
    )
    scale, hits = fit_scale(profile, [25.0], [])
    assert scale is not None and hits == 1
    scale, hits = fit_scale(profile, [25.0, 40.0], [])
    assert abs(scale - 0.25) < 1e-6 and hits == 2


def test_fit_scale_near_restricts_candidates():
    from app.ai.cad_views.revolve_profile import HalfProfile, fit_scale

    profile = HalfProfile(
        axis_y=0.0, line_px=2.0, x0=0, x1=200, outer=[(0, 40), (100, 40), (100, 60), (200, 60)]
    )
    # Надписи объясняют обе площадки и при 0,1, и при 0,25 (подобие);
    # габарит задаёт масштаб около 0,25.
    labels = [8.0, 12.0, 20.0, 30.0]
    scale, hits = fit_scale(profile, labels, [], near=0.25)
    assert abs(scale - 0.25) < 1e-6 and hits == 2


def test_inner_cavity_not_open_to_an_end_is_dropped():
    from app.ai.cad_views.revolve_body import accessible_inner
    from app.ai.cad_views.revolve_profile import HalfProfile

    # Сплошной вал: «полость» в середине — местный разрез паза, не расточка;
    # расточка от правого торца — настоящая.
    profile = HalfProfile(
        axis_y=0.0,
        line_px=4.0,
        x0=0,
        x1=400,
        outer=[(0, 60), (400, 60)],
        inner=[(0, 0), (150, 0), (150, 20), (200, 20), (200, 0), (300, 0), (300, 30), (400, 30)],
    )
    inner = accessible_inner(profile)
    assert [r for _x, r in inner[2:4]] == [0.0, 0.0]
    assert [r for _x, r in inner[-2:]] == [30, 30]


def test_same_feature_from_overlapping_views_is_merged():
    from app.ai.cad_views.pipeline import _same_feature

    flat = {
        "kind": "pocket",
        "origin_mm": [6.47, 0.0, 23.75],
        "axis": [-1.0, 0.0, 0.0],
        "width_mm": 10.255,
        "height_mm": 13.944,
        "depth_mm": 3.972,
    }
    assert _same_feature(flat, dict(flat))
    assert not _same_feature(flat, {**flat, "origin_mm": [-6.47, 0.0, 23.75]})
    assert not _same_feature(flat, {**flat, "depth_mm": 2.0})


def test_outline_cells_keep_the_bore_when_a_dimension_line_crosses_the_wall():
    """Размерная линия Ø со стрелками поперёк стенки не рвёт материал: ячейки
    — по основным линиям, расточка остаётся Ø20 (полый вал shaft-2)."""
    from app.ai.cad_views.revolve_profile import fit_scale, profile_from_material
    from app.ai.cad_views.section_material import ink_mask, material_by_outline

    image = Image.fromarray(_section())
    draw = ImageDraw.Draw(image)
    x = X0 + 10 * PX
    draw.line([(x, AXIS - 15 * PX), (x, AXIS + 15 * PX)], fill=0, width=2)
    for sign in (-1, 1):
        tip = AXIS + sign * 15 * PX
        draw.polygon([(x, tip), (x - 5, tip - sign * 25), (x + 5, tip - sign * 25)], fill=0)
    gray = np.asarray(image)
    material = material_by_outline(gray, 6.0)
    profile = profile_from_material(material, AXIS, 6.0, ink=ink_mask(gray, 6.0))
    assert profile is not None
    radial, hits = fit_scale(profile, [30.0, 40.0], [20.0])
    assert hits >= 3 and abs(radial - 1 / PX) < 0.02 / PX
    inner = [r for _x, r in profile.inner if r > 0]
    assert inner and abs(2 * np.median(inner) * radial - 20.0) < 1.0
    assert max(inner) - min(inner) <= 6.0  # одна расточка, без ступеней у стрелок
