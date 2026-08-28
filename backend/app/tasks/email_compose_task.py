"""Celery task: run the agentic 'AI help' email turn off the request path.

The turn is a full headless agent turn (looks things up with tools) and can take
minutes on a local model — too long for a synchronous HTTP request / proxy
timeout. The composer POSTs, gets a task_id, and polls GET
/api/email/compose/assist/{task_id}.
"""

from __future__ import annotations

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="email.compose_assist", bind=True, queue="scheduler")
def compose_assist_task(self, payload: dict) -> dict:
    from app.tasks.async_runner import run_async

    async def _go() -> dict:
        import uuid as _uuid

        from app.db.session import _get_session_factory
        from app.domain.email_compose import ComposeContext, assist_compose

        def _uid(v):
            return _uuid.UUID(v) if v else None

        async with _get_session_factory()() as db:
            res = await assist_compose(
                db,
                draft_subject=payload.get("subject", ""),
                draft_body=payload.get("body", ""),
                instruction=payload.get("instruction", ""),
                context=ComposeContext(
                    thread_id=_uid(payload.get("thread_id")),
                    supplier_id=_uid(payload.get("supplier_id")),
                    invoice_id=_uid(payload.get("invoice_id")),
                    mailbox=payload.get("mailbox"),
                ),
                acting_user_sub=payload.get("acting_user_sub"),
            )
        return {
            "subject": res.subject,
            "body_html": res.body_html,
            "body_text": res.body_text,
            "diff": res.diff,
            "notes": res.notes,
            "tone": res.tone,
        }

    try:
        return run_async(_go())
    except Exception as exc:  # noqa: BLE001
        logger.error("compose_assist_task_failed", error=str(exc))
        return {"error": str(exc)}
