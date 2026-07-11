"""Neural recognition backend: HTTP client for the ``technical-vectorizer``
inference service (infra/technical-vectorizer).

Wraps a vendored, openly licensed (MPL-2.0), ALREADY-TRAINED model — Deep
Vectorization of Technical Drawings (Egiazarian et al., ECCV 2020,
github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings) — instead of
the from-scratch Drawing2CAD-style seq2seq model in ``neural.py``/
``infra/cad-vectorizer``. That model targets a different task (vector input
→ CAD command sequence) and, trained only on synthetic data, never beat CV
on real photos (recall ~0). This one targets exactly our task (raster
technical drawing → vector line primitives) and was validated live
(2026-07-11) at recall +3.8..+23.5 points over the CV baseline, zero-shot,
on real test photos — see project notes / linear-riding-tome.md.

Same contract and trust posture as ``neural.py``: entities land with
``origin="neural"``/``assurance="inferred"``, ``verify.arbitrate_recognition``
scores them against source ink exactly like CV, and any failure (service
down, timeout, malformed response) declines (returns ``None``) — CV is
always the fallback.

Only line primitives (Segment) — the companion "curve" model was tested and
found to hurt precision more than it helps recall on this project's
drawings (see project notes); circles/arcs stay on the CV path.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.ai.cad_recognize.base import RecognizeOutput

logger = structlog.get_logger()

_TIMEOUT_S = 30.0


class TechnicalVectorizerRecognizer:
    name = "technical_vectorizer"

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url

    def _url(self) -> str:
        if self._base_url:
            return self._base_url
        from app.config import settings

        return settings.technical_vectorizer_url

    def recognize(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> RecognizeOutput | None:
        import cv2
        import httpx

        # Same convention mismatch as neural.py: `ink` is a binarized mask
        # (255=ink, 0=background); the model was trained on/serves normal
        # photos (dark ink on a light sheet). Invert before it leaves this
        # process.
        photo_like = cv2.bitwise_not(ink)
        ok, buf = cv2.imencode(".png", photo_like)
        if not ok:
            logger.warning("technical_vectorizer_encode_failed")
            return None

        url = self._url().rstrip("/") + "/vectorize"
        try:
            resp = httpx.post(
                url,
                files={"file": ("ink.png", buf.tobytes(), "image/png")},
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — service down/slow/malformed: decline, don't crash the pipeline
            logger.warning("technical_vectorizer_unavailable", error=str(exc)[:200])
            return None

        entities = _parse_segments(payload.get("segments", []))
        if not entities:
            logger.info("technical_vectorizer_empty")
            return None

        logger.info("technical_vectorizer_recognize", entities=len(entities))
        return RecognizeOutput(
            entities=entities,
            keep_raster=None,  # this backend doesn't claim raster-passthrough regions
            thin_px=2,
            thick_px=4,
            notes={},
        )


def _parse_segments(raw: list[dict]):
    from app.ai.cad_ir.schema import Point, Segment

    out = []
    for item in raw:
        try:
            out.append(
                Segment(
                    p1=Point(x=item["x1"], y=item["y1"]),
                    p2=Point(x=item["x2"], y=item["y2"]),
                    line_class="contour",
                    width_class="main",
                    confidence=max(0.0, min(1.0, item["confidence"])),
                    origin="neural",
                )
            )
        except Exception as exc:  # noqa: BLE001 — one malformed entity must not drop the whole batch
            logger.warning("technical_vectorizer_entity_parse_failed", error=str(exc)[:160])
    return out
