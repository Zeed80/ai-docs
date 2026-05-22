"""Tests for new technology API endpoints (Epic 7)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Drawing,
    DrawingFeature,
    DrawingFeatureType,
    DrawingStatus,
    ManufacturingOperation,
    ManufacturingProcessPlan,
    NormControlCheck,
    SurfaceMachiningSpec,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def analyzed_drawing(db_session: AsyncSession):
    d = Drawing(
        filename="shaft-v2.dxf",
        format="dxf",
        drawing_number="ЧТЖ-007",
        status=DrawingStatus.analyzed,
    )
    db_session.add(d)
    await db_session.flush()

    for i, ftype in enumerate(
        [DrawingFeatureType.hole, DrawingFeatureType.surface]
    ):
        feat = DrawingFeature(
            drawing_id=d.id,
            feature_type=ftype,
            name=f"Элемент {i+1}",
            confidence=0.9,
            ai_raw={"nominal_mm": 30.0 + i * 10, "roughness_ra": 1.6},
        )
        db_session.add(feat)

    await db_session.commit()
    return d


@pytest.fixture
async def process_plan(db_session: AsyncSession):
    plan = ManufacturingProcessPlan(
        product_name="Вал ступенчатый",
        product_code="75.0001",
        version="1.0",
        standard_system="ЕСТД",
        tp_type="единичный",
        material="Ст.45",
        blank_type="прокат",
        normcontrol_status="not_checked",
        created_by="test",
    )
    db_session.add(plan)
    await db_session.commit()
    return plan


@pytest.fixture
async def plan_with_operations(db_session: AsyncSession, process_plan):
    for i, (name, op_type) in enumerate(
        [("Токарная", "turning"), ("Фрезерная", "milling"), ("Контроль", "quality_control")]
    ):
        op = ManufacturingOperation(
            process_plan_id=process_plan.id,
            sequence_no=(i + 1) * 5,
            name=name,
            operation_type=op_type,
            operation_code=f"{4110 + i * 10}",
            machine_resource_id=None,
            transition_text=f"1. Выполнить {name.lower()}.",
            control_requirements="Проверить размер",
            cutting_parameters={"vc_m_min": 100} if op_type != "quality_control" else None,
            tsht_k_minutes=5.0 + i,
            to_minutes=3.0 + i * 0.5,
        )
        db_session.add(op)
    await db_session.commit()
    return process_plan


# ── POST generate-from-drawing ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_tp_from_drawing_creates_plan(
    client: AsyncClient, analyzed_drawing: Drawing
):
    """POST generate-from-drawing → returns plan_id and task_id."""
    mock_celery = MagicMock()
    mock_celery.apply_async.return_value = MagicMock(id="mock-task-id")
    with patch("app.api.technology.celery_tp_task", mock_celery):
        resp = await client.post(
            "/api/technology/process-plans/generate-from-drawing",
            json={
                "drawing_id": str(analyzed_drawing.id),
                "tp_type": "единичный",
                "batch_size": 10,
                "auto_normcontrol": True,
                "created_by": "test",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "plan_id" in data
    assert "task_id" in data


@pytest.mark.asyncio
async def test_generate_tp_missing_drawing_id(client: AsyncClient):
    resp = await client.post(
        "/api/technology/process-plans/generate-from-drawing",
        json={"tp_type": "единичный"},
    )
    assert resp.status_code == 422


# ── POST analyze-surfaces ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_surfaces_returns_specs(
    client: AsyncClient, plan_with_operations: ManufacturingProcessPlan, analyzed_drawing: Drawing
):
    with (
        patch(
            "app.api.technology.extract_tp_features_from_drawing",
            return_value=[
                {
                    "process_plan_id": str(plan_with_operations.id),
                    "surface_type": "hole",
                    "nominal_mm": 30.0,
                    "roughness_ra": 1.6,
                    "machining_method": "boring",
                    "machining_stage": "finish",
                    "confidence": 0.9,
                }
            ],
        ),
        patch(
            "app.api.technology.save_surface_specs",
            return_value=[MagicMock(id=uuid.uuid4(), surface_type="hole", machining_method="boring",
                                    machining_stage="finish", nominal_mm=30.0, roughness_ra=1.6,
                                    fit_system=None, confidence=0.9)],
        ),
        patch(
            "app.api.technology.recommend_blank",
            return_value={"blank_type": "прокат", "utilization_factor": 0.75, "confidence": 0.8, "reasoning": "test"},
        ),
    ):
        resp = await client.post(
            f"/api/technology/process-plans/{plan_with_operations.id}/analyze-surfaces",
            json={"drawing_id": str(analyzed_drawing.id)},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "surfaces" in data
    assert "blank_recommendation" in data


# ── POST normcontrol ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_normcontrol_returns_checks(
    client: AsyncClient, plan_with_operations: ManufacturingProcessPlan
):
    """Plan with missing fields should return normcontrol errors."""
    resp = await client.post(
        f"/api/technology/process-plans/{plan_with_operations.id}/normcontrol",
        json={},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "checks" in data
    assert "errors_count" in data
    assert isinstance(data["checks"], list)


@pytest.mark.asyncio
async def test_normcontrol_status_is_passed_or_failed(
    client: AsyncClient, plan_with_operations: ManufacturingProcessPlan
):
    resp = await client.post(
        f"/api/technology/process-plans/{plan_with_operations.id}/normcontrol",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("passed", "failed")


# ── POST normcontrol resolve ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_normcontrol_resolve_check(
    client: AsyncClient,
    db_session: AsyncSession,
    plan_with_operations: ManufacturingProcessPlan,
):
    check = NormControlCheck(
        process_plan_id=plan_with_operations.id,
        gost_code="ГОСТ 3.1118",
        check_code="ESTD_MK_001",
        severity="error",
        status="open",
        message="Материал не указан",
        recommendation="Укажите марку материала",
        auto_fixable=False,
        created_by="normcontrol_agent",
    )
    db_session.add(check)
    await db_session.commit()

    resp = await client.post(
        f"/api/technology/process-plans/{plan_with_operations.id}/normcontrol/{check.id}/resolve",
        json={"resolution": "waived"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "waived"


# ── GET surface-specs ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_surface_specs_list_empty(
    client: AsyncClient, process_plan: ManufacturingProcessPlan
):
    resp = await client.get(
        f"/api/technology/process-plans/{process_plan.id}/surface-specs"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_surface_specs_list_returns_items(
    client: AsyncClient,
    db_session: AsyncSession,
    process_plan: ManufacturingProcessPlan,
):
    spec = SurfaceMachiningSpec(
        process_plan_id=process_plan.id,
        surface_type="hole",
        nominal_mm=25.0,
        roughness_ra=1.6,
        machining_method="boring",
        machining_stage="finish",
        confidence=0.9,
    )
    db_session.add(spec)
    await db_session.commit()

    resp = await client.get(
        f"/api/technology/process-plans/{process_plan.id}/surface-specs"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["surface_type"] == "hole"


# ── POST approve gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_blocked_when_normcontrol_failed(
    client: AsyncClient,
    db_session: AsyncSession,
    process_plan: ManufacturingProcessPlan,
):
    process_plan.normcontrol_status = "failed"
    db_session.add(process_plan)
    await db_session.commit()

    resp = await client.post(
        f"/api/technology/process-plans/{process_plan.id}/approve",
        json={"approved_by": "engineer"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_approve_allowed_when_normcontrol_passed(
    client: AsyncClient,
    db_session: AsyncSession,
    process_plan: ManufacturingProcessPlan,
):
    process_plan.normcontrol_status = "passed"
    db_session.add(process_plan)
    await db_session.commit()

    resp = await client.post(
        f"/api/technology/process-plans/{process_plan.id}/approve",
        json={"approved_by": "engineer"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
