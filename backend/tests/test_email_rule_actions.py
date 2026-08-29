"""Ф3 — the rule actions that were declared but never worked.

* ``assign_role`` was literally ``msg.mailbox = msg.mailbox`` and still got
  logged as applied, so the rule screen reported work that never happened;
* ``forward_to`` appears in this module's own docstring and in no code at all;
* the daily auto-send cap promised "per recipient" in its comment, had no
  recipient filter, counted drafts that were merely created, and matched them
  by the text rendering of a JSON column;
* an automatic reply was stamped "approved" and dispatched directly — the one
  outbound path with no human in it was also the only one with no checks.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import (
    DraftAction,
    EmailAutoReply,
    EmailMessage,
    EmailRule,
    EmailThread,
    MailboxConfig,
    MailServerConfig,
    Party,
    PartyRole,
    User,
)


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))
    started = datetime.now(timezone.utc)
    try:
        with Session(engine) as db:
            yield db
    finally:
        with Session(engine) as db:
            # EmailAutoReply has no created_at (it carries sent_at instead).
            db.execute(delete(EmailAutoReply).where(EmailAutoReply.sent_at >= started))
            for model in (DraftAction, EmailMessage, EmailThread,
                          EmailRule, MailboxConfig, MailServerConfig, Party, User):
                db.execute(delete(model).where(model.created_at >= started))
            db.commit()
        engine.dispose()


def _message(db, *, sender="supplier@romex.example", subject="Счёт", headers=None):
    thread = EmailThread(subject=subject, mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread, mailbox="procurement", subject=subject, from_address=sender,
        to_addresses=["procurement@example.com"], body_text="текст",
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@romex.example>",
        headers_meta=headers,
    )
    db.add_all([thread, msg])
    db.flush()
    return thread, msg


def _rule(db, actions, *, auto_send=False, name="Правило"):
    rule = EmailRule(
        name=name, mailbox="procurement", owner_sub=None, is_active=True, priority=10,
        conditions={"match": "all",
                    "rules": [{"field": "subject", "op": "contains", "value": "счёт"}]},
        actions=actions, auto_send=auto_send,
    )
    db.add(rule)
    db.flush()
    return rule


# ── assign_role ─────────────────────────────────────────────────────────────


def test_assign_role_actually_assigns_somebody(sync_db):
    from app.domain.email_rules import apply_rules

    sync_db.add(User(sub="buyer-1", email="b1@example.com", name="Закупщик",
                     role="buyer", is_active=True))
    thread, msg = _message(sync_db)
    _rule(sync_db, [{"type": "assign_role", "role": "buyer"}])
    sync_db.commit()

    applied = apply_rules(sync_db, msg, "procurement")
    assert [a["type"] for a in applied] == ["assign_role"]
    assert applied[0]["assigned_to_sub"] == "buyer-1"
    sync_db.refresh(thread)
    assert thread.assigned_to_sub == "buyer-1"


def test_assign_role_is_not_reported_as_applied_when_nobody_holds_the_role(sync_db):
    """The old no-op always claimed success; an unassignable rule must say so."""
    from app.domain.email_rules import apply_rules

    thread, msg = _message(sync_db)
    _rule(sync_db, [{"type": "assign_role", "role": "technologist"}])
    sync_db.commit()

    assert apply_rules(sync_db, msg, "procurement") == []
    sync_db.refresh(thread)
    assert thread.assigned_to_sub is None


def test_assignment_spreads_over_the_role_instead_of_piling_on_one_person(sync_db):
    from app.domain.email_rules import apply_rules

    sync_db.add_all([
        User(sub="buyer-a", email="a@example.com", name="A", role="buyer", is_active=True),
        User(sub="buyer-b", email="b@example.com", name="B", role="buyer", is_active=True),
    ])
    _rule(sync_db, [{"type": "assign_role", "role": "buyer"}])
    sync_db.commit()

    assigned = []
    for i in range(4):
        _, msg = _message(sync_db, subject=f"Счёт {i}")
        sync_db.commit()
        apply_rules(sync_db, msg, "procurement")
        sync_db.commit()
        assigned.append(
            sync_db.get(EmailThread, msg.thread_id).assigned_to_sub
        )
    assert set(assigned) == {"buyer-a", "buyer-b"}
    assert assigned.count("buyer-a") == 2


# ── forward_to ──────────────────────────────────────────────────────────────


def test_forward_to_external_address_prepares_a_draft_but_never_sends(sync_db, monkeypatch):
    """Auto-forwarding outside the company is how corporate mail leaks."""
    from app.domain.email_rules import apply_rules

    sent = []
    import app.tasks.email_sender as sender

    monkeypatch.setattr(sender.send_email_draft, "delay", lambda *a, **k: sent.append(a))
    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=50))
    _, msg = _message(sync_db)
    _rule(sync_db, [{"type": "forward_to", "address": "outsider@evil.example"}],
          auto_send=True)
    sync_db.commit()

    applied = apply_rules(sync_db, msg, "procurement")
    assert [a["type"] for a in applied] == ["forward_to"]
    sync_db.commit()

    draft = sync_db.query(DraftAction).filter_by(action_type="email.send").one()
    assert draft.draft_data["to_addresses"] == ["outsider@evil.example"]
    assert draft.draft_data["status"] == "draft"       # held for a human
    assert draft.draft_data["forward_of_message_id"] == str(msg.id)
    assert sent == []
    assert sync_db.query(EmailAutoReply).count() == 0


def test_forward_to_internal_address_may_be_sent_automatically(sync_db, monkeypatch):
    from app.domain.email_rules import apply_rules

    sent = []
    import app.tasks.email_sender as sender

    monkeypatch.setattr(sender.send_email_draft, "delay", lambda *a, **k: sent.append(a))
    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=50))
    _, msg = _message(sync_db)
    _rule(sync_db, [{"type": "forward_to", "address": "buh@example.com"}], auto_send=True)
    sync_db.commit()

    apply_rules(sync_db, msg, "procurement")
    sync_db.commit()

    assert len(sent) == 1
    ledger = sync_db.query(EmailAutoReply).one()
    assert ledger.recipient == "buh@example.com"


# ── auto-send limits ────────────────────────────────────────────────────────


def test_per_recipient_limit_is_actually_per_recipient(sync_db):
    from app.domain.email_rules import _auto_send_allowed

    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=100))
    _, msg = _message(sync_db)
    sync_db.commit()

    for _ in range(3):
        sync_db.add(EmailAutoReply(recipient="supplier@romex.example",
                                   mailbox="procurement"))
    sync_db.commit()

    # This correspondent is capped…
    assert _auto_send_allowed(sync_db, msg, "supplier@romex.example") is False
    # …while everyone else is unaffected, which the old global counter got wrong.
    assert _auto_send_allowed(sync_db, msg, "other@romex.example") is True


def test_thread_loop_guard_stops_two_robots_talking(sync_db):
    from app.domain.email_rules import _auto_send_allowed, _thread_root

    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=100))
    _, msg = _message(sync_db)
    sync_db.commit()
    root = _thread_root(msg)

    for _ in range(2):
        sync_db.add(EmailAutoReply(recipient="someone@else.example", thread_root=root))
    sync_db.commit()

    assert _auto_send_allowed(sync_db, msg, "someone@else.example") is False


def test_automated_mail_is_never_auto_answered(sync_db):
    from app.domain.email_rules import _auto_send_allowed

    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=100))
    _, bulk = _message(sync_db, subject="Счёт рассылка",
                       headers={"precedence": "bulk"})
    _, robot = _message(sync_db, sender="no-reply@bank.example", subject="Счёт робот")
    sync_db.commit()

    assert _auto_send_allowed(sync_db, bulk, "x@y.example") is False
    assert _auto_send_allowed(sync_db, robot, "x@y.example") is False


def test_stale_ledger_rows_do_not_count(sync_db):
    from app.domain.email_rules import _auto_send_allowed

    sync_db.add(MailServerConfig(singleton_key="default", mail_domain="example.com",
                                 auto_send_enabled=True, auto_send_max_per_day=100))
    _, msg = _message(sync_db)
    sync_db.commit()
    old = datetime.now(timezone.utc) - timedelta(days=3)
    for _ in range(5):
        sync_db.add(EmailAutoReply(recipient="supplier@romex.example", sent_at=old))
    sync_db.commit()

    assert _auto_send_allowed(sync_db, msg, "supplier@romex.example") is True


def test_sensitive_content_blocks_an_automatic_reply(sync_db):
    from app.domain.email_rules import _rule_send_blocked

    draft = DraftAction(
        action_type="email.send", entity_type="email",
        draft_data={"to_addresses": ["x@example.com"],
                    "body_text": "Это конфиденциально, не пересылайте"},
    )
    assert _rule_send_blocked(sync_db, draft) is True
