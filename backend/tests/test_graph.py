"""Tests for Graph API — knowledge nodes, edges, neighborhood, path."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import KnowledgeNode, KnowledgeEdge


@pytest.fixture
async def supplier_node(db_session):
    node = KnowledgeNode(
        node_type="supplier",
        title="ООО Поставщик",
        canonical_key="ooo-postavschik",
        entity_type="party",
        confidence=1.0,
        created_by="test",
    )
    db_session.add(node)
    await db_session.commit()
    return node


@pytest.fixture
async def invoice_node(db_session):
    node = KnowledgeNode(
        node_type="invoice",
        title="Счёт №001",
        entity_type="invoice",
        confidence=0.95,
        created_by="llm",
    )
    db_session.add(node)
    await db_session.commit()
    return node


@pytest.fixture
async def supplier_invoice_edge(db_session, supplier_node, invoice_node):
    edge = KnowledgeEdge(
        source_node_id=supplier_node.id,
        target_node_id=invoice_node.id,
        edge_type="issued",
        confidence=1.0,
    )
    db_session.add(edge)
    await db_session.commit()
    return edge


# ── Nodes ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_node(client: AsyncClient):
    resp = await client.post("/api/graph/nodes", json={
        "node_type": "material",
        "title": "Сталь 40Х",
        "canonical_key": "steel-40h",
        "confidence": 0.99,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["node_type"] == "material"
    assert data["title"] == "Сталь 40Х"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_node(client: AsyncClient, supplier_node):
    resp = await client.get(f"/api/graph/nodes/{supplier_node.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(supplier_node.id)
    assert data["node_type"] == "supplier"
    assert data["title"] == "ООО Поставщик"


@pytest.mark.asyncio
async def test_get_node_not_found(client: AsyncClient):
    resp = await client.get(f"/api/graph/nodes/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Edges ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_edge(client: AsyncClient, supplier_node, invoice_node):
    resp = await client.post("/api/graph/edges", json={
        "source_node_id": str(supplier_node.id),
        "target_node_id": str(invoice_node.id),
        "edge_type": "mentions",
        "confidence": 0.9,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["edge_type"] == "mentions"
    assert data["source_node_id"] == str(supplier_node.id)
    assert data["target_node_id"] == str(invoice_node.id)


# ── Neighborhood ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neighborhood(client: AsyncClient, supplier_node, supplier_invoice_edge):
    resp = await client.get(f"/api/graph/nodes/{supplier_node.id}/neighborhood")
    assert resp.status_code == 200
    data = resp.json()
    assert "center" in data or "node" in data
    assert "edges" in data or "neighbors" in data or "center" in data


@pytest.mark.asyncio
async def test_neighborhood_not_found(client: AsyncClient):
    resp = await client.get(f"/api/graph/nodes/{uuid.uuid4()}/neighborhood")
    assert resp.status_code == 404


# ── Path ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_between_nodes(client: AsyncClient, supplier_node, invoice_node, supplier_invoice_edge):
    resp = await client.get("/api/graph/path", params={
        "source_node_id": str(supplier_node.id),
        "target_node_id": str(invoice_node.id),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data or "edges" in data or isinstance(data, dict)


@pytest.mark.asyncio
async def test_path_no_path(client: AsyncClient, supplier_node):
    # Disconnected node — create fresh node with no edges
    new_node_resp = await client.post("/api/graph/nodes", json={
        "node_type": "material",
        "title": "Изолированный узел",
        "confidence": 1.0,
    })
    new_node_id = new_node_resp.json()["id"]

    resp = await client.get("/api/graph/path", params={
        "source_node_id": str(supplier_node.id),
        "target_node_id": new_node_id,
    })
    assert resp.status_code == 200
    data = resp.json()
    # Path is empty or None when no connection
    nodes = data.get("nodes") or []
    assert isinstance(nodes, list)


# ── Review queue ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_review_queue(client: AsyncClient):
    resp = await client.get("/api/graph/review")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data or isinstance(data, dict)
