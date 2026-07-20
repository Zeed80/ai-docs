"""Learned raster evidence followed by deterministic CAD primitive fitting."""

from __future__ import annotations

from typing import Any

import structlog

from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.cv import CvRecognizer

logger = structlog.get_logger()


class EvidenceHeatmapRecognizer:
    name = "evidence-heatmap"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        tile_size: int = 640,
        tile_overlap: int = 160,
    ):
        self._base_url = base_url
        self.tile_size = tile_size
        self.tile_overlap = tile_overlap

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
        import numpy as np

        height, width = ink.shape[:2]
        if self.tile_size <= self.tile_overlap:
            raise ValueError("tile_size must be greater than tile_overlap")

        def origins(length: int) -> list[int]:
            if length <= self.tile_size:
                return [0]
            values = list(
                range(0, length - self.tile_size + 1, self.tile_size - self.tile_overlap)
            )
            if values[-1] != length - self.tile_size:
                values.append(length - self.tile_size)
            return values

        predicted = np.zeros((height, width), dtype=np.uint8)
        model_steps = set()
        tile_count = 0
        for y0 in origins(height):
            for x0 in origins(width):
                x1 = min(x0 + self.tile_size, width)
                y1 = min(y0 + self.tile_size, height)
                photo_like = cv2.bitwise_not(ink[y0:y1, x0:x1])
                ok, encoded = cv2.imencode(".png", photo_like)
                if not ok:
                    continue
                try:
                    response = httpx.post(
                        self._url().rstrip("/") + "/predict-evidence",
                        files={"file": ("tile.png", encoded.tobytes(), "image/png")},
                        timeout=30,
                    )
                    response.raise_for_status()
                    local = cv2.imdecode(
                        np.frombuffer(response.content, dtype=np.uint8),
                        cv2.IMREAD_GRAYSCALE,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cad_evidence_unavailable", error=str(exc)[:200])
                    return None
                if local is None:
                    continue
                local = np.where(local >= 128, 255, 0).astype(np.uint8)
                predicted[y0:y1, x0:x1] = np.maximum(
                    predicted[y0:y1, x0:x1],
                    local[: y1 - y0, : x1 - x0],
                )
                if step := response.headers.get("x-cad-evidence-step"):
                    model_steps.add(step)
                tile_count += 1
        if not np.any(predicted):
            logger.info("cad_evidence_empty")
            return None
        # Evidence is a selector, not a replacement drawing. Fitting the
        # network's thick/resampled mask moves centerlines and destroys
        # entity-level coordinate accuracy. Dilate only to tolerate small
        # heatmap offsets, then retain the ORIGINAL source ink pixels.
        support = cv2.dilate(
            predicted,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
        )
        filtered_ink = cv2.bitwise_and(ink, support)
        if not np.any(filtered_ink):
            logger.info("cad_evidence_no_source_supported_pixels")
            return None
        for x0, y0, x1, y1 in exclusion_boxes or []:
            filtered_ink[max(0, y0):y1, max(0, x0):x1] = 0
        fitted = CvRecognizer().recognize(filtered_ink, exclusion_boxes=[])
        if fitted is None:
            return None
        # Geometry came from a learned evidence map.  The deterministic fit
        # does not upgrade its assurance or disguise it as source-direct CV.
        entities = []
        for entity in fitted.entities:
            entities.append(
                entity.model_copy(
                    update={
                        "origin": "neural",
                        "assurance": "inferred",
                        "evidence": [
                            *entity.evidence,
                            "learned-evidence-heatmap",
                            f"model-step:{sorted(model_steps)[-1] if model_steps else ''}",
                        ],
                    }
                )
            )
        return RecognizeOutput(
            entities=entities,
            keep_raster=None,
            thin_px=fitted.thin_px,
            thick_px=fitted.thick_px,
            notes={
                "model_step": sorted(model_steps)[-1] if model_steps else None,
                "evidence_pixels": int(np.count_nonzero(predicted)),
                "source_supported_pixels": int(np.count_nonzero(filtered_ink)),
                "source_coordinates_preserved": True,
                "deterministic_fitter": "cv",
                "tiled": tile_count > 1,
                "tiles": tile_count,
            },
        )
