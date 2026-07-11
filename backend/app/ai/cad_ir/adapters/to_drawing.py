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


def _match_thread_texts_to_circles(ir: CadIR, circles: list[Circle]) -> dict[str, str]:
    """circle.id -> thread callout text, via GLOBAL greedy nearest-neighbor
    matching — not "is this text within MY threshold" evaluated per circle
    independently. The independent version let a single thread label get
    attributed to TWO circles when they sat close together (a common
    pattern: a row of threaded holes with one shared "4×M6" callout, or
    just two holes 20-40px apart with one label near the pair) — each
    circle would separately find that same text as "close enough" and both
    would claim it. Greedy nearest-pair-first matching (classic stable
    matching approximation) guarantees a given text claims at most one
    circle, and a given circle gets at most one text."""
    # (distance, circle_id, text_entity_id, text) — matched/claimed by the
    # text-bearing ENTITY's own IR id, not its string value (two separate
    # holes can legitimately carry the identical text "M6").
    candidates: list[tuple[float, str, str, str]] = []
    for e in ir.entities:
        text = getattr(e, "text", None)
        if not text or not _THREAD_PATTERN.search(text):
            continue
        anchor = _entity_anchor(e)
        if anchor is None:
            continue
        for circle in circles:
            threshold = max(circle.radius * _THREAD_SEARCH_RADIUS_FACTOR, _THREAD_SEARCH_RADIUS_MIN_PX)
            d = math.hypot(anchor.x - circle.center.x, anchor.y - circle.center.y)
            if d <= threshold:
                candidates.append((d, circle.id, e.id, text))

    candidates.sort(key=lambda c: c[0])
    matched: dict[str, str] = {}
    claimed_text_entities: set[str] = set()
    for _d, circle_id, text_entity_id, text in candidates:
        if circle_id in matched or text_entity_id in claimed_text_entities:
            continue
        matched[circle_id] = text
        claimed_text_entities.add(text_entity_id)
    return matched


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

    circles = [e for e in ir.entities if isinstance(e, Circle)]
    thread_by_circle_id = _match_thread_texts_to_circles(ir, circles)

    order = 0
    for entity in circles:
        diameter_mm = 2 * entity.radius * scale
        thread_text = thread_by_circle_id.get(entity.id)
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
