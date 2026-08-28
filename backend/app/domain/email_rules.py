"""Email filter-rules engine.

Evaluated from app.tasks.ingest.poll_imap_mailbox right after an inbound
EmailMessage row is created (sync Session), before attachment processing so a
``run_extraction`` / ``stop`` action can still take effect.

Conditions: {"match": "all"|"any", "rules": [{"field","op","value"}, ...]}
Fields:  from, to, cc, subject, body, has_attachment, attachment_name,
         attachment_type, mailbox, sender_domain, is_from_known_supplier
Ops:     contains, not_contains, equals, starts_with, ends_with, matches_regex,
         in_list, is_true
Actions: add_label, remove_label, move_to_folder, mark_read, star, forward_to,
         forward_to_agent, create_task, run_extraction, assign_role,
         auto_reply_template (DRAFT only — never auto-sends), stop
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()


# ── known domains (also fixes the hard-coded set in email.risk_check) ──────────

def known_domains_sync(db) -> set[str]:
    from sqlalchemy import select

    from app.db.models import MailboxConfig, MailServerConfig, Party

    domains: set[str] = set()
    row = db.execute(select(MailServerConfig)).scalars().first()
    if row and row.mail_domain:
        domains.add(row.mail_domain.lower())
    for addr in db.execute(select(MailboxConfig.smtp_from_address)).scalars():
        if addr and "@" in addr:
            domains.add(addr.split("@")[-1].lower())
    for email in db.execute(
        select(Party.contact_email).where(Party.role.in_(("supplier", "both")))
    ).scalars():
        if email and "@" in email:
            domains.add(email.split("@")[-1].lower())
    return {d for d in domains if d}


async def known_domains(db) -> set[str]:
    from sqlalchemy import select

    from app.db.models import MailboxConfig, MailServerConfig, Party

    domains: set[str] = set()
    row = (await db.execute(select(MailServerConfig))).scalars().first()
    if row and row.mail_domain:
        domains.add(row.mail_domain.lower())
    for addr in (await db.execute(select(MailboxConfig.smtp_from_address))).scalars():
        if addr and "@" in addr:
            domains.add(addr.split("@")[-1].lower())
    for email in (
        await db.execute(select(Party.contact_email).where(Party.role.in_(("supplier", "both"))))
    ).scalars():
        if email and "@" in email:
            domains.add(email.split("@")[-1].lower())
    return {d for d in domains if d}


# ── condition evaluation ─────────────────────────────────────────────────────

def _domain(addr: str) -> str:
    return addr.split("@")[-1].lower() if "@" in (addr or "") else ""


def _field_value(field: str, msg, attachments: list, known_supplier_domains: set[str]):
    if field == "from":
        return msg.from_address or ""
    if field == "to":
        return " ".join(msg.to_addresses or [])
    if field == "cc":
        return " ".join(msg.cc_addresses or [])
    if field == "subject":
        return msg.subject or ""
    if field == "body":
        return msg.body_text or ""
    if field == "mailbox":
        return msg.mailbox or ""
    if field == "sender_domain":
        return _domain(msg.from_address or "")
    if field == "has_attachment":
        return bool(attachments)
    if field == "attachment_name":
        return " ".join(a.filename for a in attachments)
    if field == "attachment_type":
        return " ".join((a.content_type or "") for a in attachments)
    if field == "is_from_known_supplier":
        return _domain(msg.from_address or "") in known_supplier_domains
    return ""


def _match_one(rule: dict, msg, attachments, known_supplier_domains) -> bool:
    field = rule.get("field", "")
    op = rule.get("op", "contains")
    expected = rule.get("value", "")
    actual = _field_value(field, msg, attachments, known_supplier_domains)

    if op == "is_true":
        return bool(actual)
    text = str(actual).lower()
    exp = str(expected).lower()
    if op == "contains":
        return exp in text
    if op == "not_contains":
        return exp not in text
    if op == "equals":
        return text == exp
    if op == "starts_with":
        return text.startswith(exp)
    if op == "ends_with":
        return text.endswith(exp)
    if op == "in_list":
        items = expected if isinstance(expected, list) else str(expected).split(",")
        return any(str(i).strip().lower() in text for i in items)
    if op == "matches_regex":
        try:
            return re.search(str(expected), str(actual), re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def evaluate_conditions(msg, attachments, conditions: dict, *, known_supplier_domains: set[str]) -> bool:
    rules = (conditions or {}).get("rules") or []
    if not rules:
        return False
    results = [_match_one(r, msg, attachments, known_supplier_domains) for r in rules]
    return all(results) if (conditions.get("match", "all") == "all") else any(results)


# ── action application (sync — Celery ingest path) ───────────────────────────

def apply_rules(db, msg, mailbox: str) -> list[dict]:
    """Run every active rule for ``mailbox`` against ``msg``. Mutates msg/thread,
    enqueues side-effect tasks, writes EmailRuleLog. Returns applied actions."""
    from sqlalchemy import select

    from app.db.models import (
        DraftAction,
        EmailAttachment,
        EmailRule,
        EmailRuleLog,
        EmailTemplateDB,
        EmailThread,
        EmailThreadLabel,
    )

    rules = db.execute(
        select(EmailRule)
        .where(
            EmailRule.is_active == True,  # noqa: E712
            (EmailRule.mailbox.is_(None)) | (EmailRule.mailbox == mailbox),
        )
        .order_by(EmailRule.priority.asc())
    ).scalars().all()
    if not rules:
        return []

    attachments = db.execute(
        select(EmailAttachment).where(EmailAttachment.message_id == msg.id)
    ).scalars().all()
    ksd = known_domains_sync(db)
    thread = db.get(EmailThread, msg.thread_id) if msg.thread_id else None

    all_applied: list[dict] = []
    for rule in rules:
        if not evaluate_conditions(msg, attachments, rule.conditions, known_supplier_domains=ksd):
            continue
        applied: list[dict] = []
        stop = False
        for action in rule.actions or []:
            kind = action.get("type")
            try:
                if kind == "mark_read":
                    msg.is_read = True
                    if thread:
                        thread.is_read = True
                        thread.unread_count = max((thread.unread_count or 1) - 1, 0)
                elif kind == "star":
                    msg.is_starred = True
                    if thread:
                        thread.is_starred = True
                elif kind == "move_to_folder" and action.get("folder"):
                    msg.folder = action["folder"]
                    if thread:
                        thread.folder = action["folder"]
                elif kind == "add_label" and action.get("label_id") and thread:
                    lid = uuid.UUID(str(action["label_id"]))
                    exists = db.get(EmailThreadLabel, (thread.id, lid))
                    if not exists:
                        db.add(EmailThreadLabel(thread_id=thread.id, label_id=lid,
                                                added_by=f"rule:{rule.id}"))
                elif kind == "remove_label" and action.get("label_id") and thread:
                    db.query(EmailThreadLabel).filter_by(
                        thread_id=thread.id, label_id=uuid.UUID(str(action["label_id"]))
                    ).delete()
                elif kind == "assign_role" and action.get("role"):
                    msg.mailbox = msg.mailbox  # routing handled elsewhere; recorded only
                elif kind == "run_extraction":
                    for att in attachments:
                        if att.document_id:
                            from app.tasks.extraction import process_document

                            process_document.delay(str(att.document_id), force=True)
                elif kind == "auto_reply_template" and action.get("template_id"):
                    # Deliberately DRAFT-only: an email rule never sends without a
                    # human. The draft lands in /email drafts + a notification.
                    tpl = db.get(EmailTemplateDB, uuid.UUID(str(action["template_id"])))
                    if tpl is not None:
                        subj = tpl.subject or f"Re: {msg.subject or ''}"
                        if not subj.lower().startswith("re:"):
                            subj = f"Re: {subj}"
                        db.add(DraftAction(
                            action_type="email.send",
                            entity_type="email",
                            draft_data={
                                "to_addresses": [_bare_addr(msg.from_address)],
                                "cc_addresses": [],
                                "subject": subj,
                                "body_html": tpl.body_html or "",
                                "body_text": tpl.body_text,
                                "thread_id": str(msg.thread_id) if msg.thread_id else None,
                                "mailbox": mailbox,
                                "in_reply_to_message_id": str(msg.id),
                                "attachment_ids": [],
                                "status": "draft",
                                "risk_flags": [],
                                "created_by": f"rule:{rule.id}",
                            },
                        ))
                        _notify_rule_draft(msg, rule)
                elif kind == "forward_to_agent" or kind == "create_task":
                    _enqueue_agent_task(msg, action.get("prompt"))
                elif kind == "stop":
                    stop = True
                applied.append(action)
            except Exception as exc:  # noqa: BLE001
                logger.warning("email_rule_action_failed", rule_id=str(rule.id),
                               action=kind, error=str(exc))

        if applied:
            rule.run_count = (rule.run_count or 0) + 1
            rule.last_run_at = datetime.now(timezone.utc)
            db.add(EmailRuleLog(rule_id=rule.id, message_id=msg.id, actions_applied=applied))
            all_applied.extend(applied)
        if stop or rule.stop_processing:
            break

    return all_applied


def _bare_addr(addr: str) -> str:
    m = re.search(r"<([^>]+)>", addr or "")
    return (m.group(1) if m else (addr or "")).strip()


def _notify_rule_draft(msg, rule) -> None:
    try:
        from app.db.models import MailboxConfig, NotificationType, User
        from app.db.sync_session import sync_session
        from app.services.notifications import create_notification_sync
        from sqlalchemy import select as _sel

        with sync_session() as ndb:
            row = ndb.execute(
                _sel(MailboxConfig.assigned_role, MailboxConfig.owner_sub, MailboxConfig.mailbox_type)
                .where(MailboxConfig.name == msg.mailbox)
            ).first()
            subs: list[str] = []
            if row and row[2] == "personal" and row[1]:
                subs = [row[1]]
            elif row and row[0]:
                subs = list(ndb.execute(
                    _sel(User.sub).where(User.role == row[0], User.is_active == True)  # noqa: E712
                ).scalars().all())
            if not subs:
                subs = list(ndb.execute(
                    _sel(User.sub).where(User.role == "admin", User.is_active == True)  # noqa: E712
                ).scalars().all())
            for sub in subs:
                create_notification_sync(
                    ndb, user_sub=sub, type=NotificationType.email_received,
                    title=f"Правило «{rule.name}»: подготовлен черновик ответа",
                    body=f"На письмо от {msg.from_address}: {(msg.subject or '')[:120]}",
                    entity_type="email", entity_id=msg.id, action_url="/email?panel=draft",
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_rule_draft_notify_failed", error=str(exc))


def _enqueue_agent_task(msg, prompt: str | None) -> None:
    text = prompt or (
        f"Обработай письмо от {msg.from_address} с темой «{msg.subject}»: "
        f"{(msg.body_text or '')[:500]}"
    )
    try:
        from app.tasks.email_triage import rule_create_work_order

        rule_create_work_order.delay(str(msg.id), text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_rule_agent_task_enqueue_failed", error=str(exc))
