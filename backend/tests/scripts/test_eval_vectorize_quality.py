"""eval_vectorize B4 geometry-quality metrics: fragmentation, degenerate/
duplicate rates and open-endpoint rate are self-referential (no GT alignment),
so they can score photos too."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_vectorize import _geometry_quality, _ground_truth_integrity, _recognize  # noqa: E402

from app.ai.cad_ir.schema import Point, Segment  # noqa: E402


def _seg(x1, y1, x2, y2):
    return Segment(p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2))


def test_closed_square_has_no_open_endpoints() -> None:
    # four segments meeting corner-to-corner: every endpoint has a neighbour.
    q = _geometry_quality(
        [_seg(0, 0, 100, 0), _seg(100, 0, 100, 100), _seg(100, 100, 0, 100), _seg(0, 100, 0, 0)]
    )
    assert q["n_segments"] == 4
    assert q["open_endpoint_rate"] == 0.0
    assert q["degenerate_rate"] == 0.0


def test_floating_segments_are_all_open() -> None:
    # two disjoint far-apart segments — every endpoint floats free.
    q = _geometry_quality([_seg(0, 0, 50, 0), _seg(500, 500, 550, 500)])
    assert q["open_endpoint_rate"] == 1.0


def test_degenerate_and_duplicate_rates() -> None:
    q = _geometry_quality(
        [
            _seg(0, 0, 100, 0),
            _seg(0, 0, 100, 0),  # duplicate
            _seg(10, 10, 11, 10),  # 1px → degenerate
        ]
    )
    assert q["duplicate_rate"] > 0
    assert q["degenerate_rate"] > 0


def test_empty_input() -> None:
    assert _geometry_quality([]) == {"n_segments": 0}


def test_dxf_roundtrip_reports_eskd_errors() -> None:
    # H1: the downstream chain (IR → ЕСКД validate → DXF → independent parse)
    # must report reopen success and a non-negative blocking-error count.
    from eval_vectorize import _dxf_roundtrip

    out = _dxf_roundtrip([_seg(0, 0, 100, 0), _seg(100, 0, 100, 80)], 400, 300)
    assert out["dxf_reopens"] is True
    assert out["dxf_entities"] >= 2
    assert out["eskd_errors"] >= 0


def test_broken_insert_excludes_sheet_from_ground_truth() -> None:
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (10, 0))
    doc.modelspace().new_entity(
        "INSERT",
        {"name": "MISSING_BLOCK", "insert": (0, 0)},
    )

    complete, issues = _ground_truth_integrity(doc)

    assert complete is False
    assert issues == ["broken_insert:MISSING_BLOCK"]


def test_neural_mode_calls_seq2seq_candidate(monkeypatch) -> None:
    """Candidate evaluation must not be routed to technical-vectorizer."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.base import RecognizeOutput
    from app.ai.cad_recognize.neural import NeuralRecognizer

    called = []

    def fake_recognize(self, ink, exclusion_boxes=None):
        called.append((ink.shape, exclusion_boxes))
        return RecognizeOutput(
            entities=[_seg(2, 2, 20, 2)],
            keep_raster=None,
            thin_px=2,
            thick_px=3,
        )

    monkeypatch.setattr(NeuralRecognizer, "recognize", fake_recognize)
    image = np.full((32, 32), 255, dtype=np.uint8)
    cv2.line(image, (2, 2), (20, 2), 0, 1)
    ok, encoded = cv2.imencode(".png", image)
    assert ok

    result = _recognize(encoded.tobytes(), enhance=False, recognizer="neural")

    assert called
    assert result is not None
    assert result["recognizer_used"] == "neural"
    assert result["declined"] is False
