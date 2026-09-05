"""Tests for Technology API — resources, operation templates, process plans."""

import pytest
from httpx import AsyncClient

from app.db.models import ManufacturingProcessPlan, ManufacturingResource


@pytest.fixture
async def resource(db_session):
    r = ManufacturingResource(
        resource_type="machine",
        name="Токарный станок 16К20",
        code="TK-001",
        status="active",
    )
    db_session.add(r)
    await db_session.commit()
    return r


@pytest.fixture
async def process_plan(db_session):
    plan = ManufacturingProcessPlan(
        product_name="Вал ступенчатый",
        product_code="VST-001",
        version="1.0",
        status="draft",
        standard_system="ЕСТД",
        created_by="engineer",
    )
    db_session.add(plan)
    await db_session.commit()
    return plan


# ── Resources ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resources_empty(client: AsyncClient):
    resp = await client.get("/api/technology/resources")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data or isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_list_resources(client: AsyncClient, resource):
    resp = await client.get("/api/technology/resources")
    assert resp.status_code == 200
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    names = [r["name"] for r in items]
    assert "Токарный станок 16К20" in names


@pytest.mark.asyncio
async def test_create_resource(client: AsyncClient):
    resp = await client.post(
        "/api/technology/resources",
        json={
            "resource_type": "tool",
            "name": "Резец проходной Т15К6",
            "code": "RP-001",
            "status": "active",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Резец проходной Т15К6"
    assert data["resource_type"] == "tool"
    assert "id" in data


# ── Operation templates ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_operation_templates(client: AsyncClient):
    resp = await client.get("/api/technology/operation-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data or isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_create_operation_template(client: AsyncClient):
    resp = await client.post(
        "/api/technology/operation-templates",
        json={
            "name": "Токарная обработка наружная",
            "operation_type": "turning",
            "default_operation_code": "T010",
            "is_active": True,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Токарная обработка наружная"
    assert "id" in data


# ── Process plans ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_process_plans_empty(client: AsyncClient):
    resp = await client.get("/api/technology/process-plans")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data or isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_list_process_plans(client: AsyncClient, process_plan):
    resp = await client.get("/api/technology/process-plans")
    assert resp.status_code == 200
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    names = [p["product_name"] for p in items]
    assert "Вал ступенчатый" in names


@pytest.mark.asyncio
async def test_create_process_plan(client: AsyncClient):
    resp = await client.post(
        "/api/technology/process-plans",
        json={
            "product_name": "Шестерня коническая",
            "product_code": "ShK-002",
            "version": "1.0",
            "material": "Сталь 40Х",
            "blank_type": "Поковка",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["product_name"] == "Шестерня коническая"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_process_plan(client: AsyncClient, process_plan):
    resp = await client.get(f"/api/technology/process-plans/{process_plan.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["product_code"] == "VST-001"


@pytest.mark.asyncio
async def test_add_operation_to_plan(client: AsyncClient, process_plan):
    resp = await client.post(
        f"/api/technology/process-plans/{process_plan.id}/operations",
        json={
            "sequence_no": 10,
            "operation_type": "turning",
            "name": "Токарная",
            "machine_minutes": 30.0,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["operation_type"] == "turning"
    assert "id" in data


# ── Learning rules ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_learning_rules(client: AsyncClient):
    resp = await client.get("/api/technology/learning-rules")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data or isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_create_learning_rule(client: AsyncClient):
    resp = await client.post(
        "/api/technology/learning-rules",
        json={
            "entity_type": "manufacturing_resource",
            "field_name": "material",
            "match_old_value": "Сталь 40Х ГОСТ",
            "replacement_value": "Сталь 40Х",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data


@pytest.mark.asyncio
async def test_reject_learning_rule(client: AsyncClient):
    created = await client.post(
        "/api/technology/learning-rules",
        json={
            "entity_type": "agent",
            "field_name": "behavior",
            "replacement_value": "Не применять это правило.",
            "confidence": 0.7,
        },
    )
    assert created.status_code == 201

    rejected = await client.post(
        f"/api/technology/learning-rules/{created.json()['id']}/reject",
        json={"rejected_by": "tester", "comment": "bad rule"},
    )

    assert rejected.status_code == 200
    data = rejected.json()
    assert data["status"] == "rejected"
    assert data["metadata"]["rejected_by"] == "tester"


@pytest.mark.asyncio
async def test_reflect_learning_rule_creates_proposed_then_activates(client: AsyncClient):
    """Reflection loop: first occurrence is a proposed rule; a repeat of the
    same (entity_type, field_name) reinforces it and self-promotes to active
    at activate_after, mirroring recipes.py's draft->active lifecycle."""
    payload = {
        "entity_type": "agent_turn",
        "field_name": "workspace_not_published",
        "lesson": "Публикуй результат на Рабочий стол сразу с первого хода.",
        "trigger_keywords": ["счет"],
        "activate_after": 2,
    }

    first = await client.post("/api/technology/learning-rules/reflect", json=payload)
    assert first.status_code == 200
    data = first.json()
    assert data["rule_type"] == "behavior"
    assert data["status"] == "proposed"
    assert data["occurrences"] == 1
    rule_id = data["id"]

    second = await client.post(
        "/api/technology/learning-rules/reflect",
        json={
            **payload,
            "trigger_keywords": ["товар"],  # merged with the first call's keywords, not replaced
        },
    )
    assert second.status_code == 200
    data2 = second.json()
    # Same row reinforced, not a duplicate.
    assert data2["id"] == rule_id
    assert data2["occurrences"] == 2
    assert data2["status"] == "active"
    assert data2["activated_by"] == "reflection_loop"
    assert set(data2["metadata"]["trigger_keywords"]) == {"счет", "товар"}


@pytest.mark.asyncio
async def test_reflect_learning_rule_distinct_field_name_is_separate_row(client: AsyncClient):
    """Different AuditCode (field_name) never merges into another lesson's row."""
    a = await client.post(
        "/api/technology/learning-rules/reflect",
        json={
            "entity_type": "agent_turn",
            "field_name": "filter_missing",
            "lesson": "Передавай все фильтры из запроса.",
        },
    )
    b = await client.post(
        "/api/technology/learning-rules/reflect",
        json={
            "entity_type": "agent_turn",
            "field_name": "empty_answer",
            "lesson": "Не возвращай пустой ответ.",
        },
    )
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] != b.json()["id"]
    assert a.json()["occurrences"] == 1
    assert b.json()["occurrences"] == 1
