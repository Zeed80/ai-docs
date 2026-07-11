"""CV hatching/solid-fill extraction into structured HatchRegion entities
(Ф4.4) — closing the gap where solid regions shipped as opaque raster only
and were invisible in the DXF export (only entities render there)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_recognize import CvRecognizer
from app.ai.cad_recognize.cv import _hatch_regions_from_solid


def _sheet_with_filled_triangle() -> np.ndarray:
    """A contour + a solid-filled arrowhead-like triangle — the triangle's
    inscribed radius is far beyond normal stroke width, so
    drawing_vectorize._solid_regions should flag it as a solid blob."""
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.line(ink, (40, 40), (360, 40), 255, 4)
    cv2.line(ink, (40, 40), (40, 260), 255, 4)
    pts = np.array([[150, 150], [220, 150], [185, 220]], dtype=np.int32)
    cv2.fillPoly(ink, [pts], 255)
    return ink


def test_hatch_regions_from_solid_extracts_significant_blob():
    from app.ai.drawing_vectorize import extract_primitives

    ink = _sheet_with_filled_triangle()
    result = extract_primitives(ink)
    assert result is not None
    assert result.solid_mask.any(), "the filled triangle should be flagged as solid"
    hatches = _hatch_regions_from_solid(result.solid_mask)
    assert len(hatches) == 1
    assert hatches[0].type == "hatch"
    assert len(hatches[0].boundary) >= 3
    assert hatches[0].origin == "cv"


def _square_with_a_hole_mask() -> np.ndarray:
    """A section fill with a bolt hole through it — a completely ordinary
    detail (a solid region carrying a round or square cutout), not an edge
    case. RETR_EXTERNAL used to report this as one solid blob covering the
    hole too. Bare mask for direct _hatch_regions_from_solid unit tests —
    that function traces whatever mask it's handed with no solid-vs-stroke
    judgement of its own, so no reference lines belong in it here."""
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.rectangle(ink, (100, 80), (300, 220), 255, thickness=-1)  # filled outer square
    cv2.rectangle(ink, (170, 120), (230, 180), 0, thickness=-1)  # punched-out hole
    return ink


def _sheet_with_filled_square_with_a_hole() -> np.ndarray:
    """Same shape as ``_square_with_a_hole_mask``, plus thin reference lines
    (as in ``_sheet_with_filled_triangle``) establishing a normal
    stroke-width baseline — for the full-pipeline tests
    (extract_primitives/CvRecognizer), whose solid-vs-stroke heuristic needs
    something ordinary to compare the fill against."""
    ink = _square_with_a_hole_mask()
    cv2.line(ink, (20, 20), (380, 20), 255, 4)
    cv2.line(ink, (20, 20), (20, 280), 255, 4)
    return ink


def test_hatch_regions_from_solid_captures_a_hole():
    ink = _square_with_a_hole_mask()
    hatches = _hatch_regions_from_solid(ink.astype(bool))
    assert len(hatches) == 1
    region = hatches[0]
    assert len(region.boundary) >= 3
    assert len(region.holes) == 1
    assert len(region.holes[0]) >= 3


def test_hatch_regions_from_solid_ignores_tiny_holes():
    """A hole below the same min-area threshold as a region itself (noise,
    not a real cutout) is dropped, not reported as a spurious hole."""
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.rectangle(ink, (100, 80), (300, 220), 255, thickness=-1)
    cv2.rectangle(ink, (195, 145), (205, 155), 0, thickness=-1)  # 10x10=100px^2 < 150px^2 threshold
    hatches = _hatch_regions_from_solid(ink.astype(bool))
    assert len(hatches) == 1
    assert hatches[0].holes == []


def test_hatch_region_with_hole_reaches_dxf_as_two_boundary_paths():
    """The hole must survive all the way to the exported DXF as a second
    boundary path on the same HATCH entity (an inner, non-external path) —
    not silently dropped or merged into the outer boundary."""
    import io

    import ezdxf

    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    ink = _sheet_with_filled_square_with_a_hole()
    out = CvRecognizer().recognize(ink)
    hatch_entities = [e for e in out.entities if e.type == "hatch"]
    assert len(hatch_entities) == 1
    assert len(hatch_entities[0].holes) == 1

    ir = CadIR(source=SourceInfo(image_width=400, image_height=300), scale=1.0, entities=out.entities)
    dxf_bytes = render_ir_to_dxf(ir)
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    hatch = next(e for e in doc.modelspace() if e.dxftype() == "HATCH")
    assert len(hatch.paths) == 2  # outer boundary + one hole path


def test_hatch_regions_from_solid_ignores_empty_mask():
    empty = np.zeros((100, 100), dtype=bool)
    assert _hatch_regions_from_solid(empty) == []


def test_hatch_regions_from_solid_ignores_small_blobs():
    tiny = np.zeros((100, 100), dtype=bool)
    tiny[10:15, 10:15] = True  # 25px^2, well under the 150px^2 threshold
    assert _hatch_regions_from_solid(tiny) == []


def test_cv_recognizer_includes_hatch_entities_for_filled_regions():
    ink = _sheet_with_filled_triangle()
    out = CvRecognizer().recognize(ink)
    assert out is not None
    hatch_entities = [e for e in out.entities if e.type == "hatch"]
    assert len(hatch_entities) == 1


def test_hatch_region_reaches_dxf_output():
    """End-to-end: a solid-fill region recognized by CV must appear as a
    real HATCH entity in the exported DXF — previously it shipped as raster
    only and was entirely absent from the DXF (only entities render there)."""
    import io

    import ezdxf

    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    ink = _sheet_with_filled_triangle()
    out = CvRecognizer().recognize(ink)
    assert out is not None
    ir = CadIR(source=SourceInfo(image_width=400, image_height=300), scale=1.0, entities=out.entities)
    dxf_bytes = render_ir_to_dxf(ir)
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    types = [e.dxftype() for e in doc.modelspace()]
    assert "HATCH" in types
