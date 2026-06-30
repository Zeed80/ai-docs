"""Tests for the deterministic technical drawing generator."""

from __future__ import annotations

import pytest

pytest.importorskip("svgwrite")
pytest.importorskip("cairosvg")
pytest.importorskip("ezdxf")

from app.ai import techdraw  # noqa: E402

SHAFT = {
    "type": "shaft",
    "segments": [
        {"diameter": 24, "length": 25, "tolerance": "6g", "roughness": 1.6, "thread": "M24×2"},
        {"diameter": 45, "length": 60, "tolerance": "h6", "roughness": 0.8},
        {"diameter": 35, "length": 40, "tolerance": "k6", "roughness": 1.6},
    ],
    "title": {"name": "Вал", "material": "Сталь 40Х ГОСТ 4543-2016"},
}

PLATE = {
    "type": "plate", "shape": "circle", "diameter": 120, "thickness": 14,
    "holes": [{"x": 0, "y": 0, "diameter": 40, "tolerance": "H7"}],
    "bolt_circle_d": 90, "bolt_circle_n": 6, "bolt_hole_d": 11, "bolt_hole_tol": "H12",
    "title": {"name": "Фланец", "material": "Сталь 20 ГОСТ 1050-2013"},
}


def test_shaft_svg_has_exact_dims_and_tolerances():
    svg = techdraw.render_spec_to_svg(SHAFT)
    # exact diameter+tolerance callouts are real text in the vector output
    assert "Ø45h6" in svg and "Ø35k6" in svg and "M24×2" in svg
    # roughness symbols with exact Ra values
    assert "Ra 0.8" in svg and "Ra 1.6" in svg
    # overall length = sum of segment lengths, computed exactly
    assert ">125<" in svg
    # ГОСТ title block fields
    assert "Сталь 40Х ГОСТ 4543-2016" in svg
    assert "Масштаб" in svg


def test_plate_svg_has_bolt_circle_and_fits():
    svg = techdraw.render_spec_to_svg(PLATE)
    assert "Ø120" in svg and "Ø40H7" in svg and "Ø90" in svg
    assert "6×Ø11H12" in svg
    assert "Фланец" in svg


def test_isometric_view_renders():
    svg = techdraw.render_spec_to_svg(SHAFT, view="isometric")
    assert "Изометрия" in svg and "ellipse" in svg


def test_png_and_dxf_export():
    png = techdraw.render_spec_to_png(SHAFT)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    dxf = techdraw.render_spec_to_dxf(SHAFT)
    assert b"SECTION" in dxf and b"ENTITIES" in dxf  # valid DXF


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        techdraw.render_spec_to_svg({"type": "spaceship"})
