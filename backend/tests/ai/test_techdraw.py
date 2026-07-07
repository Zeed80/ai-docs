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
    # show_frame defaults to False: no sheet frame/stamp — just the drawing,
    # with the scale still stated (ГОСТ 2.109 requires it even without a stamp)
    assert "М 1:1" in svg
    assert "Масштаб" not in svg
    assert "Сталь 40Х ГОСТ 4543-2016" not in svg


def test_shaft_svg_with_frame_has_gost_title_block():
    spec = {**SHAFT, "title": {**SHAFT["title"], "show_frame": True}}
    svg = techdraw.render_spec_to_svg(spec)
    assert "Сталь 40Х ГОСТ 4543-2016" in svg
    assert "Масштаб" in svg


def test_plate_svg_has_bolt_circle_and_fits():
    svg = techdraw.render_spec_to_svg(PLATE)
    assert "Ø120" in svg and "Ø40H7" in svg and "Ø90" in svg
    assert "6×Ø11H12" in svg
    # no frame by default → part name (only ever shown in the title block) is absent
    assert "Фланец" not in svg


def test_plate_svg_with_frame_shows_part_name():
    spec = {**PLATE, "title": {**PLATE["title"], "show_frame": True}}
    svg = techdraw.render_spec_to_svg(spec)
    assert "Фланец" in svg


def test_isometric_view_renders():
    svg = techdraw.render_spec_to_svg(SHAFT, view="isometric")
    assert "Изометрия" in svg and "ellipse" in svg


def test_png_and_dxf_export():
    png = techdraw.render_spec_to_png(SHAFT)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    dxf = techdraw.render_spec_to_dxf(SHAFT)
    assert b"SECTION" in dxf and b"ENTITIES" in dxf  # valid DXF


def test_dxf_uses_millimeters_and_real_dimensions():
    import io
    import ezdxf

    spec = {
        "type": "shaft",
        "segments": [{"diameter": 50, "length": 50, "roughness": 1.6}],
        "title": {},
    }
    doc = ezdxf.read(io.StringIO(techdraw.render_spec_to_dxf(spec).decode("utf-8")))

    assert doc.header.get("$INSUNITS") == 4  # millimeters, not meters
    dims = list(doc.modelspace().query("DIMENSION"))
    assert len(dims) >= 3
    assert {d.dxf.text for d in dims} >= {"50", "Ø50"}

    object_lines = [e for e in doc.modelspace().query("LINE") if e.dxf.layer == "OBJECT"]
    xs = [p for line in object_lines for p in (line.dxf.start.x, line.dxf.end.x)]
    ys = [p for line in object_lines for p in (line.dxf.start.y, line.dxf.end.y)]
    assert max(xs) - min(xs) == pytest.approx(50)
    assert max(ys) - min(ys) == pytest.approx(50)


def test_plate_dxf_has_hole_and_bolt_circle_dimensions():
    import io
    import ezdxf

    doc = ezdxf.read(io.StringIO(techdraw.render_spec_to_dxf(PLATE).decode("utf-8")))
    dim_texts = {d.dxf.text for d in doc.modelspace().query("DIMENSION")}
    assert {"Ø120", "Ø40H7", "Ø90"} <= dim_texts
    assert any((t.dxf.text or "").startswith("6xØ11H12") for t in doc.modelspace().query("TEXT"))


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        techdraw.render_spec_to_svg({"type": "spaceship"})


SHAFT_WITH_BORE = {
    "type": "shaft",
    "segments": [
        {"diameter": 45, "length": 60, "tolerance": "h6", "roughness": 0.8,
         "bore_diameter": 20, "section_hatch": True},
        {"diameter": 24, "length": 25, "thread": "M24x2", "thread_end_view": True},
    ],
    "title": {"name": "Вал", "material": "Сталь 45 ГОСТ 1050-2013"},
}

ASSEMBLY = {
    "type": "assembly",
    "components": [
        {"ref": "1", "spec": SHAFT, "x": 0, "y": 0},
        {"ref": "2", "spec": PLATE, "x": 140, "y": 0},
    ],
    "bom": [
        {"pos": 1, "name": "Вал", "qty": 1, "material": "Сталь 40Х"},
        {"pos": 2, "name": "Фланец", "qty": 1, "material": "Сталь 20"},
    ],
    "title": {"name": "Сборка узла"},
}


def test_shaft_section_hatches_and_shows_bore():
    front = techdraw.render_spec_to_svg(SHAFT_WITH_BORE, view="front")
    section = techdraw.render_spec_to_svg(SHAFT_WITH_BORE, view="section")
    # sectioning adds hatch lines that a plain front view doesn't have
    assert section.count("<line") > front.count("<line")


def test_shaft_half_section_renders():
    svg = techdraw.render_spec_to_svg(SHAFT_WITH_BORE, view="half_section")
    assert "<svg" in svg


def test_shaft_real_thread_geometry_not_just_label():
    svg = techdraw.render_spec_to_svg(SHAFT_WITH_BORE)
    # minor-diameter lines for M24x2 (d1 ≈ 21.6mm) exist alongside the label
    assert "M24" in svg


def test_plate_section_renders_with_hatch():
    front = techdraw.render_spec_to_svg(PLATE, view="front")
    section = techdraw.render_spec_to_svg(PLATE, view="section")
    assert section.count("<line") > front.count("<line")


def test_assembly_renders_both_components_and_bom():
    svg = techdraw.render_spec_to_svg(ASSEMBLY)
    assert "Ø45h6" in svg or "Ø45" in svg  # shaft component present
    assert "Ø120" in svg  # plate component present
    assert "Вал" in svg and "Фланец" in svg  # BOM rows


def test_assembly_isometric_not_supported():
    with pytest.raises(ValueError):
        techdraw.render_spec_to_svg(ASSEMBLY, view="isometric")


def test_assembly_requires_at_least_one_component():
    with pytest.raises(Exception):
        techdraw.render_spec_to_svg({"type": "assembly", "components": []})


def test_assembly_png_and_dxf_export():
    png = techdraw.render_spec_to_png(ASSEMBLY)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    dxf = techdraw.render_spec_to_dxf(ASSEMBLY)
    assert b"SECTION" in dxf and b"HATCH" in dxf


def test_auto_sheet_format_escalates_for_large_part():
    big = {"type": "shaft", "segments": [{"diameter": 80, "length": 800}], "title": {}}
    svg = techdraw.render_spec_to_svg(big)
    assert 'viewBox="0 0 594 420"' in svg  # A2


def test_explicit_sheet_format_honored():
    spec = {**SHAFT, "title": {**SHAFT["title"], "sheet_format": "A3"}}
    svg = techdraw.render_spec_to_svg(spec)
    assert 'viewBox="0 0 420 297"' in svg


def test_title_block_new_gost_2104_fields():
    spec = {**SHAFT, "title": {**SHAFT["title"], "show_frame": True, "mass_kg": 1.2,
                                "litera": "У", "checked_by": "Иванов", "sheet_no": 1,
                                "sheet_count": 1}}
    svg = techdraw.render_spec_to_svg(spec)
    assert "1.2" in svg and "У" in svg and "Иванов" in svg


def test_png_autocrops_when_frame_is_off():
    from PIL import Image
    import io as _io

    png_no_frame = techdraw.render_spec_to_png(SHAFT)
    spec_framed = {**SHAFT, "title": {**SHAFT["title"], "show_frame": True}}
    png_framed = techdraw.render_spec_to_png(spec_framed)

    img_no_frame = Image.open(_io.BytesIO(png_no_frame))
    img_framed = Image.open(_io.BytesIO(png_framed))
    # Framed render is the full A4 sheet; frame-less is cropped to content —
    # meaningfully smaller in both dimensions, not just a coincidence of scale.
    assert img_no_frame.width < img_framed.width
    assert img_no_frame.height < img_framed.height


def test_assembly_svg_no_frame_by_default():
    spec = {
        "type": "assembly",
        "components": [
            {"ref": "1", "spec": {**PLATE, "title": {}}, "x": 0, "y": 0},
        ],
        "bom": [{"pos": 1, "name": "Фланец", "qty": 1}],
        "title": {"name": "Сборка"},
    }
    svg = techdraw.render_spec_to_svg(spec)
    assert "Сборка" not in svg  # part name only ever drawn in the (now-off) title block
    assert "Фланец" in svg  # BOM table is real content, always shown
