"""A2 blocks: define a named block from geometry, stamp instances anywhere."""

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.blocks import define_block, insert_block
from app.ai.cad_ir.schema import Circle, Point, Segment
from app.ai.cad_ir.transform import SketchOpError


def _ir(*entities):
    return CadIR(
        source=SourceInfo(image_width=400, image_height=300, kind="blank"),
        scale=1, scale_source="manual", entities=list(entities),
    )


def test_define_and_insert_translates_to_click_point():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=20, y=0))
    circ = Circle(center=Point(x=10, y=10), radius=5)
    ir = _ir(seg, circ)
    block = define_block(ir, "bolt", [seg.id, circ.id])
    # base = bbox centre of (0,-?)..: x 0..20, y -? — points are p1,p2,center
    assert block.base.x == pytest.approx(10)
    inserted = insert_block(ir, "bolt", 100, 50)
    assert len(inserted) == 2
    new_circ = next(e for e in inserted if e.type == "circle")
    # circle centre was +0/+5 relative to base (10, 5) → lands at (100, 55)
    assert new_circ.center.x == pytest.approx(100)
    assert len(ir.entities) == 4  # originals stay


def test_insert_with_rotation():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=20, y=0))
    ir = _ir(seg)
    define_block(ir, "b", [seg.id])
    inserted = insert_block(ir, "b", 100, 100, rotation_deg=90)
    s = inserted[0]
    # horizontal 20px segment becomes vertical after 90°
    assert abs(s.p1.x - s.p2.x) < 1e-6
    assert abs(abs(s.p2.y - s.p1.y) - 20) < 1e-6


def test_redefine_replaces_and_unknown_insert_rejected():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=20, y=0))
    ir = _ir(seg)
    define_block(ir, "b", [seg.id])
    define_block(ir, "b", [seg.id])
    assert len(ir.blocks) == 1
    with pytest.raises(SketchOpError):
        insert_block(ir, "nope", 0, 0)
    with pytest.raises(SketchOpError):
        define_block(ir, "empty", ["missing-id"])


def test_inserted_entities_get_fresh_ids():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=20, y=0))
    ir = _ir(seg)
    define_block(ir, "b", [seg.id])
    a = insert_block(ir, "b", 50, 50)
    b = insert_block(ir, "b", 90, 50)
    ids = {seg.id, a[0].id, b[0].id}
    assert len(ids) == 3
