"""IR renders: DXF readback, SVG structure, PNG self-consistency."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_ir.png_render import render_ir_to_png
from app.ai.cad_ir.schema import (
    Arc,
    Circle,
    DimensionEntity,
    Point,
    Polyline,
    Segment,
    TextEntity,
)
from app.ai.cad_ir.svg_render import render_ir_to_svg


def _ir(scale: float | None = 0.5) -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=scale,
        entities=[
            Segment(p1=Point(x=20, y=280), p2=Point(x=380, y=280)),
            Segment(p1=Point(x=20, y=280), p2=Point(x=20, y=40), line_class="axis", width_class="thin"),
            Circle(center=Point(x=200, y=150), radius=60, confidence=0.62),
            Arc(center=Point(x=300, y=100), radius=30, start_angle=0, end_angle=90),
            Polyline(points=[Point(x=50, y=50), Point(x=90, y=50), Point(x=90, y=90)]),
            TextEntity(position=Point(x=100, y=100), text="Ø40H7", height=12),
            DimensionEntity(p1=Point(x=20, y=295), p2=Point(x=380, y=295), text="180"),
        ],
    )


def test_dxf_readback_layers_and_geometry() -> None:
    import io

    import ezdxf

    data = render_ir_to_dxf(_ir())
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    msp = doc.modelspace()
    types = sorted(e.dxftype() for e in msp)
    assert types.count("LINE") == 3  # 2 segments + dimension leader
    assert "CIRCLE" in types and "ARC" in types and "LWPOLYLINE" in types
    assert types.count("TEXT") == 2  # annotation + dimension label

    circle = next(e for e in msp if e.dxftype() == "CIRCLE")
    # px→mm: x*0.5, y flipped: (300-150)*0.5 = 75
    assert circle.dxf.center.x == pytest.approx(100)
    assert circle.dxf.center.y == pytest.approx(75)
    assert circle.dxf.radius == pytest.approx(30)

    axis_line = [e for e in msp if e.dxftype() == "LINE" and e.dxf.layer == "CENTER"]
    assert len(axis_line) == 1
    assert {e.dxf.layer for e in msp} <= {"OBJECT", "OBJECT_THIN", "CENTER", "HIDDEN", "DIM", "HATCH", "ANNOTATION"}


def test_dxf_without_scale_uses_pixel_units() -> None:
    import io

    import ezdxf

    doc = ezdxf.read(io.StringIO(render_ir_to_dxf(_ir(scale=None)).decode("utf-8")))
    circle = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE")
    assert circle.dxf.radius == pytest.approx(60)


def test_svg_has_entity_ids_and_confidence() -> None:
    ir = _ir()
    svg = render_ir_to_svg(ir).decode("utf-8")
    for entity in ir.entities:
        assert f'data-entity-id="{entity.id}"' in svg
    assert 'data-confidence="0.62"' in svg
    assert "Ø40H7" in svg
    assert 'stroke-dasharray="12 3 3 3"' in svg  # axis line


def test_svg_dimension_has_arrowhead_polygons() -> None:
    ir = _ir()
    svg = render_ir_to_svg(ir).decode("utf-8")
    # two filled arrowhead triangles for the linear dimension, drawn as <polygon>
    assert svg.count('fill="currentColor" stroke="none"/>') >= 2


def test_svg_diameter_and_radial_labels_get_gost_prefix() -> None:
    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,
        entities=[
            DimensionEntity(p1=Point(x=140, y=150), p2=Point(x=260, y=150), text="40", kind="diameter"),
            DimensionEntity(p1=Point(x=300, y=100), p2=Point(x=330, y=100), text="30", kind="radial"),
        ],
    )
    svg = render_ir_to_svg(ir).decode("utf-8")
    assert "⌀40" in svg
    assert "R30" in svg


def test_dxf_dimension_has_solid_arrowheads() -> None:
    import io

    import ezdxf

    data = render_ir_to_dxf(_ir())
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    solids = [e for e in doc.modelspace() if e.dxftype() == "SOLID" and e.dxf.layer == "DIM"]
    assert len(solids) == 2  # linear dimension: one arrowhead per end


def test_dxf_radial_dimension_has_single_arrowhead() -> None:
    import io

    import ezdxf

    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,
        entities=[
            DimensionEntity(p1=Point(x=300, y=100), p2=Point(x=330, y=100), text="30", kind="radial"),
        ],
    )
    doc = ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode("utf-8")))
    solids = [e for e in doc.modelspace() if e.dxftype() == "SOLID"]
    assert len(solids) == 1
    texts = [e.dxf.text for e in doc.modelspace() if e.dxftype() == "TEXT"]
    assert "R30" in texts


def test_png_self_consistency_roundtrip() -> None:
    """IR → PNG → CV recognizer → geometry matches the original entities."""
    import cv2

    from app.ai.cad_recognize import CvRecognizer

    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        entities=[
            Segment(p1=Point(x=50, y=250), p2=Point(x=350, y=250), width_class="main"),
            Circle(center=Point(x=200, y=130), radius=70, width_class="main"),
        ],
    )
    png = render_ir_to_png(ir, thin_px=2, thick_px=3)
    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    ink = ((255 - img) > 127).astype(np.uint8) * 255

    out = CvRecognizer().recognize(ink)
    assert out is not None
    circles = [e for e in out.entities if e.type == "circle"]
    segments = [e for e in out.entities if e.type == "segment"]
    assert len(circles) == 1
    assert circles[0].center.x == pytest.approx(200, abs=3)
    assert circles[0].radius == pytest.approx(70, abs=3)
    assert any(
        abs(s.p1.y - 250) < 4 and abs(s.p2.y - 250) < 4 and abs(s.p1.x - s.p2.x) > 280
        for s in segments
    )
