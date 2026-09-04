"""Три адреса, которые карточка техпроцесса звала с самого начала.

Маршрутов не было: два GET отвечали 404/405, PATCH операции уходил в никуда.
Фронт проверял только `if (r.ok)` без ветки else, поэтому «Нормоконтроль» и
«Заготовка» всегда оставались пустыми, а правка операции молча терялась —
список перечитывался и показывал прежнее значение.
"""

import pytest
from httpx import AsyncClient

from app.db.models import (
    BlankSpec,
    ManufacturingOperation,
    ManufacturingProcessPlan,
    NormControlCheck,
)


@pytest.fixture
async def plan(db_session):
    p = ManufacturingProcessPlan(
        product_name="Вал ступенчатый",
        product_code="VST-100",
        version="1.0",
        status="draft",
        standard_system="ЕСТД",
        created_by="engineer",
    )
    db_session.add(p)
    await db_session.commit()
    return p


@pytest.fixture
async def operation(db_session, plan):
    op = ManufacturingOperation(
        process_plan_id=plan.id,
        sequence_no=10,
        operation_code="005",
        name="Токарная черновая",
        operation_type="turning",
    )
    db_session.add(op)
    await db_session.commit()
    return op


# ── Нормоконтроль: чтение без повторного запуска ──────────────────────────────


@pytest.mark.asyncio
async def test_normcontrol_result_reads_saved_findings(client: AsyncClient, db_session, plan):
    db_session.add_all([
        NormControlCheck(
            process_plan_id=plan.id, gost_code="ГОСТ 3.1118", check_code="ESTD_MK_001",
            severity="error", status="open", message="Не заполнено поле «Материал»",
        ),
        NormControlCheck(
            process_plan_id=plan.id, gost_code="ГОСТ 3.1404", check_code="ESTD_NC_002",
            severity="warning", status="open", message="Не указан режим резания",
        ),
        # Закрытое замечание не должно попадать в счётчики открытых.
        NormControlCheck(
            process_plan_id=plan.id, gost_code="ГОСТ 3.1102", check_code="ESTD_MK_009",
            severity="error", status="resolved", message="Исправлено",
        ),
    ])
    await db_session.commit()

    resp = await client.get(f"/api/technology/process-plans/{plan.id}/normcontrol-result")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 3
    assert data["errors_count"] == 1
    assert data["warnings_count"] == 1
    # Статус замечания нужен UI, чтобы отличить закрытое от открытого.
    assert {c["status"] for c in data["checks"]} == {"open", "resolved"}


@pytest.mark.asyncio
async def test_normcontrol_result_for_unchecked_plan_is_empty_not_an_error(
    client: AsyncClient, plan
):
    resp = await client.get(f"/api/technology/process-plans/{plan.id}/normcontrol-result")
    assert resp.status_code == 200
    assert resp.json()["total_count"] == 0


@pytest.mark.asyncio
async def test_normcontrol_result_missing_plan_is_404(client: AsyncClient):
    resp = await client.get(
        "/api/technology/process-plans/00000000-0000-0000-0000-000000000001/normcontrol-result"
    )
    assert resp.status_code == 404


# ── Заготовка: тот же адрес теперь читается, а не только пишется ──────────────


@pytest.mark.asyncio
async def test_blank_spec_can_be_read_back_after_writing(client: AsyncClient, plan):
    written = await client.post(
        f"/api/technology/process-plans/{plan.id}/blank-spec",
        json={"blank_type": "прокат", "material_grade": "Сталь 45", "mass_blank_kg": 12.5},
    )
    assert written.status_code == 200

    read = await client.get(f"/api/technology/process-plans/{plan.id}/blank-spec")
    assert read.status_code == 200
    data = read.json()
    assert data["blank_type"] == "прокат"
    assert data["material_grade"] == "Сталь 45"
    assert data["mass_blank_kg"] == 12.5


@pytest.mark.asyncio
async def test_blank_spec_absent_is_null_not_404(client: AsyncClient, plan):
    resp = await client.get(f"/api/technology/process-plans/{plan.id}/blank-spec")
    assert resp.status_code == 200
    assert resp.json() is None


# ── Правка операции ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operation_edit_actually_persists(client: AsyncClient, db_session, operation):
    resp = await client.patch(
        f"/api/technology/operations/{operation.id}",
        json={"name": "Токарная чистовая", "operation_code": "010"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Токарная чистовая"

    await db_session.refresh(operation)
    assert operation.name == "Токарная чистовая"
    assert operation.operation_code == "010"


@pytest.mark.asyncio
async def test_operation_edit_refuses_fields_outside_the_allowlist(
    client: AsyncClient, operation
):
    resp = await client.patch(
        f"/api/technology/operations/{operation.id}",
        json={"process_plan_id": "00000000-0000-0000-0000-000000000002"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_operation_edit_missing_operation_is_404(client: AsyncClient):
    resp = await client.patch(
        "/api/technology/operations/00000000-0000-0000-0000-000000000001",
        json={"name": "X"},
    )
    assert resp.status_code == 404
