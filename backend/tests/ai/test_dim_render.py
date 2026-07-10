"""Dimension arrowhead/label geometry helpers (Ф5.1)."""

from __future__ import annotations

import math

from app.ai.cad_ir.dim_render import (
    arrow_len_mm,
    arrow_len_px,
    arrow_triangle,
    dimension_arrows_for_points,
    dimension_label,
)
from app.ai.cad_ir.schema import DimensionEntity, Point


def test_arrow_len_px_uses_scale_when_known() -> None:
    # 2.5mm ГОСТ arrow at 0.5 mm/px -> 5 px
    assert arrow_len_px(0.5) == 5.0


def test_arrow_len_px_falls_back_without_scale() -> None:
    assert arrow_len_px(None) > 0


def test_arrow_len_mm_is_the_gost_constant() -> None:
    assert arrow_len_mm() == 2.5


def test_arrow_triangle_apex_at_tip() -> None:
    tri = arrow_triangle((10, 0), (1, 0), 4)
    assert tri[0] == (10, 0)
    # base points are behind the tip along -direction, symmetric across the axis
    assert tri[1][0] == tri[2][0]
    assert tri[1][1] == -tri[2][1]


def test_dimension_arrows_for_points_linear_has_two_outward_triangles() -> None:
    tris = dimension_arrows_for_points((0, 0), (100, 0), "linear", 5)
    assert len(tris) == 2
    assert tris[0][0] == (0, 0)  # apex at p1
    assert tris[1][0] == (100, 0)  # apex at p2


def test_dimension_arrows_for_points_radial_has_one_triangle_at_p2() -> None:
    tris = dimension_arrows_for_points((0, 0), (30, 0), "radial", 5)
    assert len(tris) == 1
    assert tris[0][0] == (30, 0)


def test_dimension_label_plain_linear_unchanged() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="180", kind="linear")
    assert dimension_label(e) == "180"


def test_dimension_label_diameter_gets_gost_prefix() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="40", kind="diameter")
    assert dimension_label(e) == "⌀40"


def test_dimension_label_diameter_already_prefixed_not_doubled() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="Ø40H7", kind="diameter")
    assert dimension_label(e) == "Ø40H7"


def test_dimension_label_radial_gets_r_prefix() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="8", kind="radial")
    assert dimension_label(e) == "R8"


def test_dimension_label_falls_back_to_value_mm_when_no_text() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="", value_mm=12.5, kind="linear")
    assert dimension_label(e) == "12.5"


def test_dimension_label_empty_when_nothing_known() -> None:
    e = DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    assert dimension_label(e) == ""
