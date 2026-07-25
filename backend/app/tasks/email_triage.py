"""Email Triage pipeline — Scenario 1 degraded mode (no AiAgent).

Full chain: poll IMAP → ingest attachments → classify → extract → normalize → validate.
Runs on 'ingest' queue via Celery Beat or manual trigger.
"""

import structlog

from app.tasks.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger()

# Used only when mailbox_configs is EMPTY (fresh install, nothing configured yet)
# — never as an error fallback: pretending a DB failure is "three default
# mailboxes" turns an outage into a silently successful run.
_LEGACY_DEFAULT_MAILBOXES = ["procurement", "accounting", "general"]


class MailboxLookupError(RuntimeError):
    """The mailbox list could not be read — the run must fail loudly."""


def _sweepable_mailbox_names() -> list[str]:
    """Mailboxes the agent is allowed to poll.

    ``sweep_enabled`` is the consent switch: shared/integration mailboxes have it
    on by definition, a personal @<domain> mailbox only after its owner turns it
    on in /settings. Provisioning a mailbox for someone must not silently
    subscribe the AI to their private correspondence.
    """
    from sqlalchemy import select

    from app.db.models import MailboxConfig
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            names = db.execute(
                select(MailboxConfig.name).where(
                    MailboxConfig.is_active == True,  # noqa: E712
                    MailboxConfig.sweep_enabled == True,  # noqa: E712
                )
            ).scalars().all()
            configured = db.execute(select(MailboxConfig.id).limit(1)).first() is not None
    except Exception as e:
        logger.error("sweepable_mailbox_names_load_failed", error=str(e))
        raise MailboxLookupError(str(e)) from e

    if names:
        return list(names)
    # Nothing configured at all → fresh install, keep the historical default.
    # Something IS configured but nothing is sweepable → that is a deliberate
    # state (all personal, none opted in), not a reason to poll random names.
    return [] if configured else list(_LEGACY_DEFAULT_MAILBOXES)


@celery_app.task(name="app.tasks.email_triage.run_triage", bind=True, max_retries=1)
def run_triage(self, mailbox: str | None = None) -> dict:
    """Full email triage pipeline — degraded mode (without AiAgent).

    1. Poll IMAP for unseen emails
    2. Store attachments as Documents
    3. Trigger extraction pipeline for each
    """
    from app.tasks.ingest import poll_imap_mailbox
    from app.tasks.extraction import process_document

    if mailbox:
        mailboxes = [mailbox]
    else:
        try:
            mailboxes = _sweepable_mailbox_names()
        except MailboxLookupError as exc:
            logger.error("triage_aborted_no_mailbox_list", error=str(exc))
            return {"status": "error", "error": f"mailbox list unavailable: {exc}",
                    "mailboxes": [], "emails": 0, "documents": 0}
        if not mailboxes:
            logger.info("triage_nothing_to_sweep")
            return {"status": "ok", "mailboxes": [], "emails": 0, "documents": 0,
                    "note": "нет ящиков с включённым сбором (sweep_enabled)"}

    total_emails = 0
    total_docs = 0
    results = []

    for mb in mailboxes:
        try:
            poll_result = poll_imap_mailbox(mb)
            emails_count = poll_result.get("emails_processed", 0)
            docs = poll_result.get("documents_created", [])
            total_emails += emails_count
            total_docs += len(docs)

            # Trigger extraction for each new document
            for doc_id in docs:
                try:
                    extract_result = process_document(doc_id)
                    results.append({
                        "document_id": doc_id,
                        "mailbox": mb,
                        "status": "processed",
                        "extraction": extract_result,
                    })
                except Exception as e:
                    logger.error(
                        "triage_extract_failed",
                        document_id=doc_id,
                        error=str(e),
                    )
                    results.append({
                        "document_id": doc_id,
                        "mailbox": mb,
                        "status": "extract_failed",
                        "error": str(e),
                    })

        except Exception as e:
            logger.error("triage_poll_failed", mailbox=mb, error=str(e))

    logger.info(
        "triage_complete",
        total_emails=total_emails,
        total_docs=total_docs,
        results_count=len(results),
    )

    return {
        "total_emails": total_emails,
        "total_documents": total_docs,
        "results": results,
    }
