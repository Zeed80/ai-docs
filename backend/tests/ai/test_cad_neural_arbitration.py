"""Neural recognizer client + neural/CV arbitration in verify.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_ir.schema import Circle, Point, Segment
from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.neural import NeuralRecognizer
from app.ai.cad_recognize.primitive_set import PrimitiveSetRecognizer
from app.ai.cad_recognize.verify import _open_endpoint_rate, arbitrate_recognition


def _sheet() -> np.ndarray:
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.line(ink, (40, 40), (360, 40), 255, 4)
    cv2.line(ink, (40, 40), (40, 260), 255, 4)
    cv2.circle(ink, (200, 150), 60, 255, 2)
    return ink


class _FakeRecognizer:
    def __init__(self, name: str, output: RecognizeOutput | None):
        self.name = name
        self._output = output

    def recognize(self, ink, exclusion_boxes=None):
        return self._output


def _good_entities() -> list:
    return [
        Segment(p1=Point(x=40, y=40), p2=Point(x=360, y=40), width_class="main"),
        Segment(p1=Point(x=40, y=40), p2=Point(x=40, y=260), width_class="main"),
        Circle(center=Point(x=200, y=150), radius=60, width_class="thin"),
    ]


# ── NeuralRecognizer client ──────────────────────────────────────────────────


def test_neural_recognizer_declines_on_connection_error():
    rec = NeuralRecognizer(base_url="http://neural-does-not-exist.invalid:1")
    assert rec.recognize(_sheet()) is None


def test_neural_recognizer_parses_valid_response():
    rec = NeuralRecognizer(base_url="http://fake")
    payload = {
        "entities": [
            {"type": "segment", "p1": {"x": 1, "y": 2}, "p2": {"x": 3, "y": 4},
             "line_class": "contour", "width_class": "main", "confidence": 0.6,
             "origin": "neural", "assurance": "inferred"},
        ],
        "model_step": 500,
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    with patch("httpx.post", return_value=resp) as mock_post:
        out = rec.recognize(_sheet())
    assert mock_post.called
    assert out is not None
    assert len(out.entities) == 1
    assert out.entities[0].origin == "neural"
    assert out.notes["model_step"] == 500


def test_primitive_set_recognizer_uses_candidate_endpoint():
    rec = PrimitiveSetRecognizer(base_url="http://fake", tile_size=1000)
    payload = {
        "entities": [
            {
                "type": "circle",
                "center": {"x": 50, "y": 60},
                "radius": 12,
                "line_class": "contour",
                "width_class": "main",
                "confidence": 0.91,
                "origin": "neural",
                "assurance": "inferred",
            }
        ],
        "model_step": 200,
    }
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    with patch("httpx.post", return_value=response) as post:
        out = rec.recognize(_sheet())

    assert out is not None
    assert out.entities[0].type == "circle"
    assert post.call_args.args[0] == "http://fake/detect-primitives"


def test_neural_recognizer_skips_malformed_entities_but_keeps_valid_ones():
    rec = NeuralRecognizer(base_url="http://fake")
    payload = {"entities": [
        {"type": "segment", "p1": {"x": 1, "y": 2}},  # missing p2 -> invalid
        {"type": "circle", "center": {"x": 5, "y": 5}, "radius": 3,
         "line_class": "contour", "width_class": "main", "confidence": 0.5,
         "origin": "neural", "assurance": "inferred"},
    ]}
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    with patch("httpx.post", return_value=resp):
        out = rec.recognize(_sheet())
    assert out is not None
    assert len(out.entities) == 1
    assert out.entities[0].type == "circle"


def test_neural_recognizer_tiles_and_restores_sheet_coordinates(monkeypatch):
    import numpy as np

    from app.ai.cad_ir.schema import Point, Segment
    from app.ai.cad_recognize.base import RecognizeOutput

    rec = NeuralRecognizer(base_url="http://unused", tile_size=64, tile_overlap=16)
    calls = []

    def fake_once(ink, exclusion_boxes=None):
        calls.append(ink.shape)
        return RecognizeOutput(
            entities=[
                Segment(
                    p1=Point(x=20, y=20),
                    p2=Point(x=30, y=20),
                    origin="neural",
                )
            ],
            notes={"model_step": 7},
        )

    monkeypatch.setattr(rec, "_recognize_once", fake_once)
    out = rec.recognize(np.zeros((64, 112), dtype=np.uint8))

    assert out is not None
    assert calls == [(64, 64), (64, 64)]
    assert out.notes == {"model_step": 7, "tiled": True, "tiles": 2}
    assert len(out.entities) == 2
    assert out.entities[0].p1.x == 20
    assert out.entities[1].p1.x == 68


# ── Arbitration ──────────────────────────────────────────────────────────────


def test_arbitration_falls_back_to_cv_when_neural_unavailable():
    ink = _sheet()
    cv_out = RecognizeOutput(entities=_good_entities(), thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink,
        None,
        _FakeRecognizer("neural", None),
        _FakeRecognizer("cv", cv_out),
    )
    assert result.recognizer_used == "cv"
    assert not result.neural_available
    assert result.score.ok


def test_arbitration_prefers_neural_when_both_pass_and_similar():
    ink = _sheet()
    good = _good_entities()
    cv_out = RecognizeOutput(entities=good, thin_px=2, thick_px=4)
    neural_out = RecognizeOutput(entities=good, thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", neural_out), _FakeRecognizer("cv", cv_out)
    )
    assert result.recognizer_used == "neural"
    assert result.neural_available
    assert not result.discrepancy


def test_line_only_neural_keeps_cv_curve_families():
    ink = _sheet()
    good = _good_entities()
    cv_out = RecognizeOutput(entities=good, thin_px=2, thick_px=4)
    neural_out = RecognizeOutput(entities=good[:2], thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", neural_out), _FakeRecognizer("cv", cv_out)
    )
    assert result.recognizer_used == "neural+cv"
    assert {entity.type for entity in result.entities} == {"segment", "circle"}
    assert result.notes["cv_supplement_types"] == ["circle"]


def test_arbitration_falls_back_to_cv_when_neural_fails_coverage():
    ink = _sheet()
    cv_out = RecognizeOutput(entities=_good_entities(), thin_px=2, thick_px=4)
    bad_neural = RecognizeOutput(
        entities=[Segment(p1=Point(x=10, y=290), p2=Point(x=390, y=295))], thin_px=1, thick_px=2,
    )
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", bad_neural), _FakeRecognizer("cv", cv_out)
    )
    assert result.recognizer_used == "cv"


def test_arbitration_flags_discrepancy_when_both_pass_but_disagree_on_count():
    ink = _sheet()
    good = _good_entities()
    cv_out = RecognizeOutput(entities=good, thin_px=2, thick_px=4)
    # Neural "passes" coverage but reports a very different count: extra
    # circles stacked on the existing one. Circles aren't collinear-merged
    # or dash-recognized (only segments are), and each overlaps the real
    # circle ink, so they survive consolidation AND keep precision high —
    # a genuine whole-sheet miscount for the review queue to surface.
    inflated = [
        *good,
        Circle(center=Point(x=200, y=150), radius=60, width_class="thin"),
        Circle(center=Point(x=200, y=150), radius=60, width_class="thin"),
        Circle(center=Point(x=200, y=150), radius=60, width_class="thin"),
    ]
    neural_out = RecognizeOutput(entities=inflated, thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", neural_out), _FakeRecognizer("cv", cv_out)
    )
    assert result.discrepancy
    assert result.recognizer_used == "neural+cv"
    assert result.notes["cv_entities"] == len(good)
    assert result.notes["neural_entities"] == len(inflated)


def test_open_endpoint_rate_distinguishes_connected_from_floating():
    # A closed square: every endpoint meets a neighbour → nothing floats.
    square = [
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0)),
        Segment(p1=Point(x=100, y=0), p2=Point(x=100, y=100)),
        Segment(p1=Point(x=100, y=100), p2=Point(x=0, y=100)),
        Segment(p1=Point(x=0, y=100), p2=Point(x=0, y=0)),
    ]
    assert _open_endpoint_rate(square) == 0.0
    # Four isolated, far-apart segments: all eight endpoints float free.
    floating = [
        Segment(p1=Point(x=20 + 40 * i, y=200), p2=Point(x=20 + 40 * i, y=250))
        for i in range(4)
    ]
    assert _open_endpoint_rate(floating) == 1.0
    assert _open_endpoint_rate(square[:2]) is None  # too few to judge


def test_open_endpoint_rate_counts_t_junction_as_connected():
    geometry = [
        Segment(p1=Point(x=0, y=50), p2=Point(x=100, y=50)),
        Segment(p1=Point(x=50, y=0), p2=Point(x=50, y=50)),
        Segment(p1=Point(x=0, y=0), p2=Point(x=0, y=50)),
        Segment(p1=Point(x=100, y=0), p2=Point(x=100, y=50)),
    ]

    # Bottom endpoint of the vertical segment touches the middle of the
    # horizontal segment and is therefore a real junction, not a floating end.
    assert _open_endpoint_rate(geometry) == 3 / 8


def test_arbitration_prefers_clean_cv_over_disconnected_neural():
    # CV reads the square cleanly and completely; neural covers the same ink
    # but sprays extra disconnected fragments the count-ratio guard (3×) would
    # miss (8 vs 4). The disconnection guard hands the sheet to the cleaner CV.
    ink = np.zeros((320, 320), dtype=np.uint8)
    cv2.rectangle(ink, (40, 40), (280, 280), 255, 4)
    square = [
        Segment(p1=Point(x=40, y=40), p2=Point(x=280, y=40), width_class="main"),
        Segment(p1=Point(x=280, y=40), p2=Point(x=280, y=280), width_class="main"),
        Segment(p1=Point(x=280, y=280), p2=Point(x=40, y=280), width_class="main"),
        Segment(p1=Point(x=40, y=280), p2=Point(x=40, y=40), width_class="main"),
    ]
    cv_out = RecognizeOutput(entities=list(square), thin_px=2, thick_px=4)
    floating = [
        Segment(p1=Point(x=120, y=120 + 40 * i), p2=Point(x=170, y=120 + 40 * i))
        for i in range(4)
    ]
    neural_out = RecognizeOutput(entities=square + floating, thin_px=2, thick_px=4)

    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", neural_out), _FakeRecognizer("cv", cv_out)
    )

    assert result.recognizer_used == "cv"
    assert result.notes["neural_disconnected"] is True
    assert result.notes["neural_fragmented"] is False  # count ratio 2× < 3×
    assert result.discrepancy


def test_arbitration_rejects_runaway_neural_fragmentation():
    ink = _sheet()
    good = _good_entities()
    cv_out = RecognizeOutput(entities=good, thin_px=2, thick_px=4)
    # Fragmentation that consolidation cannot repair: parallel ticks 8px
    # apart crossing the top edge — not collinear, not chained, so the
    # inflated entity count is real leftover fragmentation. CV itself is a
    # complete read (passes the full coverage bar), so the guard applies.
    fragmented = good + [
        Segment(p1=Point(x=48 + 8 * i, y=36), p2=Point(x=48 + 8 * i, y=44))
        for i in range(20)
    ]
    neural_out = RecognizeOutput(entities=fragmented, thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", neural_out), _FakeRecognizer("cv", cv_out)
    )
    assert result.recognizer_used == "cv"
    assert {e.type for e in result.entities} == {"segment", "circle"}
    assert len(result.entities) == len(good)
    assert result.discrepancy
    assert result.notes["neural_fragmented"] is True


def test_arbitration_declines_both_returns_empty():
    ink = _sheet()
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", None), _FakeRecognizer("cv", None)
    )
    assert result.entities == []
    assert not result.neural_available


def test_lone_survivor_must_pass_coverage_too():
    """Regression: found live on real out-of-domain photos (2026-07-10) — CV
    declined outright, neural was the only responder but scored ~0 coverage
    (runaway generation), and arbitration shipped it anyway because the old
    code treated "the other backend is None" as "use whatever this one
    says", without checking IT passes the bar either. A low-coverage lone
    survivor must decline exactly like both backends declining would."""
    ink = _sheet()
    garbage_neural = RecognizeOutput(
        entities=[Segment(p1=Point(x=5, y=295), p2=Point(x=8, y=298))], thin_px=1, thick_px=2,
    )
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", garbage_neural), _FakeRecognizer("cv", None)
    )
    assert result.entities == []
    assert not result.score.ok


def test_lone_survivor_with_honest_partial_coverage_still_ships():
    """Regression (2026-07-11, live test_vector_files photos): CV found the
    two straight lines but missed the circle — recall ~0.7, precision 1.0
    (nothing hallucinated, just incomplete). The old code discarded this to
    zero entities because it re-used the FULL production bar (0.85/0.85) as
    the lone-survivor gate, silently turning "ship with COVERAGE_LOW for
    review" (cad_validate._check_coverage's actual job) into "found nothing
    at all" — the user saw a blank decline for a photo that was 70% readable.
    An honest partial result must ship non-empty so cad_validate can flag
    it, exactly like the two-recognizer path already does for a failing
    score (see test_arbitration_falls_back_to_cv_when_neural_fails_coverage
    — that path never force-empties either)."""
    ink = _sheet()
    partial = _good_entities()[:2]  # both lines, circle missing
    cv_out = RecognizeOutput(entities=partial, thin_px=2, thick_px=4)
    result = arbitrate_recognition(
        ink, None, _FakeRecognizer("neural", None), _FakeRecognizer("cv", cv_out)
    )
    assert result.entities == partial
    assert result.recognizer_used == "cv"
    assert not result.score.ok  # below the production bar...
    assert result.score.precision == 1.0  # ...but zero hallucination
    assert result.score.recall >= 0.3  # ...and clearly not garbage either
