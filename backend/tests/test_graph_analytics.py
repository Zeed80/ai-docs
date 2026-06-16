"""Background graph analytics: god nodes / clusters / surprising connections."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import graph_analytics as ga
from app.ai.graph_analytics import (
    GraphAnalyticsSettings,
    run_graph_analytics_async,
    save_graph_analytics_settings,
)
from app.db.models import KnowledgeEdge, KnowledgeNode, MemoryFact


@pytest.fixture(autouse=True)
def _mem_redis_settings(monkeypatch):
    """In-memory stand-in for the Redis-backed settings store (no live Redis
    needed for these tests — same pattern as tests/test_task_routing.py)."""
    store: dict = {}
    monkeypatch.setattr(ga, "_redis_get_settings", lambda: dict(store) if store else None)

    def _set(value: dict) -> None:
        store.clear()
        store.update(value)

    monkeypatch.setattr(ga, "_redis_set_settings", _set)


@pytest.fixture(autouse=True)
async def _clean_graph_tables(db_session: AsyncSession):
    yield
    await db_session.execute(delete(MemoryFact).where(MemoryFact.kind.in_(
        ["graph_insight", "graph_analytics_state"]
    )))
    await db_session.execute(delete(KnowledgeEdge))
    await db_session.execute(delete(KnowledgeNode))
    await db_session.commit()


async def _seed_supplier_invoice_graph(db: AsyncSession) -> None:
    supplier = KnowledgeNode(
        node_type="supplier", entity_type="supplier", title="ООО Ромашка",
        confidence=1.0, created_by="system",
    )
    db.add(supplier)
    await db.flush()

    invoices = []
    for i in range(3):
        inv = KnowledgeNode(
            node_type="invoice", entity_type="invoice", title=f"Счёт №{i}",
            confidence=1.0, created_by="system",
        )
        db.add(inv)
        invoices.append(inv)
    await db.flush()

    for inv in invoices:
        db.add(KnowledgeEdge(
            source_node_id=supplier.id, target_node_id=inv.id,
            edge_type="has_invoice", confidence=1.0, created_by="system",
        ))

    # One cross-domain edge (rare) to exercise the "surprising connection" path.
    machine = KnowledgeNode(
        node_type="machine", entity_type="machine", title="Токарный станок",
        confidence=1.0, created_by="system",
    )
    db.add(machine)
    await db.flush()
    db.add(KnowledgeEdge(
        source_node_id=invoices[0].id, target_node_id=machine.id,
        edge_type="uses_machine", confidence=1.0, created_by="system",
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_run_graph_analytics_computes_god_nodes_and_stores_facts(db_session: AsyncSession):
    await _seed_supplier_invoice_graph(db_session)

    result = await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()

    assert result["skipped"] is False
    assert result["nodes"] == 5
    assert result["edges"] == 4
    # supplier (degree 3) and invoice #0 (degree 2: supplier + machine) qualify.
    assert result["god_nodes"] == 2

    facts = (await db_session.execute(
        select(MemoryFact).where(MemoryFact.kind == "graph_insight")
    )).scalars().all()
    titles = [f.title for f in facts]
    assert "Самые связанные узлы графа памяти" in titles
    assert any("ромашка" in f.summary.lower() for f in facts if f.title == "Самые связанные узлы графа памяти")


@pytest.mark.asyncio
async def test_run_graph_analytics_skips_when_graph_unchanged(db_session: AsyncSession):
    await _seed_supplier_invoice_graph(db_session)

    first = await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()
    assert first["skipped"] is False

    # Zero out the throttle interval so the dirty-flag check (not the
    # interval check) is what's actually being exercised here.
    save_graph_analytics_settings(GraphAnalyticsSettings(interval_seconds=0))

    second = await run_graph_analytics_async(db_session, force=False)
    await db_session.commit()
    assert second == {"skipped": True, "reason": "graph_unchanged"}


@pytest.mark.asyncio
async def test_run_graph_analytics_respects_interval_setting(db_session: AsyncSession):
    await _seed_supplier_invoice_graph(db_session)
    save_graph_analytics_settings(GraphAnalyticsSettings(interval_seconds=3600))

    first = await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()
    assert first["skipped"] is False

    second = await run_graph_analytics_async(db_session, force=False)
    await db_session.commit()
    assert second == {"skipped": True, "reason": "interval_not_elapsed"}


@pytest.mark.asyncio
async def test_run_graph_analytics_disabled_setting_skips(db_session: AsyncSession):
    await _seed_supplier_invoice_graph(db_session)
    save_graph_analytics_settings(GraphAnalyticsSettings(enabled=False))

    result = await run_graph_analytics_async(db_session, force=False)
    assert result == {"skipped": True, "reason": "disabled"}

    # force=True must bypass the disabled flag (manual rebuild button).
    forced = await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()
    assert forced["skipped"] is False


@pytest.mark.asyncio
async def test_run_graph_analytics_skips_on_empty_graph(db_session: AsyncSession):
    result = await run_graph_analytics_async(db_session, force=False)
    assert result == {"skipped": True, "reason": "graph_empty"}


@pytest.mark.asyncio
async def test_run_graph_analytics_replaces_stale_insights_on_rerun(db_session: AsyncSession):
    await _seed_supplier_invoice_graph(db_session)
    await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()

    # Add another supplier+invoice to change the graph, then force a rerun —
    # old graph_insight rows must be replaced, not accumulated.
    supplier2 = KnowledgeNode(
        node_type="supplier", entity_type="supplier", title="ЗАО Лютик",
        confidence=1.0, created_by="system",
    )
    db_session.add(supplier2)
    await db_session.flush()
    inv2 = KnowledgeNode(
        node_type="invoice", entity_type="invoice", title="Счёт №99",
        confidence=1.0, created_by="system",
    )
    db_session.add(inv2)
    await db_session.flush()
    db_session.add(KnowledgeEdge(
        source_node_id=supplier2.id, target_node_id=inv2.id,
        edge_type="has_invoice", confidence=1.0, created_by="system",
    ))
    await db_session.commit()

    await run_graph_analytics_async(db_session, force=True)
    await db_session.commit()

    facts = (await db_session.execute(
        select(MemoryFact).where(MemoryFact.kind == "graph_insight")
    )).scalars().all()
    god_node_facts = [f for f in facts if f.title == "Самые связанные узлы графа памяти"]
    assert len(god_node_facts) == 1  # not duplicated across runs
