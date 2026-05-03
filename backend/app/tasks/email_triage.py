"""Email Triage pipeline — Scenario 1 degraded mode (no AiAgent).

Full chain: poll IMAP → ingest attachments → classify → extract → normalize → validate.
Runs on 'ingest' queue via Celery Beat or manual trigger.
"""

import structlog

from app.tasks.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger()


@celery_app.task(name="app.tasks.email_triage.run_triage", bind=True, max_retries=1)
def run_triage(self, mailbox: str | None = None) -> dict:
    """Full email triage pipeline — degraded mode (without AiAgent).

    1. Poll IMAP for unseen emails
    2. Store attachments as Documents
    3. Trigger extraction pipeline for each
    """
    from app.tasks.ingest import poll_imap_mailbox
    from app.tasks.extraction import process_document

    mailboxes = [mailbox] if mailbox else ["procurement", "accounting", "general"]
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
