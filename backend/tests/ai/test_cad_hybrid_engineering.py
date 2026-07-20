import numpy as np

from app.ai.cad_ir.schema import Circle, Point, Segment
from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.hybrid_engineering import HybridEngineeringRecognizer


class _Stub:
    def __init__(self, entities):
        self.entities = entities

    def recognize(self, _ink, _exclusion_boxes=None):
        return RecognizeOutput(entities=self.entities)


def test_hybrid_keeps_cv_global_geometry_and_adds_supported_circle():
    import cv2

    ink = np.zeros((100, 100), dtype=np.uint8)
    cv2.circle(ink, (50, 50), 20, 255, 1)
    segment = Segment(p1=Point(x=5, y=5), p2=Point(x=95, y=5), confidence=1)
    circle = Circle(center=Point(x=50, y=50), radius=20, confidence=0.99)
    recognizer = HybridEngineeringRecognizer(
        cv=_Stub([segment]),
        primitive=_Stub([circle]),
    )

    result = recognizer.recognize(ink)

    assert result is not None
    assert [entity.type for entity in result.entities] == ["segment", "circle"]
    assert result.notes["primitive_round_entities_added"] == 1


def test_hybrid_rejects_circle_without_independent_ink_support():
    ink = np.zeros((100, 100), dtype=np.uint8)
    circle = Circle(center=Point(x=50, y=50), radius=20, confidence=0.99)
    recognizer = HybridEngineeringRecognizer(
        cv=_Stub([]),
        primitive=_Stub([circle]),
    )

    result = recognizer.recognize(ink)

    assert result is not None
    assert result.entities == []
    assert result.notes["primitive_rejected_unsupported"] == 1
