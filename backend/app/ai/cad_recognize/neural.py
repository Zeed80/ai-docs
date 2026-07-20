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

    def __init__(
        self,
        base_url: str | None = None,
        *,
        tile_size: int | None = None,
        tile_overlap: int = 160,
        endpoint: str = "/vectorize",
    ):
        self._base_url = base_url
        self._tile_size = tile_size
        self._tile_overlap = tile_overlap
        self._endpoint = endpoint

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
        if self._tile_size and (
            ink.shape[1] > self._tile_size or ink.shape[0] > self._tile_size
        ):
            return self._recognize_tiled(ink, exclusion_boxes or [])
        return self._recognize_once(ink, exclusion_boxes)

    def _recognize_once(
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

        url = self._url().rstrip("/") + self._endpoint
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

        logger.info(
            "neural_recognize",
            entities=len(entities),
            model_step=payload.get("model_step"),
        )
        return RecognizeOutput(
            entities=entities,
            # Neural doesn't claim raster-passthrough regions; arbitration
            # merges CV's regions later when needed.
            keep_raster=None,
            thin_px=2,
            thick_px=3,
            notes={"model_step": payload.get("model_step")},
        )

    def _recognize_tiled(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]],
    ) -> RecognizeOutput | None:
        """Infer overlapping local views, then stitch them in sheet space.

        Ownership cores prevent duplicate geometry in overlaps.  The global
        CV recognizer remains responsible for long strokes crossing tile
        boundaries; the local model handles bounded multi-type primitives.
        """
        from app.ai.cad_ir.schema import (
            AnnotationEntity,
            Arc,
            Circle,
            DimensionEntity,
            HatchRegion,
            Point,
            Polyline,
            Segment,
            TextEntity,
        )

        tile = int(self._tile_size or 0)
        overlap = self._tile_overlap
        if tile <= overlap:
            raise ValueError("tile_size must be greater than tile_overlap")
        height, width = ink.shape[:2]

        def origins(length: int) -> list[int]:
            if length <= tile:
                return [0]
            values = list(range(0, length - tile + 1, tile - overlap))
            if values[-1] != length - tile:
                values.append(length - tile)
            return values

        xs, ys = origins(width), origins(height)
        stitched = []
        model_steps = set()
        for row, y0 in enumerate(ys):
            for col, x0 in enumerate(xs):
                x1, y1 = min(x0 + tile, width), min(y0 + tile, height)
                local_boxes = []
                for bx0, by0, bx1, by1 in exclusion_boxes:
                    if bx1 <= x0 or by1 <= y0 or bx0 >= x1 or by0 >= y1:
                        continue
                    local_boxes.append(
                        (
                            max(0, bx0 - x0),
                            max(0, by0 - y0),
                            min(x1 - x0, bx1 - x0),
                            min(y1 - y0, by1 - y0),
                        )
                    )
                result = self._recognize_once(ink[y0:y1, x0:x1], local_boxes)
                if result is None:
                    continue
                if step := result.notes.get("model_step"):
                    model_steps.add(step)

                # Split each overlap at its midpoint; every predicted entity
                # therefore has exactly one owning tile.
                own_x0 = x0 if col == 0 else (x0 + xs[col - 1] + tile) / 2
                own_y0 = y0 if row == 0 else (y0 + ys[row - 1] + tile) / 2
                own_x1 = x1 if col == len(xs) - 1 else (x1 + xs[col + 1]) / 2
                own_y1 = y1 if row == len(ys) - 1 else (y1 + ys[row + 1]) / 2

                def center(entity) -> tuple[float, float]:
                    if isinstance(entity, Segment):
                        return (
                            (entity.p1.x + entity.p2.x) / 2,
                            (entity.p1.y + entity.p2.y) / 2,
                        )
                    if isinstance(entity, (Arc, Circle)):
                        return entity.center.x, entity.center.y
                    if isinstance(entity, Polyline):
                        return (
                            sum(point.x for point in entity.points) / len(entity.points),
                            sum(point.y for point in entity.points) / len(entity.points),
                        )
                    if isinstance(entity, HatchRegion):
                        return (
                            sum(point.x for point in entity.boundary) / len(entity.boundary),
                            sum(point.y for point in entity.boundary) / len(entity.boundary),
                        )
                    if isinstance(entity, DimensionEntity):
                        return (
                            (entity.p1.x + entity.p2.x) / 2,
                            (entity.p1.y + entity.p2.y) / 2,
                        )
                    if isinstance(entity, (TextEntity, AnnotationEntity)):
                        return entity.position.x, entity.position.y
                    return 0.0, 0.0

                def move(point):
                    return Point(x=point.x + x0, y=point.y + y0)

                for entity in result.entities:
                    cx, cy = center(entity)
                    if not (
                        own_x0 - x0 <= cx < own_x1 - x0
                        and own_y0 - y0 <= cy < own_y1 - y0
                    ):
                        continue
                    out = entity.model_copy(deep=True)
                    if isinstance(out, Segment):
                        out.p1, out.p2 = move(out.p1), move(out.p2)
                    elif isinstance(out, (Arc, Circle)):
                        out.center = move(out.center)
                    elif isinstance(out, Polyline):
                        out.points = [move(point) for point in out.points]
                    elif isinstance(out, HatchRegion):
                        out.boundary = [move(point) for point in out.boundary]
                        out.holes = [
                            [move(point) for point in hole] for hole in out.holes
                        ]
                    elif isinstance(out, DimensionEntity):
                        out.p1, out.p2 = move(out.p1), move(out.p2)
                    elif isinstance(out, (TextEntity, AnnotationEntity)):
                        out.position = move(out.position)
                        if isinstance(out, AnnotationEntity) and out.leader:
                            out.leader = move(out.leader)
                    stitched.append(out)
        if not stitched:
            return None
        return RecognizeOutput(
            entities=stitched,
            keep_raster=None,
            thin_px=2,
            thick_px=3,
            notes={
                "model_step": sorted(model_steps)[-1] if model_steps else None,
                "tiled": True,
                "tiles": len(xs) * len(ys),
            },
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
