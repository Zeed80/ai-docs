"""Celery task — reconcile business-entity graph nodes against live rows.

Safety net for the explicit graph-update hooks in app.domain.memory_builder
(build_supplier_invoice_memory_*, build_anomaly_memory_async,
build_approval_memory_async): catches anything that slipped through a direct
SQL delete or a future code path bypassing the hooks.
"""

from __future__ import annotations

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="memory.reconcile_graph")
def reconcile_graph_memory() -> dict:
    return run_async(_run())


async def _run() -> dict:
    from app.db.session import _get_session_factory
    from app.domain.memory_builder import reconcile_orphaned_business_nodes_async

    async with _get_session_factory()() as db:
        removed = await reconcile_orphaned_business_nodes_async(db)
        await db.commit()

    logger.info("graph_memory_reconciled", removed=removed)
    return {"removed": removed}


@celery_app.task(name="memory.rebuild_business_graph")
def rebuild_business_graph() -> dict:
    """Manual "Пересобрать граф" trigger (admin GUI).

    Walks every Invoice/AnomalyCard/Approval through the same idempotent
    builders the live hooks use (backfills documents approved before the
    graph-memory hooks existed), then force-recomputes graph_insight facts
    on the now-current graph.
    """
    return run_async(_rebuild())


async def _rebuild() -> dict:
    from app.ai.graph_analytics import run_graph_analytics_async
    from app.db.session import _get_session_factory
    from app.domain.memory_builder import backfill_business_graph_async

    async with _get_session_factory()() as db:
        counts = await backfill_business_graph_async(db)
        await db.commit()
        analytics = await run_graph_analytics_async(db, force=True)
        await db.commit()

    logger.info("graph_memory_rebuilt", **counts, analytics=analytics)
    return {"backfilled": counts, "analytics": analytics}
