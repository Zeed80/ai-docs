import cv2
import numpy as np

from app.ai.cad_ir.schema import Point, Segment
from app.ai.cad_recognize.edge_graph import snap_edge_graph_entities


def test_edge_graph_snaps_to_source_endpoints_and_rejects_unsupported_edge() -> None:
    ink = np.zeros((100, 120), dtype=np.uint8)
    cv2.line(ink, (10, 30), (110, 30), 255, 2)
    supported = Segment(
        p1=Point(x=13, y=32),
        p2=Point(x=107, y=29),
        origin="neural",
        assurance="inferred",
    )
    unsupported = Segment(
        p1=Point(x=12, y=32),
        p2=Point(x=108, y=75),
        origin="neural",
        assurance="inferred",
    )
    result = snap_edge_graph_entities([supported, unsupported], ink)
    assert len(result) == 1
    assert result[0].p1.x <= 11
    assert result[0].p2.x >= 109
    assert "source-skeleton-snapped" in result[0].evidence
