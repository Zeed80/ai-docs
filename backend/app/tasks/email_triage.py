"""Email Triage pipeline — Scenario 1 degraded mode (no AiAgent).

Full chain: poll IMAP → ingest attachments → classify → extract → normalize → validate.
Runs on 'ingest' queue via Celery Beat or manual trigger.
"""

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

# Used only when mailbox_configs is EMPTY (fresh install, nothing configured yet)
# — never as an error fallback: pretending a DB failure is "three default
# mailboxes" turns an outage into a silently successful run.
_LEGACY_DEFAULT_MAILBOXES = ["procurement", "accounting", "general"]


class MailboxLookupError(RuntimeError):
    """The mailbox list could not be read — the run must fail loudly."""


def list_active_mailboxes(*, require_sweep: bool) -> list[str]:
    """Names of mailboxes to act on.

    ``require_sweep=False`` — every active mailbox. Used to POLL: the web client
    shows a mailbox's mail regardless of whether the agent may read it, so the
    poller must fill the DB for all of them.

    ``require_sweep=True`` — only mailboxes whose owner consented to AI reading
    (``sweep_enabled``). Shared/integration mailboxes have it on by definition;
    a personal @<domain> mailbox only after its owner turns it on in /settings.
    Used for anything the AGENT does with mailbox contents.

    On a completely empty table this returns the historical defaults (fresh
    install). A non-empty table with nothing matching returns ``[]`` — that is a
    deliberate state, not a reason to poll random names.
    """
    from sqlalchemy import select

    from app.db.models import MailboxConfig
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            q = select(MailboxConfig.name).where(
                MailboxConfig.is_active == True  # noqa: E712
            )
            if require_sweep:
                q = q.where(MailboxConfig.sweep_enabled == True)  # noqa: E712
            names = list(db.execute(q).scalars().all())
            configured = db.execute(select(MailboxConfig.id).limit(1)).first() is not None
    except Exception as e:
        logger.error("active_mailbox_names_load_failed", error=str(e), require_sweep=require_sweep)
        raise MailboxLookupError(str(e)) from e

    if names:
        return names
    return [] if configured else list(_LEGACY_DEFAULT_MAILBOXES)


def _sweepable_mailbox_names() -> list[str]:
    """Backwards-compatible alias — mailboxes the agent is allowed to read."""
    return list_active_mailboxes(require_sweep=True)


@celery_app.task(name="app.tasks.email_triage.dispatch_mailbox_polls", bind=True)
def dispatch_mailbox_polls(self) -> dict:
    """Beat entrypoint: fan out ``poll_imap_mailbox`` per active MailboxConfig row.

    Replaces the old hard-coded ``poll-email-procurement/accounting/general``
    beat entries — a mailbox added through the UI with any other name was never
    polled by anything, which is the main reason the client kept saying
    "IMAP not configured".
    """
    from app.tasks.ingest import poll_imap_mailbox

    try:
        names = list_active_mailboxes(require_sweep=False)
    except MailboxLookupError as exc:
        logger.error("dispatch_mailbox_polls_aborted", error=str(exc))
        return {"status": "error", "error": str(exc), "dispatched": []}

    for name in names:
        poll_imap_mailbox.apply_async(args=[name], queue="ingest")

    logger.info("dispatch_mailbox_polls", count=len(names), mailboxes=names)
    return {"status": "ok", "dispatched": names}


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
            mailboxes = list_active_mailboxes(require_sweep=False)
        except MailboxLookupError as exc:
            logger.error("triage_aborted_no_mailbox_list", error=str(exc))
            return {"status": "error", "error": f"mailbox list unavailable: {exc}",
                    "mailboxes": [], "emails": 0, "documents": 0}
        if not mailboxes:
            logger.info("triage_nothing_to_sweep")
            return {"status": "ok", "mailboxes": [], "emails": 0, "documents": 0,
                    "note": "нет активных почтовых ящиков"}

    total_emails = 0
    total_docs = 0
    results = []

    for mb in mailboxes:
        try:
            poll_result = poll_imap_mailbox(mb)
            emails_count = poll_result.get("fetched", 0)
            docs = poll_result.get("documents", [])
            # ``documents`` may be a count (from a failed/short-circuit poll) or a
            # list of ids — normalise so the extraction loop below is safe.
            doc_ids = docs if isinstance(docs, list) else []
            total_emails += emails_count
            total_docs += len(doc_ids) if isinstance(docs, list) else int(docs or 0)

            for doc_id in doc_ids:
                try:
                    extract_result = process_document(doc_id)
                    results.append({
                        "document_id": doc_id,
                        "mailbox": mb,
                        "status": "processed",
                        "extraction": extract_result,
                    })
                except Exception as e:
                    logger.error("triage_extract_failed", document_id=doc_id, error=str(e))
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


@celery_app.task(name="app.tasks.email_triage.rule_create_work_order", bind=True, max_retries=2)
def rule_create_work_order(self, email_message_id: str, prompt: str) -> dict:
    """An email rule's forward_to_agent / create_task action: turn the message
    into a WorkOrder (durable runtime). Kept out of the sync ingest transaction."""
    from app.tasks.async_runner import run_async

    async def _go() -> dict:
        from sqlalchemy import select

        from app.db.models import EmailMessage
        from app.db.session import _get_session_factory
        from app.domain.work_orders import create_work_order, create_work_plan

        async with _get_session_factory()() as db:
            msg = (
                await db.execute(select(EmailMessage).where(EmailMessage.id == email_message_id))
            ).scalar_one_or_none()
            if msg is None:
                return {"status": "error", "reason": "message_gone"}
            order = await create_work_order(
                db,
                owner_key=f"email_rule:{msg.mailbox}",
                objective=(msg.subject or "Письмо из правила")[:200],
                description=prompt[:2000],
                source="email_rule",
                metadata={"email_message_id": str(msg.id), "email_from": msg.from_address},
            )
            await create_work_plan(
                db,
                order,
                steps=[{
                    "step_key": "handle",
                    "title": "Обработать письмо по правилу фильтра",
                    "kind": "agent_turn",
                    "input": {"prompt": prompt},
                    "depends_on": [],
                }],
            )
            await db.commit()
            return {"status": "ok", "work_order_id": str(order.id)}

    return run_async(_go())


@celery_app.task(name="app.tasks.email_triage.apply_rule_to_backlog", bind=True)
def apply_rule_to_backlog(self, rule_id: str, limit: int = 500) -> dict:
    """Apply one EmailRule to existing inbound messages (rules/{id}/run)."""
    from sqlalchemy import select

    from app.db.models import EmailMessage, EmailRule
    from app.db.sync_session import sync_session
    from app.domain.email_rules import apply_rules

    applied = 0
    with sync_session() as db:
        rule = db.get(EmailRule, rule_id)
        if rule is None:
            return {"status": "error", "reason": "rule_gone"}
        q = select(EmailMessage).where(EmailMessage.is_inbound == True)  # noqa: E712
        if rule.mailbox:
            q = q.where(EmailMessage.mailbox == rule.mailbox)
        msgs = db.execute(q.order_by(EmailMessage.received_at.desc()).limit(limit)).scalars().all()
        for m in msgs:
            try:
                if apply_rules(db, m, m.mailbox):
                    applied += 1
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    return {"status": "ok", "applied": applied}


@celery_app.task(name="app.tasks.email_triage.prune_attachments", bind=True)
def prune_attachments(self) -> dict:
    """Delete stored bytes of email attachments older than the retention window
    (MailServerConfig.attachment_retention_days) that are NOT linked to a kept
    Document. The EmailAttachment row stays (filename/size for the thread view);
    only storage_path is cleared and the object removed."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db.models import EmailAttachment, MailServerConfig
    from app.db.sync_session import sync_session

    removed = 0
    with sync_session() as db:
        cfg = db.execute(select(MailServerConfig)).scalars().first()
        days = (cfg.attachment_retention_days if cfg else 180) or 180
        if days <= 0:
            return {"status": "disabled"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = db.execute(
            select(EmailAttachment).where(
                EmailAttachment.created_at < cutoff,
                EmailAttachment.storage_path.isnot(None),
                EmailAttachment.document_id.is_(None),
            ).limit(2000)
        ).scalars().all()
        for att in rows:
            try:
                from app.storage import delete_file

                delete_file(att.storage_path)
            except Exception:  # noqa: BLE001
                pass
            att.storage_path = None
            removed += 1
        db.commit()
    logger.info("email_attachments_pruned", removed=removed, retention_days=days)
    return {"status": "ok", "removed": removed}
