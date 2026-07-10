"""Accepted CAD IR -> Drawing + DrawingFeature rows (Ф6.2) — the first nить
of "чертёж → элемент → операция ТП": promotes a vectorize/blank-sheet result
into the SAME ``Drawing``/``DrawingFeature`` models that
``tp_generator.generate_process_plan_from_drawing`` already consumes for
scanned/uploaded drawings, so an accepted studio drawing gets a route into
the technology module without any new tp-generator code.

Deliberately scoped to circular features (holes/threads) — the single most
directly machining-relevant, unambiguous feature type IR already represents
cleanly. Full contour/GD&T/surface-finish promotion is a larger, separate
piece of work left for later (assurance/provenance carries over fine either
way since it's just more DrawingFeature rows on the same Drawing).
"""

from __future__ import annotations

import math
import re
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_ir.schema import CadIR, Circle, Entity, Point
from app.db.models import (
    Drawing,
    DrawingFeature,
    DrawingFeatureType,
    DrawingStatus,
    FeatureContour,
    FeatureDimension,
    FeatureDimType,
    FeaturePrimitiveType,
)

if TYPE_CHECKING:
    from app.db.models import ImageGeneration

_THREAD_PATTERN = re.compile(r"M\d+(?:[x×]\d+(?:\.\d+)?)?", re.IGNORECASE)
_THREAD_SEARCH_RADIUS_FACTOR = 4.0
_THREAD_SEARCH_RADIUS_MIN_PX = 20.0


def _entity_anchor(entity: Entity) -> Point | None:
    """A representative point for proximity search — center for circular
    things, position for text, midpoint for anything with two endpoints."""
    if getattr(entity, "center", None) is not None:
        return entity.center
    if getattr(entity, "position", None) is not None:
        return entity.position
    p1, p2 = getattr(entity, "p1", None), getattr(entity, "p2", None)
    if p1 is not None and p2 is not None:
        return Point(x=(p1.x + p2.x) / 2, y=(p1.y + p2.y) / 2)
    return None


def _thread_text_near(ir: CadIR, circle: Circle) -> str | None:
    """A thread callout (``M20x1.5``-like text) close enough to this circle
    to plausibly annotate it — same proximity-matching spirit as
    ``cad_hypothesis._axis_bonus_for_class``, just for a different question."""
    threshold = max(circle.radius * _THREAD_SEARCH_RADIUS_FACTOR, _THREAD_SEARCH_RADIUS_MIN_PX)
    best: str | None = None
    best_d = threshold
    for e in ir.entities:
        text = getattr(e, "text", None)
        if not text or not _THREAD_PATTERN.search(text):
            continue
        anchor = _entity_anchor(e)
        if anchor is None:
            continue
        d = math.hypot(anchor.x - circle.center.x, anchor.y - circle.center.y)
        if d < best_d:
            best_d = d
            best = text
    return best


async def promote_ir_to_drawing(
    db: AsyncSession, gen: "ImageGeneration", ir: CadIR, revision: int
) -> Drawing:
    """Create (and flush) a Drawing row plus one DrawingFeature per circle
    entity in the IR — holes by default, threads when a callout is found
    nearby. Caller owns the transaction (commits or not)."""
    scale = ir.scale or 1.0
    units = "mm" if ir.scale else "px"
    drawing = Drawing(
        document_id=gen.source_document_id,
        drawing_number=gen.prompt or f"STUDIO-{gen.id}",
        filename=f"studio-{gen.id}.dxf",
        format="dxf",
        svg_path=(gen.params or {}).get("svg_path"),
        thumbnail_path=gen.thumbnail_path,
        title_block=ir.sheet.title_block or None,
        bounding_box={
            "x_min": 0.0, "y_min": 0.0,
            "x_max": ir.source.image_width * scale, "y_max": ir.source.image_height * scale,
            "units": units,
        },
        is_confidential=True,
        drawing_type="detail",
        status=DrawingStatus.analyzed,
        metadata_={
            "source": "studio_vectorize",
            "source_generation_id": str(gen.id),
            "cad_ir_revision": revision,
        },
    )
    db.add(drawing)
    await db.flush()

    order = 0
    for entity in ir.entities:
        if not isinstance(entity, Circle):
            continue
        diameter_mm = 2 * entity.radius * scale
        thread_text = _thread_text_near(ir, entity)
        if thread_text:
            feature = DrawingFeature(
                drawing_id=drawing.id,
                feature_type=DrawingFeatureType.thread,
                name=thread_text,
                confidence=entity.confidence,
                sort_order=order,
                ai_raw={"cad_ir_entity_id": entity.id, "cad_ir_revision": revision},
            )
        else:
            feature = DrawingFeature(
                drawing_id=drawing.id,
                feature_type=DrawingFeatureType.hole,
                name=f"Отверстие ⌀{diameter_mm:g}",
                confidence=entity.confidence,
                sort_order=order,
                ai_raw={"cad_ir_entity_id": entity.id, "cad_ir_revision": revision},
            )
        db.add(feature)
        await db.flush()
        db.add(
            FeatureContour(
                feature_id=feature.id,
                primitive_type=FeaturePrimitiveType.circle,
                params={
                    "cx": entity.center.x * scale, "cy": entity.center.y * scale,
                    "r": entity.radius * scale,
                },
                is_user_edited=entity.origin == "human",
            )
        )
        db.add(
            FeatureDimension(
                feature_id=feature.id,
                dim_type=FeatureDimType.diameter,
                nominal=diameter_mm,
                label=f"⌀{diameter_mm:g}",
            )
        )
        order += 1

    await db.flush()
    return drawing
