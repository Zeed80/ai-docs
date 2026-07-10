"""Neural recognition backend: HTTP client for the ``cad-vectorizer``
inference service (infra/cad-vectorizer, Ф3).

Contract with the model is one-directional trust: whatever the network
proposes lands with ``origin="neural"``/``assurance="inferred"`` — the SAME
bottom rung a first-pass CV read gets. This client makes no claim of truth;
``verify.arbitrate_recognition`` scores the proposal against the source ink
exactly like it scores CV, and only promotes assurance on independent
cross-checks (never inside this module). On any failure (service down,
timeout, malformed response) it declines (returns ``None``) — the caller
always has the CV backend to fall back to, so neural is additive, not a
single point of failure for the pipeline.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.ai.cad_recognize.base import RecognizeOutput

logger = structlog.get_logger()

_TIMEOUT_S = 30.0


class NeuralRecognizer:
    name = "neural"

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url

    def _url(self) -> str:
        if self._base_url:
            return self._base_url
        from app.config import settings

        return settings.cad_vectorizer_url

    def recognize(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> RecognizeOutput | None:
        import cv2
        import httpx

        # Contract mismatch guard: `ink` is a binarized MASK (255=ink,
        # 0=background — drawing_vectorize.py's convention, what every CV
        # caller passes). The model was trained on/serves NORMAL PHOTOS
        # (dark ink on a light sheet) — serve.py inverts (1 - pixel/255) to
        # get an ink-as-signal tensor, exactly like tools/cad-dataset
        # training pairs. Sending the mask as-is would double-invert
        # relative to training (ink reads as background and vice versa),
        # which is degenerate input the model never saw. Convert back to
        # photo convention before it leaves this process.
        photo_like = cv2.bitwise_not(ink)
        ok, buf = cv2.imencode(".png", photo_like)
        if not ok:
            logger.warning("neural_recognize_encode_failed")
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
            logger.warning("neural_recognize_unavailable", error=str(exc)[:200])
            return None

        entities = _parse_entities(payload.get("entities", []))
        if not entities:
            logger.info("neural_recognize_empty", model_step=payload.get("model_step"))
            return None

        logger.info("neural_recognize", entities=len(entities), model_step=payload.get("model_step"))
        return RecognizeOutput(
            entities=entities,
            keep_raster=None,  # neural doesn't claim raster-passthrough regions; caller merges with CV's if needed
            thin_px=2,
            thick_px=3,
            notes={"model_step": payload.get("model_step")},
        )


def _parse_entities(raw: list[dict]):
    from pydantic import TypeAdapter

    from app.ai.cad_ir.schema import Entity

    adapter = TypeAdapter(Entity)
    out = []
    for item in raw:
        try:
            out.append(adapter.validate_python(item))
        except Exception as exc:  # noqa: BLE001 — one malformed entity must not drop the whole batch
            logger.warning("neural_entity_parse_failed", error=str(exc)[:160])
    return out
