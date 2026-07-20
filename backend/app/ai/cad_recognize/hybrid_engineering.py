"""Conservative CV + primitive-set fusion.

CV remains authoritative for long/global geometry.  The learned detector may
only add circles/arcs that are strongly supported by source ink and are not a
duplicate of an existing CV primitive.  The candidate stays opt-in until the
entity-level promotion gate passes.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.ai.cad_ir.png_render import rasterize_entities
from app.ai.cad_ir.schema import Arc, Circle, Entity
from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.cv import CvRecognizer
from app.ai.cad_recognize.primitive_set import PrimitiveSetRecognizer


def _same_round_primitive(left: Entity, right: Entity) -> bool:
    if not isinstance(left, (Circle, Arc)) or not isinstance(right, type(left)):
        return False
    center_gap = math.dist(
        (left.center.x, left.center.y),
        (right.center.x, right.center.y),
    )
    radius = max(left.radius, right.radius, 1.0)
    return center_gap <= max(3.0, 0.08 * radius) and abs(left.radius - right.radius) <= max(
        2.0, 0.08 * radius
    )


def _ink_precision(entity: Entity, ink: Any, thin_px: int, thick_px: int) -> float:
    import cv2

    ink_bool = np.asarray(ink) > 0
    height, width = ink_bool.shape[:2]
    drawn = rasterize_entities([entity], width, height, thin_px, thick_px) < 128
    count = int(drawn.sum())
    if count == 0:
        return 0.0
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    nearby_ink = cv2.dilate(ink_bool.astype(np.uint8), kernel) > 0
    return float((nearby_ink & drawn).sum()) / count


class HybridEngineeringRecognizer:
    name = "hybrid-engineering"

    def __init__(
        self,
        *,
        cv: CvRecognizer | None = None,
        primitive: PrimitiveSetRecognizer | None = None,
        min_confidence: float = 0.80,
        min_ink_precision: float = 0.90,
    ):
        self.cv = cv or CvRecognizer()
        self.primitive = primitive or PrimitiveSetRecognizer()
        self.min_confidence = min_confidence
        self.min_ink_precision = min_ink_precision

    def recognize(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> RecognizeOutput | None:
        cv_out = self.cv.recognize(ink, exclusion_boxes)
        learned_out = self.primitive.recognize(ink, exclusion_boxes)
        if cv_out is None:
            return learned_out
        if learned_out is None:
            return cv_out

        accepted: list[Entity] = []
        rejected_low_confidence = 0
        rejected_unsupported = 0
        rejected_duplicate = 0
        for entity in learned_out.entities:
            if not isinstance(entity, (Circle, Arc)):
                continue
            if entity.confidence < self.min_confidence:
                rejected_low_confidence += 1
                continue
            if any(_same_round_primitive(entity, existing) for existing in cv_out.entities):
                rejected_duplicate += 1
                continue
            precision = _ink_precision(
                entity,
                ink,
                cv_out.thin_px,
                cv_out.thick_px,
            )
            if precision < self.min_ink_precision:
                rejected_unsupported += 1
                continue
            accepted.append(entity)

        return RecognizeOutput(
            entities=[*cv_out.entities, *accepted],
            keep_raster=cv_out.keep_raster,
            thin_px=cv_out.thin_px,
            thick_px=cv_out.thick_px,
            notes={
                **cv_out.notes,
                "primitive_round_entities_added": len(accepted),
                "primitive_rejected_low_confidence": rejected_low_confidence,
                "primitive_rejected_unsupported": rejected_unsupported,
                "primitive_rejected_duplicate": rejected_duplicate,
            },
        )
