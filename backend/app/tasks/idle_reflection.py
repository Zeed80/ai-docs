"""Celery task — the idle-reflection "subconscious" beat job (Ф6,
AGENT_AUTONOMY_ROADMAP.md).

Wraps app.domain.idle_reflection.run_idle_reflection: self-throttled on both
"is anyone using the system right now" and "did we already run recently
enough", so most ticks are a cheap no-op — same shape as
app.tasks.graph_analytics.
"""

from __future__ import annotations

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="agent.idle_reflection")
def run_idle_reflection_task(force: bool = False) -> dict:
    return run_async(_run(force=force))


async def _run(*, force: bool) -> dict:
    from app.db.session import _get_session_factory
    from app.domain.idle_reflection import run_idle_reflection

    async with _get_session_factory()() as db:
        result = await run_idle_reflection(db, force=force)
        await db.commit()

    return result
