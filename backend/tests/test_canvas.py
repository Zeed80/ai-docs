"""Tests for Canvas API — publish blocks to workspace."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_publish_markdown_block(client: AsyncClient):
    resp = await client.post("/api/canvas/publish", json={
        "canvas_id": "test:canvas-markdown",
        "block": {
            "type": "markdown",
            "title": "Тест markdown",
            "content": "# Заголовок\n\nТекст блока.",
        },
        "append": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["canvas_id"] == "test:canvas-markdown"


@pytest.mark.asyncio
async def test_publish_table_block(client: AsyncClient):
    resp = await client.post("/api/canvas/publish", json={
        "canvas_id": "test:canvas-table",
        "block": {
            "type": "table",
            "title": "Таблица счетов",
            "columns": [
                {"key": "id", "header": "ID", "type": "text"},
                {"key": "amount", "header": "Сумма", "type": "number"},
            ],
            "rows": [
                {"id": "INV-001", "amount": 5000},
                {"id": "INV-002", "amount": 12000},
            ],
        },
        "append": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"


@pytest.mark.asyncio
async def test_publish_chart_block(client: AsyncClient):
    resp = await client.post("/api/canvas/publish", json={
        "canvas_id": "test:canvas-chart",
        "block": {
            "type": "chart",
            "title": "Расходы по месяцам",
            "chart_type": "bar",
            "chart_data": {
                "labels": ["Янв", "Фев", "Мар"],
                "datasets": [{"label": "Счета", "data": [10000, 25000, 18000]}],
            },
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"


@pytest.mark.asyncio
async def test_publish_auto_canvas_id(client: AsyncClient):
    """Canvas ID should be auto-generated if not provided."""
    resp = await client.post("/api/canvas/publish", json={
        "block": {
            "type": "markdown",
            "content": "Автоматический ID",
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["canvas_id"] is not None


@pytest.mark.asyncio
async def test_publish_append_mode(client: AsyncClient):
    """Append mode should add to workspace without overwriting."""
    for i in range(2):
        resp = await client.post("/api/canvas/publish", json={
            "canvas_id": "test:canvas-append",
            "block": {
                "type": "markdown",
                "title": f"Блок {i}",
                "content": f"Содержимое {i}",
            },
            "append": True,
        })
        assert resp.status_code == 200
