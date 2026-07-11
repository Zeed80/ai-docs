"""IR -> Drawing/DrawingFeature bridge (Ф6.2)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.ai.cad_ir.adapters.to_drawing import promote_ir_to_drawing
from app.ai.cad_ir.schema import CadIR, Circle, Point, SourceInfo, TextEntity
from app.db.models import (
    DrawingFeature,
    DrawingFeatureType,
    FeatureContour,
    FeatureDimension,
    ImageGeneration,
    ImageGenStatus,
)


def _ir_with_holes_and_thread() -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,  # mm per px
        entities=[
            Circle(center=Point(x=100, y=100), radius=20, confidence=0.9),  # plain hole, d=20mm
            Circle(center=Point(x=250, y=100), radius=10, confidence=0.8),  # threaded hole, d=10mm
            TextEntity(position=Point(x=255, y=95), text="M20x1.5", height=10),
        ],
    )


@pytest.mark.asyncio
async def test_promote_ir_creates_drawing_and_hole_features(db_session):
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        prompt="Фланец", params={"svg_path": "x.svg"}, source_image_paths=[],
        accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    ir = _ir_with_holes_and_thread()
    drawing = await promote_ir_to_drawing(db_session, gen, ir, revision=3)
    await db_session.commit()

    assert drawing.id is not None
    assert drawing.drawing_number == "Фланец"
    assert drawing.metadata_["source_generation_id"] == str(gen.id)
    assert drawing.metadata_["cad_ir_revision"] == 3
    assert drawing.bounding_box["x_max"] == pytest.approx(200)  # 400px * 0.5 mm/px

    features = (
        await db_session.execute(
            sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id)
        )
    ).scalars().all()
    assert len(features) == 2
    types = sorted(f.feature_type for f in features)
    assert types == [DrawingFeatureType.hole, DrawingFeatureType.thread]


@pytest.mark.asyncio
async def test_promote_ir_thread_feature_gets_the_callout_as_name(db_session):
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[], accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    drawing = await promote_ir_to_drawing(db_session, gen, _ir_with_holes_and_thread(), revision=0)
    await db_session.commit()

    features = (
        await db_session.execute(sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id))
    ).scalars().all()
    thread = next(f for f in features if f.feature_type == DrawingFeatureType.thread)
    assert thread.name == "M20x1.5"


@pytest.mark.asyncio
async def test_promote_ir_hole_gets_contour_and_diameter_dimension(db_session):
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[], accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    drawing = await promote_ir_to_drawing(db_session, gen, _ir_with_holes_and_thread(), revision=0)
    await db_session.commit()

    hole = (
        await db_session.execute(
            sa.select(DrawingFeature).where(
                DrawingFeature.drawing_id == drawing.id,
                DrawingFeature.feature_type == DrawingFeatureType.hole,
            )
        )
    ).scalar_one()

    contour = (
        await db_session.execute(sa.select(FeatureContour).where(FeatureContour.feature_id == hole.id))
    ).scalar_one()
    assert contour.params["r"] == pytest.approx(10)  # 20px * 0.5 mm/px

    dim = (
        await db_session.execute(sa.select(FeatureDimension).where(FeatureDimension.feature_id == hole.id))
    ).scalar_one()
    assert dim.nominal == pytest.approx(20)  # diameter = 2*10mm


@pytest.mark.asyncio
async def test_promote_ir_does_not_cross_attribute_thread_to_the_wrong_hole(db_session):
    """Regression: two holes close together, one thread callout sitting
    within BOTH holes' independent search radius (a dense hole pattern —
    an ordinary layout, not a contrived edge case). Only the geometrically
    CLOSER circle may claim it; the other must fall back to a plain hole,
    not also claim the same thread text."""
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[], accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,
        entities=[
            Circle(center=Point(x=100, y=100), radius=10, confidence=0.9),  # close to the text
            Circle(center=Point(x=140, y=100), radius=10, confidence=0.9),  # farther, but still in ITS OWN threshold
            TextEntity(position=Point(x=105, y=95), text="M6", height=8),
        ],
    )
    drawing = await promote_ir_to_drawing(db_session, gen, ir, revision=0)
    await db_session.commit()

    features = (
        await db_session.execute(sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id))
    ).scalars().all()
    threads = [f for f in features if f.feature_type == DrawingFeatureType.thread]
    holes = [f for f in features if f.feature_type == DrawingFeatureType.hole]
    assert len(threads) == 1  # not 2 — the far circle must not also claim it
    assert len(holes) == 1


@pytest.mark.asyncio
async def test_promote_ir_two_separate_holes_can_each_get_their_own_thread(db_session):
    """Two DIFFERENT thread texts, each genuinely closest to its own circle
    — both should be recognized as threads, not just the first one found."""
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[], accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,
        entities=[
            Circle(center=Point(x=100, y=100), radius=10, confidence=0.9),
            Circle(center=Point(x=300, y=100), radius=10, confidence=0.9),
            TextEntity(position=Point(x=105, y=95), text="M6", height=8),
            TextEntity(position=Point(x=305, y=95), text="M8", height=8),
        ],
    )
    drawing = await promote_ir_to_drawing(db_session, gen, ir, revision=0)
    await db_session.commit()

    features = (
        await db_session.execute(sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id))
    ).scalars().all()
    thread_names = {f.name for f in features if f.feature_type == DrawingFeatureType.thread}
    assert thread_names == {"M6", "M8"}


@pytest.mark.asyncio
async def test_promote_ir_no_circles_yields_no_features(db_session):
    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[], accepted=True,
    )
    db_session.add(gen)
    await db_session.flush()

    ir = CadIR(source=SourceInfo(image_width=400, image_height=300), scale=0.5, entities=[])
    drawing = await promote_ir_to_drawing(db_session, gen, ir, revision=0)
    await db_session.commit()

    features = (
        await db_session.execute(sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id))
    ).scalars().all()
    assert features == []
