"""Tests for the drawing feature correction and few-shot learning endpoints."""

import uuid
import pytest
from httpx import AsyncClient

from app.db.models import Drawing, DrawingStatus, DrawingFeature, DrawingFeatureType, DrawingFeatureCorrection


@pytest.fixture
async def drawing_with_features(db_session):
    d = Drawing(
        filename="shaft_test.dxf",
        format="dxf",
        drawing_number="TST-001",
        status=DrawingStatus.analyzed,
        metadata_={"drawing_type": "detail"},
    )
    db_session.add(d)
    await db_session.flush()

    feats = [
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.hole,
            name="Отверстие Ø10",
            confidence=0.3,   # uncertain
        ),
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.surface,
            name="Шейка Ø50h6",
            confidence=0.85,  # confident
        ),
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.pocket,
            name="Паз неизвестный",
            confidence=0.55,  # uncertain
        ),
    ]
    for f in feats:
        db_session.add(f)
    await db_session.commit()
    return d, feats


# ── GET /drawings/{id}/uncertain-features ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_uncertain_features_returns_low_confidence(
    client: AsyncClient, drawing_with_features
):
    drawing, feats = drawing_with_features
    resp = await client.get(f"/api/drawings/{drawing.id}/uncertain-features")
    assert resp.status_code == 200
    data = resp.json()
    names = [f["name"] for f in data]
    assert "Отверстие Ø10" in names
    assert "Паз неизвестный" in names
    # confident feature excluded
    assert "Шейка Ø50h6" not in names


@pytest.mark.asyncio
async def test_get_uncertain_features_ordered_by_confidence(
    client: AsyncClient, drawing_with_features
):
    drawing, _ = drawing_with_features
    resp = await client.get(f"/api/drawings/{drawing.id}/uncertain-features")
    assert resp.status_code == 200
    data = resp.json()
    confidences = [f["confidence"] for f in data]
    assert confidences == sorted(confidences)


@pytest.mark.asyncio
async def test_get_uncertain_features_empty_when_all_confident(
    client: AsyncClient, db_session
):
    d = Drawing(
        filename="perfect.dxf", format="dxf", status=DrawingStatus.analyzed,
        metadata_={"drawing_type": "detail"},
    )
    db_session.add(d)
    await db_session.flush()
    feat = DrawingFeature(
        drawing_id=d.id, feature_type=DrawingFeatureType.hole,
        name="Уверенное отверстие", confidence=0.95,
    )
    db_session.add(feat)
    await db_session.commit()

    resp = await client.get(f"/api/drawings/{d.id}/uncertain-features")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_uncertain_features_custom_threshold(
    client: AsyncClient, drawing_with_features
):
    drawing, _ = drawing_with_features
    # threshold=0.5 — only the 0.3 feature should appear
    resp = await client.get(
        f"/api/drawings/{drawing.id}/uncertain-features", params={"threshold": 0.5}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert all(f["confidence"] < 0.5 for f in data)


@pytest.mark.asyncio
async def test_get_uncertain_features_drawing_not_found(client: AsyncClient):
    resp = await client.get(f"/api/drawings/{uuid.uuid4()}/uncertain-features")
    assert resp.status_code == 404


# ── POST /drawings/{id}/features/{fid}/correct ────────────────────────────────


@pytest.mark.asyncio
async def test_correct_feature_updates_type(
    client: AsyncClient, drawing_with_features, db_session
):
    drawing, feats = drawing_with_features
    uncertain = feats[0]  # hole, confidence=0.3

    resp = await client.post(
        f"/api/drawings/{drawing.id}/features/{uncertain.id}/correct",
        json={"original_type": "hole", "corrected_type": "key_slot"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["feature_type"] == "key_slot"
    assert data["confidence"] == 1.0


@pytest.mark.asyncio
async def test_correct_feature_sets_reviewed_at(
    client: AsyncClient, drawing_with_features, db_session
):
    drawing, feats = drawing_with_features
    uncertain = feats[0]

    resp = await client.post(
        f"/api/drawings/{drawing.id}/features/{uncertain.id}/correct",
        json={"original_type": "hole", "corrected_type": "groove"},
    )
    assert resp.status_code == 200
    assert resp.json()["reviewed_at"] is not None


@pytest.mark.asyncio
async def test_correct_feature_updates_name(
    client: AsyncClient, drawing_with_features
):
    drawing, feats = drawing_with_features
    uncertain = feats[2]  # pocket, confidence=0.55

    resp = await client.post(
        f"/api/drawings/{drawing.id}/features/{uncertain.id}/correct",
        json={
            "original_type": "pocket",
            "corrected_type": "key_slot",
            "corrected_name": "Шпоночный паз 12×6",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Шпоночный паз 12×6"


@pytest.mark.asyncio
async def test_correct_feature_stores_correction_record(
    client: AsyncClient, drawing_with_features, db_session
):
    from sqlalchemy import select
    drawing, feats = drawing_with_features
    uncertain = feats[0]

    await client.post(
        f"/api/drawings/{drawing.id}/features/{uncertain.id}/correct",
        json={"original_type": "hole", "corrected_type": "center_bore"},
    )

    result = await db_session.execute(
        select(DrawingFeatureCorrection).where(
            DrawingFeatureCorrection.feature_id == uncertain.id
        )
    )
    record = result.scalar_one_or_none()
    assert record is not None
    assert record.corrected_type == "center_bore"
    assert record.original_type == "hole"
    assert record.confidence_at_correction == pytest.approx(0.3, abs=0.01)


@pytest.mark.asyncio
async def test_correct_feature_records_corrected_by(
    client: AsyncClient, drawing_with_features, db_session
):
    from sqlalchemy import select
    drawing, feats = drawing_with_features

    await client.post(
        f"/api/drawings/{drawing.id}/features/{feats[0].id}/correct",
        json={
            "original_type": "hole",
            "corrected_type": "groove",
            "corrected_by": "technologist_ivanov",
        },
    )
    result = await db_session.execute(
        select(DrawingFeatureCorrection).where(
            DrawingFeatureCorrection.feature_id == feats[0].id
        )
    )
    record = result.scalar_one_or_none()
    assert record.corrected_by == "technologist_ivanov"


@pytest.mark.asyncio
async def test_correct_feature_not_found(client: AsyncClient, drawing_with_features):
    drawing, _ = drawing_with_features
    resp = await client.post(
        f"/api/drawings/{drawing.id}/features/{uuid.uuid4()}/correct",
        json={"original_type": "hole", "corrected_type": "groove"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_correct_feature_wrong_drawing(
    client: AsyncClient, drawing_with_features, db_session
):
    drawing, feats = drawing_with_features
    other_drawing = Drawing(
        filename="other.dxf", format="dxf", status=DrawingStatus.analyzed,
    )
    db_session.add(other_drawing)
    await db_session.commit()

    resp = await client.post(
        f"/api/drawings/{other_drawing.id}/features/{feats[0].id}/correct",
        json={"original_type": "hole", "corrected_type": "groove"},
    )
    assert resp.status_code == 404


# ── Few-shot loading ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_few_shot_format(db_session):
    """_load_few_shot_corrections returns list with description and correct_type keys."""
    from app.tasks.drawing_analysis import _load_few_shot_corrections

    d = Drawing(filename="fs_test.dxf", format="dxf", status=DrawingStatus.analyzed)
    db_session.add(d)
    await db_session.flush()
    feat = DrawingFeature(
        drawing_id=d.id, feature_type=DrawingFeatureType.hole,
        name="Тестовое отверстие", confidence=0.4,
    )
    db_session.add(feat)
    await db_session.flush()

    correction = DrawingFeatureCorrection(
        drawing_id=d.id,
        feature_id=feat.id,
        original_type="hole",
        corrected_type="groove",
        original_name="Тестовое отверстие",
        confidence_at_correction=0.4,
        drawing_type="detail",
        corrected_by="test_user",
    )
    db_session.add(correction)
    await db_session.commit()

    examples = await _load_few_shot_corrections(db_session, drawing_type="detail", limit=10)
    assert len(examples) >= 1
    ex = examples[0]
    assert "description" in ex
    assert "correct_type" in ex
    assert ex["correct_type"] == "groove"


@pytest.mark.asyncio
async def test_few_shot_filters_by_drawing_type(db_session):
    """_load_few_shot_corrections only returns corrections for matching drawing_type."""
    from app.tasks.drawing_analysis import _load_few_shot_corrections

    d = Drawing(filename="assembly_test.dxf", format="dxf", status=DrawingStatus.analyzed)
    db_session.add(d)
    await db_session.flush()
    feat = DrawingFeature(
        drawing_id=d.id, feature_type=DrawingFeatureType.hole,
        name="Позиция 1", confidence=0.4,
    )
    db_session.add(feat)
    await db_session.flush()

    correction = DrawingFeatureCorrection(
        drawing_id=d.id,
        feature_id=feat.id,
        original_type="hole",
        corrected_type="balloon",
        original_name="Позиция 1",
        confidence_at_correction=0.4,
        drawing_type="assembly",
        corrected_by="test_user",
    )
    db_session.add(correction)
    await db_session.commit()

    # Should return assembly-type corrections
    assembly_examples = await _load_few_shot_corrections(db_session, drawing_type="assembly")
    assert any(ex["correct_type"] == "balloon" for ex in assembly_examples)

    # Should NOT return for detail type
    detail_examples = await _load_few_shot_corrections(db_session, drawing_type="detail")
    assert not any(ex["correct_type"] == "balloon" for ex in detail_examples)
