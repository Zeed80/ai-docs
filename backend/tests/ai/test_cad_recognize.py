"""CV recognizer → CAD IR entities + independent coverage verifier."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import Circle, Point, Segment
from app.ai.cad_recognize import CvRecognizer
from app.ai.cad_recognize.verify import apply_to_ir, score_coverage


def _blank(h: int = 300, w: int = 400) -> np.ndarray:
    return np.zeros((h, w), dtype=np.uint8)


def _simple_sheet() -> np.ndarray:
    ink = _blank(400, 500)
    cv2.line(ink, (50, 50), (450, 50), 255, 4)
    cv2.line(ink, (50, 50), (50, 350), 255, 4)
    cv2.circle(ink, (250, 200), 80, 255, 2)
    return ink


def test_cv_recognizer_produces_typed_entities():
    out = CvRecognizer().recognize(_simple_sheet())
    assert out is not None
    types = sorted(e.type for e in out.entities)
    assert "circle" in types
    assert types.count("segment") >= 2
    for entity in out.entities:
        assert 0.5 <= entity.confidence <= 1.0
        assert entity.origin == "cv"
        assert entity.width_class in ("main", "thin")


def test_cv_recognizer_geometry_close_to_source():
    out = CvRecognizer().recognize(_simple_sheet())
    circles = [e for e in out.entities if e.type == "circle"]
    assert len(circles) == 1
    c = circles[0]
    assert c.center.x == pytest.approx(250, abs=3)
    assert c.center.y == pytest.approx(200, abs=3)
    assert c.radius == pytest.approx(80, abs=3)


def test_cv_recognizer_declines_dense_raster():
    ink = np.full((200, 200), 255, dtype=np.uint8)  # solid black sheet
    assert CvRecognizer().recognize(ink) is None


def test_verifier_accepts_faithful_proposal():
    ink = _simple_sheet()
    out = CvRecognizer().recognize(ink)
    score = score_coverage(
        out.entities, ink, out.keep_raster, thin_px=out.thin_px, thick_px=out.thick_px
    )
    assert score.ok, f"recall={score.recall} precision={score.precision}"

    ir = CadIR(source=SourceInfo(image_width=500, image_height=400), entities=out.entities)
    apply_to_ir(ir, score)
    assert ir.validation.coverage_recall == score.recall


def test_verifier_rejects_hallucinated_proposal():
    ink = _simple_sheet()
    fake = [
        Segment(p1=Point(x=400, y=380), p2=Point(x=490, y=390)),
        Circle(center=Point(x=100, y=350), radius=30),
    ]
    score = score_coverage(fake, ink)
    assert not score.ok


def test_verifier_rejects_incomplete_proposal():
    ink = _simple_sheet()
    out = CvRecognizer().recognize(ink)
    only_circle = [e for e in out.entities if e.type == "circle"]
    score = score_coverage(only_circle, ink, thin_px=out.thin_px, thick_px=out.thick_px)
    assert score.recall < 0.85
