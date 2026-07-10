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
