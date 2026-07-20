"""DXF → CAD IR import adapter (the /cad section's file entry point)."""

from __future__ import annotations

import math

import pytest

pytest.importorskip("ezdxf")

from app.ai.cad_ir.adapters.from_dxf import DxfImportError, dxf_to_ir
from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_ir.schema import (
    AnnotationEntity,
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Point,
    Polyline,
    Segment,
    SourceInfo,
    TextEntity,
)


def _source_ir() -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=800, image_height=600, kind="blank"),
        scale=0.25,  # 4 px/mm, same as the blank sheet default
        scale_source="sheet_format",
        entities=[
            Segment(p1=Point(x=100, y=100), p2=Point(x=700, y=100), line_class="contour"),
            Segment(p1=Point(x=100, y=100), p2=Point(x=100, y=500), line_class="axis"),
            Circle(center=Point(x=400, y=300), radius=120),
            Arc(center=Point(x=400, y=300), radius=180, start_angle=10, end_angle=120),
            Polyline(
                points=[Point(x=200, y=400), Point(x=300, y=450), Point(x=380, y=400)],
                closed=False,
            ),
            TextEntity(position=Point(x=150, y=550), text="Ø40H7", height=14),
            DimensionEntity(
                p1=Point(x=200, y=200),
                p2=Point(x=400, y=200),
                kind="linear",
                text="50",
                value_mm=50,
            ),
            DimensionEntity(
                p1=Point(x=200, y=250),
                p2=Point(x=400, y=250),
                kind="diameter",
                text="50",
                value_mm=50,
            ),
            HatchRegion(
                boundary=[
                    Point(x=500, y=350),
                    Point(x=650, y=350),
                    Point(x=650, y=500),
                    Point(x=500, y=500),
                ],
                holes=[[
                    Point(x=540, y=390),
                    Point(x=580, y=390),
                    Point(x=580, y=430),
                ]],
            ),
            AnnotationEntity(
                position=Point(x=250, y=500),
                kind="roughness",
                value="3.2",
            ),
        ],
    )


def test_roundtrip_via_own_exporter_preserves_entities():
    dxf = render_ir_to_dxf(_source_ir())
    ir = dxf_to_ir(dxf)
    counts: dict[str, int] = {}
    for e in ir.entities:
        counts[e.type] = counts.get(e.type, 0) + 1
    assert counts == {
        "segment": 2,
        "circle": 1,
        "arc": 1,
        "polyline": 1,
        "text": 1,
        "dimension": 2,
        "hatch": 1,
        "annotation": 1,
    }
    assert ir.source.kind == "import"
    assert ir.scale is not None and ir.scale_source is not None
    assert all(e.origin == "human" for e in ir.entities)


def test_roundtrip_preserves_geometry_shape():
    src = _source_ir()
    dxf = render_ir_to_dxf(src)
    ir = dxf_to_ir(dxf)

    # Lengths are scale-invariant across the export/import pixel spaces:
    # both use 4 px/mm here, so px lengths must match within a pixel.
    def seg_len(s):
        return math.hypot(s.p2.x - s.p1.x, s.p2.y - s.p1.y)

    src_lens = sorted(seg_len(s) for s in src.entities if s.type == "segment")
    out_lens = sorted(seg_len(s) for s in ir.entities if s.type == "segment")
    assert out_lens == pytest.approx(src_lens, abs=1.0)

    circle = next(e for e in ir.entities if e.type == "circle")
    assert circle.radius == pytest.approx(120, abs=1.0)

    arc = next(e for e in ir.entities if e.type == "arc")
    assert arc.radius == pytest.approx(180, abs=1.0)
    # IR->DXF->IR must preserve the arc's angular span.
    src_arc = next(e for e in src.entities if e.type == "arc")
    span = abs(arc.end_angle - arc.start_angle) % 360
    src_span = abs(src_arc.end_angle - src_arc.start_angle) % 360
    assert span == pytest.approx(src_span, abs=1.0)


def test_layer_names_map_back_to_line_classes():
    dxf = render_ir_to_dxf(_source_ir())
    ir = dxf_to_ir(dxf)
    seg_classes = sorted(e.line_class for e in ir.entities if e.type == "segment")
    assert seg_classes == ["axis", "contour"]


def test_dimension_hatch_and_annotation_semantics_survive_roundtrip():
    ir = dxf_to_ir(render_ir_to_dxf(_source_ir()))

    dimensions = [entity for entity in ir.entities if entity.type == "dimension"]
    assert [entity.kind for entity in dimensions] == ["linear", "diameter"]
    assert [entity.value_mm for entity in dimensions] == [50.0, 50.0]
    hatch = next(entity for entity in ir.entities if entity.type == "hatch")
    assert hatch.pattern == "ansi31"
    assert len(hatch.boundary) >= 4
    assert len(hatch.holes) == 1
    annotation = next(entity for entity in ir.entities if entity.type == "annotation")
    assert annotation.kind == "roughness"
    assert annotation.value == "Ra 3.2"


def test_nested_insert_is_expanded_with_transform():
    import io

    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    child = doc.blocks.new("CHILD")
    child.add_circle((0, 0), 2)
    parent = doc.blocks.new("PARENT")
    parent.add_blockref("CHILD", (10, 0))
    doc.modelspace().add_blockref("PARENT", (100, 50))
    stream = io.StringIO()
    doc.write(stream)

    ir = dxf_to_ir(stream.getvalue().encode())

    circle = next(entity for entity in ir.entities if entity.type == "circle")
    assert circle.radius == pytest.approx(8.0)


def test_inches_units_convert_to_mm():
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 1  # inches
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))  # 10in = 254mm
    import io

    buf = io.StringIO()
    doc.write(buf)
    ir = dxf_to_ir(buf.getvalue().encode())
    seg = next(e for e in ir.entities if e.type == "segment")
    length_px = math.hypot(seg.p2.x - seg.p1.x, seg.p2.y - seg.p1.y)
    assert length_px == pytest.approx(254 * 4.0, abs=2.0)  # 4 px/mm


def test_garbage_bytes_raise_import_error():
    with pytest.raises(DxfImportError):
        dxf_to_ir(b"\x00\x01\x02 not a dxf at all")


def test_empty_modelspace_raises():
    import io

    import ezdxf

    doc = ezdxf.new("R2010")
    buf = io.StringIO()
    doc.write(buf)
    with pytest.raises(DxfImportError):
        dxf_to_ir(buf.getvalue().encode())
