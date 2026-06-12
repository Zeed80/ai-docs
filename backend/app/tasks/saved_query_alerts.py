"""Celery task — periodic check of SavedQuery alerts."""

from __future__ import annotations

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="search.check_saved_query_alerts")
def check_saved_query_alerts() -> dict:
    """Re-run saved queries with alert_cron set and notify if results changed."""
    return run_async(_check_alerts())


async def _check_alerts() -> dict:
    from sqlalchemy import select

    from app.core.chat_bus import chat_bus
    from app.db.models import SavedQuery
    from app.db.session import _get_session_factory

    fired = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(SavedQuery).where(SavedQuery.is_alert == True)  # noqa: E712
        )
        queries = result.scalars().all()

        for sq in queries:
            try:
                new_count = await _run_query_count(db, sq.nl_text)
                if sq.result_count is not None and new_count != sq.result_count:
                    await chat_bus.publish({
                        "type": "notification",
                        "level": "info",
                        "title": "Алерт поиска",
                        "message": (
                            f"Запрос «{sq.nl_text[:60]}» — "
                            f"результатов: {sq.result_count} → {new_count}"
                        ),
                        "entity_type": "saved_query",
                        "entity_id": str(sq.id),
                    })
                    fired += 1

                sq.result_count = new_count
            except Exception as exc:
                logger.warning(
                    "saved_query_alert_check_failed",
                    query_id=str(sq.id),
                    error=str(exc),
                )

        await db.commit()

    logger.info("saved_query_alerts_checked", fired=fired)
    return {"fired": fired}


async def _run_query_count(db, nl_text: str) -> int:
    """Run a simple text count of matching invoices/documents."""
    from sqlalchemy import func, select

    from app.db.models import Document, Invoice

    q = nl_text.lower()

    inv_result = await db.execute(
        select(func.count()).select_from(Invoice).where(
            Invoice.invoice_number.ilike(f"%{q}%")
        )
    )
    doc_result = await db.execute(
        select(func.count()).select_from(Document).where(
            Document.file_name.ilike(f"%{q}%")
        )
    )
    return (inv_result.scalar() or 0) + (doc_result.scalar() or 0)
