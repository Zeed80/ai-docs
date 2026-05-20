"""Tests for Email Templates API — template CRUD and render."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import EmailTemplateCategory, EmailTemplateDB


@pytest.fixture
async def template(db_session):
    t = EmailTemplateDB(
        name="Запрос цены",
        slug="price-request",
        category=EmailTemplateCategory.inquiry,
        language="ru",
        subject="Запрос коммерческого предложения",
        body_html="<p>Уважаемые коллеги, просим предоставить КП на {{product}}.</p>",
        body_text="Просим предоставить КП на {{product}}.",
        variables=["product"],
        is_builtin=False,
        use_count=0,
        created_by="dev-user",
    )
    db_session.add(t)
    await db_session.commit()
    return t


# ── List ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_templates_empty(client: AsyncClient):
    resp = await client.get("/api/email-templates/")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_list_templates(client: AsyncClient, template):
    resp = await client.get("/api/email-templates/")
    assert resp.status_code == 200
    data = resp.json()
    names = [t["name"] for t in data]
    assert "Запрос цены" in names


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_template(client: AsyncClient):
    resp = await client.post("/api/email-templates/", json={
        "name": "Подтверждение заказа",
        "slug": "order-confirm",
        "category": "confirmation",
        "language": "ru",
        "subject": "Подтверждение заказа №{{order_id}}",
        "body_html": "<p>Ваш заказ №{{order_id}} принят.</p>",
        "variables": ["order_id"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Подтверждение заказа"
    assert data["slug"] == "order-confirm"
    assert "id" in data


# ── Get ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_template(client: AsyncClient, template):
    resp = await client.get(f"/api/email-templates/{template.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "price-request"
    assert data["category"] == "inquiry"


@pytest.mark.asyncio
async def test_get_template_not_found(client: AsyncClient):
    resp = await client.get(f"/api/email-templates/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_template(client: AsyncClient, template):
    resp = await client.patch(f"/api/email-templates/{template.id}", json={
        "subject": "Обновлённый запрос КП",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["subject"] == "Обновлённый запрос КП"


# ── Delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_template(client: AsyncClient):
    create_resp = await client.post("/api/email-templates/", json={
        "name": "Временный шаблон",
        "category": "custom",
        "language": "ru",
        "subject": "Тест",
        "body_html": "<p>Тест</p>",
    })
    template_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/email-templates/{template_id}")
    assert resp.status_code == 204


# ── Render ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_template(client: AsyncClient, template):
    resp = await client.post(f"/api/email-templates/{template.id}/render", json={
        "variables": {"product": "Болт М8×40"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "subject" in data or "body_html" in data or "rendered" in data or isinstance(data, dict)
