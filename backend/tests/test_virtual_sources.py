"""Virtual spec-table sources: vector_search + graph_query."""

from __future__ import annotations

import pytest

from app.domain import table_spec as ts


@pytest.mark.asyncio
async def test_graph_query_neighborhood(db_session):
    from app.db.models import KnowledgeEdge, KnowledgeNode

    supplier = KnowledgeNode(node_type="supplier", title="ООО Ромашка")
    inv = KnowledgeNode(node_type="invoice", title="Счёт INV-001")
    item = KnowledgeNode(node_type="material", title="Фреза ⌀5")
    db_session.add_all([supplier, inv, item])
    await db_session.flush()
    db_session.add_all([
        KnowledgeEdge(source_node_id=inv.id, target_node_id=supplier.id,
                      edge_type="purchased_from", confidence=0.9),
        KnowledgeEdge(source_node_id=inv.id, target_node_id=item.id,
                      edge_type="mentions", confidence=0.8),
    ])
    await db_session.flush()

    # The invoice node is the hub — both edges touch it.
    spec = ts.TableSpec(
        source="graph_query",
        filters=[ts.FilterSpec(field="start_node", op="contains", value="INV-001")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 2  # both edges touch the invoice's neighbourhood
    edge_types = {r["edge_type"] for r in result.rows}
    assert edge_types == {"purchased_from", "mentions"}
    # Default columns projected.
    assert [c["key"] for c in result.columns] == [
        "source_title", "edge_type", "target_title", "target_type",
    ]
    # Virtual rows are read-only.
    assert all(c["editable"] is False for c in result.columns)


@pytest.mark.asyncio
async def test_graph_query_missing_node_returns_empty(db_session):
    spec = ts.TableSpec(
        source="graph_query",
        filters=[ts.FilterSpec(field="start_node", op="contains", value="нет-такого")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 0 and result.rows == []


@pytest.mark.asyncio
async def test_graph_query_requires_start_node(db_session):
    spec = ts.TableSpec(source="graph_query")
    with pytest.raises(ValueError, match="start_node"):
        await ts.execute_spec(db_session, spec)


@pytest.mark.asyncio
async def test_vector_search_requires_query(db_session):
    spec = ts.TableSpec(source="vector_search")
    with pytest.raises(ValueError, match="query"):
        await ts.execute_spec(db_session, spec)


@pytest.mark.asyncio
async def test_vector_search_maps_hits(db_session, monkeypatch):
    """vector_search projects Qdrant hits into table rows (engine mocked)."""
    async def fake_embed(text, task_type="passage"):
        return [0.1, 0.2, 0.3]

    def fake_search(vector, *, limit=20, doc_type=None, **kw):
        return [
            {"doc_id": "d1", "score": 0.91, "file_name": "act.pdf",
             "doc_type": "act", "status": "approved", "payload": {"text": "акт фрезы"}},
            {"doc_id": "d2", "score": 0.72, "file_name": "inv.pdf",
             "doc_type": "invoice", "status": "ingested", "payload": {}},
        ]

    monkeypatch.setattr("app.ai.embeddings.embed_text", fake_embed)
    monkeypatch.setattr("app.vector.qdrant_store.search_similar", fake_search)

    spec = ts.TableSpec(
        source="vector_search",
        columns=[ts.ColumnSpec(field="score"), ts.ColumnSpec(field="file_name"),
                 ts.ColumnSpec(field="doc_type")],
        filters=[ts.FilterSpec(field="query", op="contains", value="фрезы")],
        sort=[ts.SortSpec(field="score", dir="desc")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 2
    assert result.rows[0]["file_name"] == "act.pdf"  # higher score first
    assert result.rows[0]["score"] == 0.91


@pytest.mark.asyncio
async def test_catalog_lists_virtual_sources(client):
    resp = await client.get("/api/workspace/agent/spec-table/catalog")
    sources = resp.json()["sources"]
    assert "vector_search" in sources and "graph_query" in sources
    vkeys = [f["key"] for f in sources["vector_search"]["fields"]]
    assert "query" in vkeys and "score" in vkeys
