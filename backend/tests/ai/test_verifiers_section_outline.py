"""Лыски и радиальные отверстия по контуру сечений (дорожка У, У3).

Синтетическое сечение как на листе 1:4 (≈ 3 px/мм): обводка основной линией,
штриховка 45°, лыска — хорда, радиальное отверстие — канал; поверх — то, что
сбивало замер на корпусе: выносная размера глубины, касательная к исходной
окружности, и тонкая размерная линия поперёк канала.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.ai.cad_recognize.verifiers.section_outline import (
    channel_width,
    flat_line,
    locate_sections,
    verify_placed_on_sections,
)

PX = 3.0  # px/мм
VIEW = (50.0, 50.0, 1150.0, 250.0)  # главный вид; сечения — ниже
STEP = 40.0  # Ø ступени


def _section(
    image: np.ndarray,
    cx: float,
    cy: float,
    *,
    flat: tuple[float, float] | None = None,
    hole: tuple[float, float] | None = None,
) -> None:
    """Сечение Ø40: ``flat`` — (угол, глубина мм), ``hole`` — (угол, Ø мм), сквозное."""
    height, width = image.shape
    yy, xx = np.mgrid[0:height, 0:width]
    u, v = (xx - cx) / PX, -(yy - cy) / PX
    material = u**2 + v**2 <= (STEP / 2.0) ** 2
    if flat:
        angle, depth = flat
        nx, ny = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        material &= u * nx + v * ny <= STEP / 2.0 - depth
    if hole:
        angle, diameter = hole
        nx, ny = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        material &= np.abs(-u * ny + v * nx) >= diameter / 2.0
    mask = material.astype(np.uint8)
    outline = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((5, 5), np.uint8)).astype(bool)
    hatch = ((xx + yy) % 12 < 2) & material
    image[outline | hatch] = 0
    if flat:
        # Выносная размера глубины — касательная к исходной окружности.
        angle, _depth = flat
        nx, ny = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
        tx, ty = -ny, nx
        base = (cx + nx * (STEP / 2.0 + 0.7) * PX, cy + ny * (STEP / 2.0 + 0.7) * PX)
        a = (int(base[0] - tx * 25 * PX), int(base[1] - ty * 25 * PX))
        b = (int(base[0] + tx * 25 * PX), int(base[1] + ty * 25 * PX))
        cv2.line(image, a, b, 0, 2)
    if hole:
        # Тонкая размерная линия Ø поперёк канала.
        angle, _diameter = hole
        nx, ny = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
        tx, ty = -ny, nx
        middle = (cx + nx * 14 * PX, cy + ny * 14 * PX)
        a = (int(middle[0] - tx * 12 * PX + nx * 3), int(middle[1] - ty * 12 * PX + ny * 3))
        b = (int(middle[0] + tx * 12 * PX - nx * 3), int(middle[1] + ty * 12 * PX - ny * 3))
        cv2.line(image, a, b, 0, 1)


def _sheet() -> np.ndarray:
    image = np.full((600, 1200), 255, np.uint8)
    cv2.rectangle(image, (50, 50), (1150, 250), 0, 5)
    _section(image, 300, 420, flat=(90.0, 4.0))
    _section(image, 700, 420, hole=(45.0, 8.0))
    return image


def _body(*features: dict) -> dict:
    return {
        "outer": [{"diameter_mm": 30.0, "length_mm": 50.0}, {"diameter_mm": STEP, "length_mm": 80}],
        "placed_features": list(features),
    }


def _flat(angle: float, depth: float) -> dict:
    a = math.radians(angle)
    r = STEP / 2.0
    return {
        "kind": "pocket",
        "profile": "rectangle",
        "origin_mm": [r * math.cos(a), r * math.sin(a), 70.0],
        "axis": [-math.cos(a), -math.sin(a), 0.0],
        "width_mm": 30.0,
        "height_mm": 20.0,
        "depth_mm": depth,
    }


def _hole(angle: float, diameter: float) -> dict:
    a = math.radians(angle)
    r = STEP / 2.0
    return {
        "kind": "hole",
        "origin_mm": [r * math.cos(a), r * math.sin(a), 100.0],
        "axis": [-math.cos(a), -math.sin(a), 0.0],
        "diameter_mm": diameter,
        "through": True,
    }


def _ink(image: np.ndarray) -> np.ndarray:
    return image < 128


def test_sections_are_located_by_the_step_radius_and_line_middle():
    sections = locate_sections(_ink(_sheet()), VIEW, 1.0 / PX, [30.0, STEP])

    assert len(sections) == 2, sections
    for section, cx in zip(sorted(sections, key=lambda s: s["center_px"][0]), (300, 700)):
        assert section["step_diameter_mm"] == STEP
        assert abs(section["center_px"][0] - cx) < 1.5
        assert abs(section["center_px"][1] - 420) < 1.5
        assert abs(2.0 * section["radius_px"] / PX - STEP) < 0.6


def test_the_flat_is_measured_inside_past_the_tangent_extension_line():
    ink = _ink(_sheet())
    radius = STEP / 2.0 * PX
    chord = flat_line(ink, 300, 420, radius, 90.0, 10.0 * PX, 4.0)

    assert chord is not None
    angle, distance = chord
    assert abs(angle - 90.0) < 2.0
    assert abs((radius - distance) / PX - 4.0) < 0.6


def test_the_hole_is_measured_by_its_walls_across_a_thin_dimension_line():
    width = channel_width(_ink(_sheet()), 700, 420, STEP / 2.0 * PX, 45.0, 4.0)

    assert width is not None
    assert abs(width / PX - 8.0) < 0.6


def test_a_true_reading_is_confirmed():
    items = verify_placed_on_sections(
        _sheet(), VIEW, 1.0 / PX, _body(_flat(90.0, 4.0), _hole(45.0, 8.0))
    )

    assert [item["status"] for item in items] == ["confirmed", "confirmed"], items


def test_a_wrong_size_is_refuted_with_the_measurement():
    items = verify_placed_on_sections(
        _sheet(), VIEW, 1.0 / PX, _body(_flat(90.0, 6.0), _hole(45.0, 10.0))
    )

    assert [item["status"] for item in items] == ["refuted", "refuted"], items
    assert abs(items[0]["measured"]["depth_mm"] - 4.0) < 0.6
    assert abs(items[1]["measured"]["diameter_mm"] - 8.0) < 0.6


def test_a_wrong_angle_nobody_else_explains_is_refuted():
    items = verify_placed_on_sections(_sheet(), VIEW, 1.0 / PX, _body(_hole(75.0, 8.0)))

    assert items[0]["status"] == "refuted", items
    assert abs(items[0]["measured"]["angle_deg"] - 45.0) < 3.0


def test_a_gap_explained_by_another_read_feature_is_another_object():
    """E28: расхождение, указывающее на другой прочитанный элемент, — не измеримо."""
    items = verify_placed_on_sections(
        _sheet(), VIEW, 1.0 / PX, _body(_hole(45.0, 8.0), _hole(80.0, 8.0))
    )

    assert items[0]["status"] == "confirmed", items
    assert items[1]["status"] == "unmeasurable", items


def test_no_section_on_the_sheet_is_unmeasurable():
    image = np.full((600, 1200), 255, np.uint8)
    items = verify_placed_on_sections(image, VIEW, 1.0 / PX, _body(_hole(45.0, 8.0)))

    assert [item["status"] for item in items] == ["unmeasurable"]


def test_a_coarse_sheet_is_unmeasurable_not_refuted():
    """Ниже 250 dpi (основная линия < 4,5 px) — «не измеримо», как у профиля вала."""
    items = verify_placed_on_sections(
        _sheet(), VIEW, 1.0 / PX, _body(_hole(45.0, 10.0)), line_px=3.0
    )

    assert [item["status"] for item in items] == ["unmeasurable"]
    assert "грубый" in items[0]["reason"]


def test_without_a_shaft_view_every_placed_feature_still_gets_a_verdict():
    """Контракт стадии: вид вала не найден (фото) — «не измеримо», не молчание."""
    import io

    from PIL import Image

    from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

    blank = io.BytesIO()
    Image.new("L", (1200, 600), 255).save(blank, format="PNG")
    spec = {"main_view": _body(_hole(45.0, 8.0), _flat(90.0, 4.0))}
    items = [
        item
        for item in verify_spec_against_sheet(blank.getvalue(), spec)["items"]
        if item["kind"] == "placed_feature"
    ]

    assert [item["status"] for item in items] == ["unmeasurable", "unmeasurable"], items
