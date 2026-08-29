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

# Loop guards for rule-generated mail. The org-wide cap lives in
# MailServerConfig.auto_send_max_per_day; these two are the shapes that cap
# cannot see — one correspondent hammered, or two robots answering each other
# inside a single conversation.
_MAX_AUTO_REPLIES_PER_RECIPIENT_PER_DAY = 3
_MAX_AUTO_REPLIES_PER_THREAD_PER_DAY = 2


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

def apply_rules(db, msg, mailbox: str, *, only_rule_id=None) -> list[dict]:
    """Run every active rule for ``mailbox`` against ``msg``. Mutates msg/thread,
    enqueues side-effect tasks, writes EmailRuleLog. Returns applied actions.

    ``only_rule_id`` restricts the run to one rule — used by "apply this rule to
    existing mail" (rules/{id}/run), which previously called this function
    unrestricted and therefore re-ran *every* rule over the backlog: the user
    asked to try one filter and got all of them applied to 500 messages.
    """
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

    # Ф0.5 — which rules may act on THIS mailbox.
    #
    #   * a rule naming this mailbox: yes;
    #   * a rule with mailbox=None ("all mailboxes"): only if it is a shared
    #     rule (owner_sub IS NULL), which only an admin can create.
    #
    # Selecting purely by mailbox — the previous behaviour — meant a rule any
    # employee created with no mailbox ran against every mailbox in the
    # company, including colleagues' personal ones: it could label their mail,
    # mark it read, draft auto-replies from their address and hand it to the
    # agent. The mailbox's own owner is not consulted here on purpose: a
    # personal rule is scoped by the mailbox it names, and the API refuses to
    # create one naming a mailbox its author cannot write to.
    from app.db.models import MailboxConfig as _MailboxConfig

    owner_sub = db.execute(
        select(_MailboxConfig.owner_sub).where(_MailboxConfig.name == mailbox)
    ).scalar_one_or_none()

    scope = (EmailRule.mailbox == mailbox) | (
        EmailRule.mailbox.is_(None) & EmailRule.owner_sub.is_(None)
    )
    if owner_sub:
        # The owner's own all-mailboxes rule still applies to their own box.
        scope = scope | (EmailRule.mailbox.is_(None) & (EmailRule.owner_sub == owner_sub))

    q = select(EmailRule).where(EmailRule.is_active == True, scope)  # noqa: E712
    if only_rule_id is not None:
        q = q.where(EmailRule.id == only_rule_id)
    rules = db.execute(q.order_by(EmailRule.priority.asc())).scalars().all()
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
                    # Ф3 — was literally `msg.mailbox = msg.mailbox`: a no-op
                    # that still got written to EmailRuleLog as "applied", so
                    # the rule screen reported work that never happened.
                    assigned = _assign_thread(db, thread, action["role"])
                    if assigned:
                        action = {**action, "assigned_to_sub": assigned}
                    else:
                        logger.info(
                            "email_rule_assign_no_user", rule_id=str(rule.id),
                            role=action.get("role"),
                        )
                        continue
                elif kind == "run_extraction":
                    for att in attachments:
                        if att.document_id:
                            from app.tasks.extraction import process_document

                            process_document.delay(str(att.document_id), force=True)
                elif kind == "auto_reply_template" and action.get("template_id"):
                    tpl = db.get(EmailTemplateDB, uuid.UUID(str(action["template_id"])))
                    if tpl is not None:
                        subj = tpl.subject or f"Re: {msg.subject or ''}"
                        if not subj.lower().startswith("re:"):
                            subj = f"Re: {subj}"
                        # auto_send: only if the rule opts in AND an admin enabled
                        # the org-wide policy AND the per-recipient daily rate
                        # limit is not exhausted AND the incoming mail is not
                        # itself auto-generated / already answered by a human.
                        do_send = bool(rule.auto_send) and _auto_send_allowed(db, msg)
                        draft = DraftAction(
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
                                "status": "approved" if do_send else "draft",
                                "risk_flags": [],
                                "created_by": f"rule:{rule.id}",
                                # Ownership for app.domain.email_access.
                                # may_access_draft: a rule-made draft belongs to
                                # whoever may send from the mailbox it landed in
                                # — the owner for a personal box, the team for a
                                # shared one. The rule author is deliberately
                                # NOT the owner: an admin's global rule must not
                                # hand them a draft sitting in someone's private
                                # mailbox.
                                "created_by_sub": None,
                                "sent_by": f"rule:{rule.id}" if do_send else None,
                            },
                        )
                        from app.domain.email_send import draft_content_digest

                        draft.draft_data["content_digest"] = draft_content_digest(
                            draft.draft_data
                        )
                        db.add(draft)
                        db.flush()
                        # Ф3: an automatic reply goes through the same risk
                        # detectors as any other outbound mail. It used to be
                        # stamped "approved" and dispatched directly, so the
                        # one path with no human in it was also the only path
                        # with no checks.
                        if do_send and _rule_send_blocked(db, draft):
                            logger.warning(
                                "email_rule_auto_send_blocked_by_risk",
                                rule_id=str(rule.id),
                            )
                            do_send = False
                            draft.draft_data = {**draft.draft_data, "status": "draft",
                                                "sent_by": None}
                        if do_send:
                            _dispatch_rule_send(
                                db, draft, rule, msg,
                                _bare_addr(msg.from_address), mailbox,
                            )
                        else:
                            _notify_rule_draft(msg, rule)
                elif kind == "forward_to" and action.get("address"):
                    # Ф3 — documented in this module's own docstring since the
                    # beginning and never implemented. A forward is an outbound
                    # message, so it goes through the normal draft path; an
                    # external recipient additionally requires a human, or a
                    # filter rule becomes a one-click corporate-mail exfil.
                    _forward_message(db, msg, rule, action["address"], mailbox)
                elif kind == "forward_to_agent" or kind == "create_task":
                    _enqueue_agent_task(msg, action.get("prompt"))
                elif kind == "stop":
                    stop = True
                applied.append(action)
            except Exception as exc:  # noqa: BLE001
                logger.warning("email_rule_action_failed", rule_id=str(rule.id),
                               action=kind, error=str(exc))

        if applied:
            from app.core.metrics import email_rule_actions_total

            for action in applied:
                email_rule_actions_total.labels(
                    action=str(action.get("type") or "unknown")
                ).inc()
            rule.run_count = (rule.run_count or 0) + 1
            rule.last_run_at = datetime.now(timezone.utc)
            db.add(EmailRuleLog(rule_id=rule.id, message_id=msg.id, actions_applied=applied))
            all_applied.extend(applied)
        if stop or rule.stop_processing:
            break

    return all_applied


def _rule_send_blocked(db, draft) -> bool:
    """True when an automatic send must be held for a human.

    Runs the same detectors the API exposes as email.risk_check, in the sync
    ingest context. Only ERROR-severity flags block: warnings ("внешний домен")
    are the normal case for supplier correspondence and would stop every
    auto-reply.
    """
    data = draft.draft_data or {}
    body = (data.get("body_text") or data.get("body_html") or "").lower()
    sensitive = ("конфиденциальн", "секрет", "не для распростран", "внутренн")
    if any(word in body for word in sensitive):
        return True

    # A recipient nobody in the system knows is not somewhere to send template
    # replies unattended.
    known = known_domains_sync(db)
    for addr in data.get("to_addresses") or []:
        domain = addr.split("@")[-1].lower() if "@" in addr else ""
        if domain and domain in known:
            return False
    return bool(data.get("to_addresses"))


def _assign_thread(db, thread, role: str) -> str | None:
    """Make one person responsible for this conversation.

    Picks the longest-idle active user holding ``role`` (round-robin by
    assignment count) so a busy queue does not all land on whoever was created
    first. Returns the sub, or None when nobody holds that role — in which case
    the action is NOT recorded as applied.
    """
    from sqlalchemy import func as _f, select as _sel

    from app.db.models import EmailThread, NotificationType, User

    if thread is None:
        return None
    candidates = db.execute(
        _sel(User.sub).where(User.role == role, User.is_active == True)  # noqa: E712
    ).scalars().all()
    if not candidates:
        return None
    load = dict(
        db.execute(
            _sel(EmailThread.assigned_to_sub, _f.count(EmailThread.id))
            .where(EmailThread.assigned_to_sub.in_(candidates))
            .group_by(EmailThread.assigned_to_sub)
        ).all()
    )
    chosen = min(candidates, key=lambda sub: (load.get(sub, 0), sub))
    thread.assigned_to_sub = chosen

    try:
        from app.services.notifications import create_notification_sync

        create_notification_sync(
            db,
            user_sub=chosen,
            type=NotificationType.handover,
            title="Вам назначена переписка",
            body=(thread.subject or "(без темы)")[:480],
            entity_type="email",
            entity_id=thread.id,
            action_url=f"/email/{thread.id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_rule_assign_notify_failed", error=str(exc))
    return chosen


def _forward_message(db, msg, rule, address: str, mailbox: str) -> None:
    """Forward an incoming message to ``address`` as a normal outbound draft.

    Auto-forwarding to an EXTERNAL domain is exactly how corporate mail leaks,
    so it is never sent by a rule on its own: the draft is prepared and a human
    is notified. Internal addresses follow the org's auto-send policy like any
    other rule-generated reply.
    """
    from app.db.models import DraftAction

    target = _bare_addr(address)
    if not target or "@" not in target:
        return
    known = known_domains_sync(db)
    domain = target.split("@")[-1].lower()
    external = domain not in known

    body_html = (
        f"<p>Переслано правилом «{rule.name}».</p>"
        f"<p>От: {msg.from_address}<br/>Тема: {msg.subject or '(без темы)'}</p>"
        f"<hr/>{msg.body_html or ''}"
    ) if msg.body_html else (
        f"<p>Переслано правилом «{rule.name}».</p>"
        f"<p>От: {msg.from_address}<br/>Тема: {msg.subject or '(без темы)'}</p>"
        f"<hr/><pre>{(msg.body_text or '')[:5000]}</pre>"
    )
    do_send = (not external) and bool(rule.auto_send) and _auto_send_allowed(db, msg, target)
    subject = msg.subject or "(без темы)"
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"

    draft = DraftAction(
        action_type="email.send",
        entity_type="email",
        draft_data={
            "to_addresses": [target],
            "cc_addresses": [],
            "subject": subject,
            "body_html": body_html,
            "body_text": msg.body_text,
            "thread_id": str(msg.thread_id) if msg.thread_id else None,
            "mailbox": mailbox,
            "forward_of_message_id": str(msg.id),
            "attachment_ids": [],
            "status": "approved" if do_send else "draft",
            "risk_flags": [],
            "created_by": f"rule:{rule.id}",
            "created_by_sub": None,
            "sent_by": f"rule:{rule.id}" if do_send else None,
        },
    )
    from app.domain.email_send import draft_content_digest

    draft.draft_data["content_digest"] = draft_content_digest(draft.draft_data)
    db.add(draft)
    db.flush()

    if do_send:
        _dispatch_rule_send(db, draft, rule, msg, target, mailbox)
    else:
        logger.info(
            "email_rule_forward_prepared", rule_id=str(rule.id), to=target,
            external=external,
        )
        _notify_rule_draft(msg, rule)


def _dispatch_rule_send(db, draft, rule, msg, recipient: str, mailbox: str) -> None:
    """Queue a rule-generated send and record it in the auto-reply ledger."""
    from app.db.models import EmailAutoReply

    try:
        from app.tasks.email_sender import send_email_draft

        draft.draft_data = {**draft.draft_data, "status": "queued"}
        db.add(EmailAutoReply(
            rule_id=rule.id,
            draft_id=draft.id,
            in_reply_to_message_id=msg.id,
            mailbox=mailbox,
            recipient=recipient,
            thread_root=_thread_root(msg),
        ))
        db.flush()
        send_email_draft.delay(str(draft.id))
        logger.info("email_rule_auto_sent", rule_id=str(rule.id), to=recipient)
    except Exception as exc:  # noqa: BLE001
        logger.error("email_rule_auto_send_failed", error=str(exc))
        _notify_rule_draft(msg, rule)


def _thread_root(msg) -> str | None:
    """First Message-ID of the conversation — the loop-detection key."""
    refs = (getattr(msg, "references", None) or "").split()
    if refs:
        return refs[0][:500]
    return (msg.in_reply_to or msg.message_id_header or None)


def _bare_addr(addr: str) -> str:
    m = re.search(r"<([^>]+)>", addr or "")
    return (m.group(1) if m else (addr or "")).strip()


def _auto_send_allowed(db, msg, recipient: str | None = None) -> bool:
    """Guardrails for a rule that sends without a human.

    Ф3 rewrote all three counters. Before:

    * "daily rate limit per recipient" (the comment) was implemented with no
      recipient filter at all — a single global counter for the company;
    * it counted DraftAction rows CREATED, so a send the relay refused still
      consumed the quota;
    * it matched them with ``cast(draft_data, String) LIKE '%"sent_by": "rule:%'``,
      which depends on how Postgres happens to render JSON.

    Now both limits are counted from ``email_auto_replies`` — rows that exist
    only because a message actually went out.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func as _f, select as _sel

    from app.db.models import EmailAutoReply, MailServerConfig
    from app.tasks.imap_client import is_automated_message

    from app.db.models import MailboxConfig

    cfg = db.execute(_sel(MailServerConfig)).scalars().first()
    # Ф9 — the mailbox gets the last word. NULL = inherit the global policy.
    box = db.execute(
        _sel(MailboxConfig.auto_send_enabled, MailboxConfig.auto_send_max_per_day)
        .where(MailboxConfig.name == msg.mailbox)
    ).first()
    box_enabled = box[0] if box else None
    enabled = box_enabled if box_enabled is not None else bool(
        cfg and cfg.auto_send_enabled
    )
    if not enabled:
        return False

    # Never auto-reply to auto-generated / bulk mail. The authoritative signals
    # are headers (Auto-Submitted, Precedence, List-Id); the previous check
    # searched the BODY for "noreply", which for an HTML-only letter meant
    # searching an empty string — no protection exactly where it mattered.
    if is_automated_message(getattr(msg, "headers_meta", None)):
        return False
    sender = _bare_addr(msg.from_address).lower()
    if any(marker in sender for marker in ("no-reply", "noreply", "donotreply", "mailer-daemon")):
        return False
    body = f"{msg.subject or ''}\n{(msg.body_text or '')[:2000]}".lower()
    if any(k in body for k in ("не отвечайте на это письмо", "автоматическое уведомление",
                               "this is an automated message")):
        return False

    # Not if a human already replied in this thread.
    if msg.thread_id:
        from app.db.models import EmailMessage as _EM

        human_reply = db.execute(
            _sel(_EM.id).where(
                _EM.thread_id == msg.thread_id, _EM.is_inbound == False,  # noqa: E712
            ).limit(1)
        ).first()
        if human_reply:
            return False

    to = (recipient or _bare_addr(msg.from_address)).lower()
    since = datetime.now(timezone.utc) - timedelta(days=1)

    # Per-conversation: two systems answering each other loop within one thread
    # long before any daily cap notices.
    root = _thread_root(msg)
    if root:
        in_thread = db.execute(
            _sel(_f.count(EmailAutoReply.id)).where(
                EmailAutoReply.thread_root == root,
                EmailAutoReply.sent_at >= since,
            )
        ).scalar() or 0
        if in_thread >= _MAX_AUTO_REPLIES_PER_THREAD_PER_DAY:
            logger.warning("email_auto_reply_thread_limit", thread_root=root)
            return False

    per_recipient = db.execute(
        _sel(_f.count(EmailAutoReply.id)).where(
            _f.lower(EmailAutoReply.recipient) == to,
            EmailAutoReply.sent_at >= since,
        )
    ).scalar() or 0
    if per_recipient >= _MAX_AUTO_REPLIES_PER_RECIPIENT_PER_DAY:
        logger.warning("email_auto_reply_recipient_limit", recipient=to)
        return False

    # A per-mailbox cap must be counted per mailbox: applying it to the global
    # tally would let a busy shared mailbox exhaust a personal one's quota.
    box_cap = box[1] if box else None
    total_q = _sel(_f.count(EmailAutoReply.id)).where(EmailAutoReply.sent_at >= since)
    if box_cap is not None:
        total_q = total_q.where(EmailAutoReply.mailbox == msg.mailbox)
    total_today = db.execute(total_q).scalar() or 0
    daily_cap = box_cap if box_cap is not None else (
        (cfg.auto_send_max_per_day if cfg else None) or 20
    )
    if total_today >= daily_cap:
        logger.warning("email_auto_reply_global_limit", sent=total_today,
                       cap=daily_cap, mailbox=msg.mailbox)
        return False
    return True


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
