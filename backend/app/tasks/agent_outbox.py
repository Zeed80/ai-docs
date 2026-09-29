"""Opt-in Celery entry point for E14 transactional outbox delivery.

No external channel adapter is registered here.  Registering a concrete adapter
requires its own reviewed card proving recipient-side idempotency or its
unknown-outcome policy; until then this task is intentionally a no-op.
"""

from __future__ import annotations

from app.db.session import _get_session_factory
from app.domain.agent_outbox import OutboxDeliveryAdapter, deliver_due_outbox
from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

DELIVERY_ADAPTERS: dict[str, OutboxDeliveryAdapter] = {}


async def _deliver_outbox() -> int:
    results = await deliver_due_outbox(_get_session_factory(), adapters=DELIVERY_ADAPTERS)
    return len(results)


@celery_app.task(
    name="agent_outbox.deliver_due", queue="scheduler", max_retries=0, ignore_result=True
)
def deliver_due_outbox_task() -> None:
    """Dispatch an explicitly registered safe adapter batch, if one exists."""
    run_async(_deliver_outbox())
