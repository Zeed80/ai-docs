"""Technical-vectorizer recognizer client (backend/app/ai/cad_recognize/
technical_vectorizer.py) — the primary neural recognizer since 2026-07-11,
replacing the from-scratch neural.py model in arbitrate_recognition (see
project notes: that model never beat CV on real photos; this one, wrapping
a vendored openly-licensed pretrained model, does — validated live)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_recognize.technical_vectorizer import TechnicalVectorizerRecognizer


def _sheet() -> np.ndarray:
    ink = np.zeros((300, 400), dtype=np.uint8)
    cv2.line(ink, (40, 40), (360, 40), 255, 4)
    cv2.line(ink, (40, 40), (40, 260), 255, 4)
    return ink


def test_technical_vectorizer_declines_on_connection_error():
    rec = TechnicalVectorizerRecognizer(base_url="http://technical-vectorizer-does-not-exist.invalid:1")
    assert rec.recognize(_sheet()) is None


def test_technical_vectorizer_parses_valid_response():
    rec = TechnicalVectorizerRecognizer(base_url="http://fake")
    payload = {
        "segments": [
            {"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0, "confidence": 0.9, "width": 2.0},
        ],
        "checkpoint_loaded": True,
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
    assert out.entities[0].type == "segment"
    assert out.entities[0].p1.x == 1.0


def test_technical_vectorizer_skips_malformed_segments_but_keeps_valid_ones():
    rec = TechnicalVectorizerRecognizer(base_url="http://fake")
    payload = {
        "segments": [
            {"x1": 1.0, "y1": 2.0},  # missing x2/y2 -> invalid
            {"x1": 5.0, "y1": 6.0, "x2": 7.0, "y2": 8.0, "confidence": 0.5, "width": 1.5},
        ],
        "checkpoint_loaded": True,
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    with patch("httpx.post", return_value=resp):
        out = rec.recognize(_sheet())
    assert out is not None
    assert len(out.entities) == 1
    assert out.entities[0].p1.x == 5.0


def test_technical_vectorizer_clamps_confidence_out_of_range():
    rec = TechnicalVectorizerRecognizer(base_url="http://fake")
    payload = {
        "segments": [
            {"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0, "confidence": 64.0, "width": 2.0},
        ],
        "checkpoint_loaded": True,
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    with patch("httpx.post", return_value=resp):
        out = rec.recognize(_sheet())
    assert out is not None
    assert out.entities[0].confidence == 1.0


def test_technical_vectorizer_declines_on_empty_segments():
    rec = TechnicalVectorizerRecognizer(base_url="http://fake")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"segments": [], "checkpoint_loaded": True}
    with patch("httpx.post", return_value=resp):
        out = rec.recognize(_sheet())
    assert out is None
