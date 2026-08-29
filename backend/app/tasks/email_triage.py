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
        poll_imap_mailbox.apply_async(args=[name], queue="mail")

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
                # Only the rule the user asked to run — not the whole rule set.
                if apply_rules(db, m, m.mailbox, only_rule_id=rule.id):
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
        failed = 0
        for att in rows:
            try:
                from app.storage import delete_file

                delete_file(att.storage_path)
            except Exception as exc:  # noqa: BLE001
                # Ф8 — the old code swallowed this AND cleared storage_path, so
                # the object stayed in MinIO forever with nothing left pointing
                # at it: an orphan nobody could ever find again. Keep the path,
                # let the next run try again.
                logger.warning(
                    "email_attachment_delete_failed",
                    attachment_id=str(att.id), path=att.storage_path, error=str(exc),
                )
                failed += 1
                continue
            att.storage_path = None
            removed += 1
        db.commit()
    logger.info(
        "email_attachments_pruned", removed=removed, failed=failed, retention_days=days,
    )
    return {"status": "ok", "removed": removed, "failed": failed}


def prune_bodies_for(db) -> dict[str, int]:
    """The body-retention pass itself, so it is testable against a session."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_, select

    from app.db.models import EmailMessage, MailboxConfig

    pruned_by_mailbox: dict[str, int] = {}
    configs = db.execute(
        select(MailboxConfig.name, MailboxConfig.body_retention_days).where(
            MailboxConfig.body_retention_days > 0
        )
    ).all()
    for mailbox, days in configs:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        rows = db.execute(
            select(EmailMessage).where(
                EmailMessage.mailbox == mailbox,
                EmailMessage.received_at < cutoff,
                or_(
                    EmailMessage.body_text.isnot(None),
                    EmailMessage.body_html.isnot(None),
                ),
            ).limit(5000)
        ).scalars().all()
        for msg in rows:
            msg.body_text = None
            msg.body_html = None
            msg.body_html_sanitized = None
            # The snippet stays: a thread list of blank rows is unusable, and
            # 300 characters is not what a retention policy is about.
            msg.body_pruned_at = datetime.now(timezone.utc)
        if rows:
            pruned_by_mailbox[mailbox] = len(rows)
    return pruned_by_mailbox


@celery_app.task(name="app.tasks.email_triage.prune_message_bodies", bind=True)
def prune_message_bodies(self) -> dict:
    """Ф8 — drop the CONTENT of letters older than a mailbox's retention window.

    What survives: sender, subject, date, thread structure, links to documents
    and invoices. What goes: body_text, body_html and the sanitised copy — the
    part that is actually private and actually large.

    A mailbox with ``body_retention_days = 0`` (the default) is untouched: mail
    is not deleted because a cron job exists.
    """
    from app.db.sync_session import sync_session

    with sync_session() as db:
        pruned_by_mailbox = prune_bodies_for(db)
        db.commit()

    total = sum(pruned_by_mailbox.values())
    logger.info("email_bodies_pruned", total=total, by_mailbox=pruned_by_mailbox)
    return {"status": "ok", "pruned": total, "by_mailbox": pruned_by_mailbox}


@celery_app.task(name="app.tasks.email_triage.backfill_body_text", bind=True)
def backfill_body_text(self, limit: int = 2000) -> dict:
    """Ф1.1 — render body_text for already-stored HTML-only messages.

    Everything downstream reads ``body_text``: the Russian FTS index, filter
    rules matching on ``body``, snippets in the thread list, and what the agent
    is handed when asked to read a letter. Messages ingested before the parser
    learned to render HTML have that column empty and are effectively invisible
    to all of it, so they need a one-off pass. Idempotent and batched — safe to
    re-run until it reports ``updated: 0``.
    """
    from sqlalchemy import or_, select

    from app.db.models import EmailMessage
    from app.db.sync_session import sync_session
    from app.domain.email_html import html_to_text

    updated = 0
    with sync_session() as db:
        rows = db.execute(
            select(EmailMessage)
            .where(
                or_(EmailMessage.body_text.is_(None), EmailMessage.body_text == ""),
                EmailMessage.body_html.isnot(None),
                EmailMessage.body_html != "",
            )
            .limit(limit)
        ).scalars().all()
        for msg in rows:
            text = html_to_text(msg.body_html)
            if not text:
                continue
            msg.body_text = text
            msg.body_text_derived = True
            msg.snippet = " ".join(text.split())[:300]
            updated += 1
        db.commit()

    logger.info("email_body_text_backfilled", updated=updated, scanned=len(rows))
    return {"status": "ok", "updated": updated, "scanned": len(rows)}


@celery_app.task(name="email.triage_message", bind=True, max_retries=2, queue="mail")
def triage_message(self, message_id: str) -> dict:
    """Ф6.4 — work out what an incoming letter is, and act within policy.

    Deliberately separate from attachment recognition (Ф6.1), which already
    runs on its own: this is the layer above it. A counterparty asking for
    documents, a supplier's quote and a newsletter are indistinguishable to a
    pipeline that only knows how to OCR a PDF.

    The result is persisted so the thread view can explain itself — autonomy
    that cannot say what it did or why does not get used.
    """
    from app.tasks.async_runner import run_async

    async def _go() -> dict:
        from sqlalchemy import select

        from app.db.models import (
            EmailAttachment, EmailMessage, EmailTriageResult, MailboxConfig,
        )
        from app.db.session import _get_session_factory
        from app.domain.email_triage import classify_letter, label_for, plan_actions

        async with _get_session_factory()() as db:
            msg = (
                await db.execute(select(EmailMessage).where(EmailMessage.id == message_id))
            ).scalar_one_or_none()
            if msg is None:
                return {"status": "error", "reason": "message_gone"}

            mode = (
                await db.execute(
                    select(MailboxConfig.agent_triage_mode).where(
                        MailboxConfig.name == msg.mailbox
                    )
                )
            ).scalar_one_or_none() or "classify"
            if mode == "off":
                return {"status": "skipped", "reason": "triage_off"}

            existing = (
                await db.execute(
                    select(EmailTriageResult).where(
                        EmailTriageResult.message_id == msg.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"status": "skipped", "reason": "already_triaged"}

            attachments = (
                await db.execute(
                    select(EmailAttachment.filename).where(
                        EmailAttachment.message_id == msg.id,
                        EmailAttachment.is_inline == False,  # noqa: E712
                    )
                )
            ).scalars().all()

            try:
                outcome = await classify_letter(
                    sender=msg.from_address,
                    subject=msg.subject or "",
                    body=msg.body_text or "",
                    attachments=list(attachments),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("email_triage_failed", message_id=message_id, error=str(exc))
                db.add(EmailTriageResult(
                    message_id=msg.id, mailbox=msg.mailbox, category="other",
                    status="error", error=str(exc)[:500],
                ))
                await db.commit()
                # Retry: a model hiccup should not permanently leave a letter
                # unclassified, but the failure is recorded either way.
                raise self.retry(exc=exc, countdown=120)

            perform, propose = plan_actions(
                outcome, has_attachments=bool(attachments), mode=mode,
            )
            done = await _apply_triage_actions(db, msg, outcome, perform)

            db.add(EmailTriageResult(
                message_id=msg.id,
                mailbox=msg.mailbox,
                category=outcome.category,
                confidence=outcome.confidence,
                summary=outcome.summary,
                entities=outcome.entities,
                proposed=propose,
                performed=done,
                model_name=outcome.model_name,
                status="done",
            ))
            await db.commit()

            from app.core.metrics import email_triage_total

            email_triage_total.labels(category=outcome.category).inc()
            logger.info(
                "email_triaged", message_id=message_id, category=outcome.category,
                confidence=outcome.confidence, performed=len(done), proposed=len(propose),
            )
            return {
                "status": "ok",
                "category": outcome.category,
                "label": label_for(outcome.category),
                "confidence": outcome.confidence,
                "performed": done,
                "proposed": propose,
            }

    return run_async(_go())


async def _apply_triage_actions(db, msg, outcome, perform: list[dict]) -> list[dict]:
    """Carry out the side-effect-free-ish half of the plan.

    Returns only what actually happened: a panel that lists intentions as
    achievements is worse than no panel.
    """
    from sqlalchemy import select

    from app.db.models import EmailLabel, EmailThread, EmailThreadLabel
    from app.domain.email_triage import label_for

    done: list[dict] = []
    for action in perform:
        kind = action.get("type")
        try:
            if kind == "label":
                name = label_for(action["category"])
                label = (
                    await db.execute(
                        select(EmailLabel).where(
                            EmailLabel.name == name, EmailLabel.owner_sub.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                if label is None:
                    label = EmailLabel(name=name, is_system=True, owner_sub=None)
                    db.add(label)
                    await db.flush()
                if msg.thread_id:
                    exists = await db.get(EmailThreadLabel, (msg.thread_id, label.id))
                    if exists is None:
                        db.add(EmailThreadLabel(
                            thread_id=msg.thread_id, label_id=label.id, added_by="sveta",
                        ))
                done.append({**action, "label": name})

            elif kind == "notify_responsible":
                recipients = await _triage_recipients(db, msg)
                if not recipients:
                    # Nobody to tell — fall back to the plain new-mail ping so
                    # the letter is not swallowed by our own cleverness.
                    from app.tasks.ingest import _notify_new_email_async

                    await _notify_new_email_async(db, msg.mailbox, msg)
                    continue
                from app.db.models import NotificationType
                from app.services.notifications import create_notification

                for sub in recipients:
                    await create_notification(
                        db,
                        user_sub=sub,
                        type=NotificationType.email_received,
                        title=f"Света разобрала письмо · {label_for(outcome.category)}",
                        body=(outcome.summary or msg.subject or "")[:480],
                        entity_type="email",
                        entity_id=msg.thread_id or msg.id,
                        action_url=f"/email/{msg.thread_id}" if msg.thread_id else "/email",
                    )
                done.append({**action, "notified": len(recipients)})

            elif kind == "link_invoice":
                thread = await db.get(EmailThread, msg.thread_id) if msg.thread_id else None
                if thread is not None and outcome.entities.get("supplier_name"):
                    from app.db.models import Party

                    party = (
                        await db.execute(
                            select(Party).where(
                                Party.name.ilike(f"%{outcome.entities['supplier_name']}%")
                            ).limit(1)
                        )
                    ).scalar_one_or_none()
                    if party is not None:
                        thread.party_id = party.id
                        done.append({**action, "party_id": str(party.id)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("email_triage_action_failed", action=kind, error=str(exc))
    return done


async def _triage_recipients(db, msg) -> list[str]:
    """Who should hear about this letter — same rule as new-mail notification."""
    from sqlalchemy import select

    from app.db.models import EmailThread, MailboxConfig, User

    thread = await db.get(EmailThread, msg.thread_id) if msg.thread_id else None
    if thread is not None and getattr(thread, "assigned_to_sub", None):
        return [thread.assigned_to_sub]

    row = (
        await db.execute(
            select(MailboxConfig.assigned_role, MailboxConfig.mailbox_type,
                   MailboxConfig.owner_sub).where(MailboxConfig.name == msg.mailbox)
        )
    ).first()
    if row is None:
        return []
    role, mailbox_type, owner_sub = row
    if mailbox_type == "personal":
        return [owner_sub] if owner_sub else []
    if role and role != "agent_ingress":
        return list(
            (await db.execute(
                select(User.sub).where(User.role == role, User.is_active == True)  # noqa: E712
            )).scalars().all()
        )
    return []
