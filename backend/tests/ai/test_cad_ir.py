"""CAD IR schema and sequence roundtrip."""

from __future__ import annotations

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import (
    Arc,
    Circle,
    DimensionEntity,
    HatchRegion,
    Point,
    Polyline,
    Segment,
    TextEntity,
)
from app.ai.cad_ir.sequence import COMMANDS, N_PARAMS, decode, encode


def _sample_ir() -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=800, image_height=600),
        scale=0.25,
        entities=[
            Segment(p1=Point(x=10, y=20), p2=Point(x=410, y=20)),
            Segment(p1=Point(x=10, y=20), p2=Point(x=10, y=320), line_class="thin", width_class="thin"),
            Arc(center=Point(x=200, y=150), radius=50, start_angle=0, end_angle=90, line_class="axis"),
            Circle(center=Point(x=400, y=300), radius=40),
            Polyline(points=[Point(x=1, y=1), Point(x=5, y=9), Point(x=20, y=9)], closed=True),
            HatchRegion(boundary=[Point(x=0, y=0), Point(x=10, y=0), Point(x=5, y=8)]),
            TextEntity(position=Point(x=100, y=90), text="Ø40H7", height=5),
            DimensionEntity(p1=Point(x=10, y=340), p2=Point(x=410, y=340), text="400", value_mm=100.0),
        ],
    )


def test_json_roundtrip() -> None:
    ir = _sample_ir()
    restored = CadIR.model_validate_json(ir.model_dump_json())
    assert restored == ir
    assert restored.counts() == {
        "segment": 2,
        "arc": 1,
        "circle": 1,
        "polyline": 1,
        "hatch": 1,
        "text": 1,
        "dimension": 1,
    }


def test_entity_by_id() -> None:
    ir = _sample_ir()
    first = ir.entities[0]
    assert ir.entity_by_id(first.id) is first
    assert ir.entity_by_id("missing") is None


def test_sequence_roundtrip_geometry() -> None:
    ir = _sample_ir()
    rows = encode(ir)
    assert all(len(r) == N_PARAMS + 1 for r in rows)
    assert int(rows[-1][0]) == COMMANDS.index("EOS")

    decoded = decode(rows, ir.source, origin="neural")
    # text/dimension are out-of-band: geometry entities only
    geo = [e for e in ir.entities if e.type not in ("text", "dimension")]
    assert [e.type for e in decoded] == [e.type for e in geo]
    for got, want in zip(decoded, geo):
        assert got.line_class == want.line_class
        assert got.width_class == want.width_class
        assert got.origin == "neural"

    seg = decoded[0]
    assert seg.p1.x == pytest.approx(10, abs=1e-6)
    assert seg.p2.x == pytest.approx(410, abs=1e-6)
    arc = decoded[2]
    assert arc.radius == pytest.approx(50, abs=1e-6)
    assert arc.end_angle == pytest.approx(90, abs=1e-6)
    pln = decoded[4]
    assert pln.closed is True
    assert len(pln.points) == 3


def test_decode_tolerates_garbage_rows() -> None:
    ir = _sample_ir()
    rows = encode(ir)
    rows.insert(0, [99.0] * (N_PARAMS + 1))  # unknown command
    rows.insert(1, [1.0, 0.5])  # malformed width
    decoded = decode(rows, ir.source)
    assert len(decoded) == len([e for e in ir.entities if e.type not in ("text", "dimension")])
