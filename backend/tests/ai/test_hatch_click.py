"""Click-to-hatch: enclosed-region detection from a click point (Ф5.8)."""

from __future__ import annotations

import pytest

pytest.importorskip("cv2")

from app.ai.cad_ir.hatch_click import hatch_region_at_point
from app.ai.cad_ir.schema import CadIR, Point, Segment, SourceInfo


def _closed_square_ir() -> CadIR:
    pts = [(50, 50), (250, 50), (250, 250), (50, 250)]
    segs = []
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 4]
        segs.append(
            Segment(
                p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2),
                line_class="contour", width_class="main",
            )
        )
    return CadIR(source=SourceInfo(image_width=400, image_height=300), scale=1.0, entities=segs)


def test_click_inside_closed_square_returns_hatch() -> None:
    ir = _closed_square_ir()
    region = hatch_region_at_point(ir, 150, 150)
    assert region is not None
    assert region.type == "hatch"
    assert len(region.boundary) >= 3
    assert region.origin == "human"
    assert region.assurance == "human_approved"


def test_click_on_the_line_itself_returns_none() -> None:
    ir = _closed_square_ir()
    region = hatch_region_at_point(ir, 50, 150)  # on the left edge
    assert region is None


def test_click_outside_any_enclosure_returns_none() -> None:
    ir = _closed_square_ir()
    region = hatch_region_at_point(ir, 350, 280)  # open sheet area, flood spills to border
    assert region is None


def test_click_off_sheet_returns_none() -> None:
    ir = _closed_square_ir()
    region = hatch_region_at_point(ir, -10, 150)
    assert region is None


def test_click_with_no_geometry_returns_none() -> None:
    ir = CadIR(source=SourceInfo(image_width=400, image_height=300), scale=1.0, entities=[])
    region = hatch_region_at_point(ir, 150, 150)
    assert region is None
