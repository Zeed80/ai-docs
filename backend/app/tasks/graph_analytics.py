"""Celery task — periodic background graph analytics (god nodes/clusters).

Wraps app.ai.graph_analytics.run_graph_analytics_async: dirty-flag-gated, so
most ticks are a cheap no-op once the graph stabilizes between cron runs.
"""

from __future__ import annotations

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="memory.run_graph_analytics")
def run_graph_analytics(force: bool = False) -> dict:
    return run_async(_run(force=force))


async def _run(*, force: bool) -> dict:
    from app.ai.graph_analytics import run_graph_analytics_async
    from app.db.session import _get_session_factory

    async with _get_session_factory()() as db:
        result = await run_graph_analytics_async(db, force=force)
        await db.commit()

    return result
