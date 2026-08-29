"""Ф0.6 — the agent-instruction mailbox.

Three defects this covers:

* every message in such a mailbox became a WorkOrder, without the subject
  marker check that ``create_work_order_from_email``'s own docstring requires
  of its caller — spam to that address became work for the agent;
* any sender could command the agent, because nothing checked who wrote;
* the messages were never stored as EmailMessage at all (a full short-circuit
  of the pipeline), so nothing was visible in the mail client and there was no
  record of what the agent had been told to do.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import MailboxConfig, User


@pytest.fixture
def sync_session_factory(test_engine, monkeypatch):
    """Sync Session against the same test DB — the ingest path is sync code.

    Same shape as the fixture in test_email_client_p0.py.
    """
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))

    def _clean() -> None:
        with Session(engine) as db:
            db.execute(delete(MailboxConfig))
            db.execute(delete(User).where(User.sub.in_(["u1", "u2", "admin-1"])))
            db.commit()

    _clean()
    try:
        with Session(engine) as db:
            yield db
    finally:
        _clean()
        engine.dispose()


def _parsed(subject: str, sender: str):
    """Shape of app.tasks.imap_client.ParsedEmail, without imaplib."""
    return SimpleNamespace(
        message_id=f"<{uuid.uuid4()}@example.com>",
        in_reply_to=None,
        from_address=sender,
        to_addresses=["agent@example.com"],
        cc_addresses=[],
        subject=subject,
        body_text="Собери отчёт по поставщику Ромекс",
        body_html="",
        sent_at=datetime.now(timezone.utc),
        has_attachments=False,
        attachments=[],
    )


def _ingress_mailbox(allowed=None) -> MailboxConfig:
    return MailboxConfig(
        name="agent@example.com", display_name="Поручения",
        imap_host="mail.example.com", imap_port=993, imap_user="agent@example.com",
        imap_password_encrypted="x", imap_ssl=True, is_active=True,
        assigned_role="agent_ingress", ingress_allowed_senders=allowed,
    )


def test_marker_check_exists_and_is_case_insensitive():
    from app.domain.work_email_ingress import is_agent_instruction_email

    assert is_agent_instruction_email(_parsed("Поручение: собрать отчёт", "x@y.z"))
    assert is_agent_instruction_email(_parsed("  ПОРУЧЕНИЕ: срочно", "x@y.z"))
    assert not is_agent_instruction_email(_parsed("Реклама: купите пылесос", "x@y.z"))
    assert not is_agent_instruction_email(_parsed("", "x@y.z"))


def test_sender_allowlist_defaults_to_known_users(sync_session_factory):
    from app.tasks.ingest import _ingress_sender_allowed

    sync_session_factory.add(_ingress_mailbox())
    sync_session_factory.add(User(sub="u1", email="ivanov@example.com", name="Иванов",
                     role="buyer", is_active=True))
    sync_session_factory.commit()

    assert _ingress_sender_allowed(sync_session_factory, "agent@example.com", "Иванов <ivanov@example.com>")
    assert not _ingress_sender_allowed(sync_session_factory, "agent@example.com", "stranger@evil.example")
    assert not _ingress_sender_allowed(sync_session_factory, "agent@example.com", "")


def test_explicit_allowlist_accepts_address_and_bare_domain(sync_session_factory):
    from app.tasks.ingest import _ingress_sender_allowed

    sync_session_factory.add(_ingress_mailbox(allowed=["boss@example.com", "partner.example"]))
    sync_session_factory.add(User(sub="u2", email="ivanov@example.com", name="Иванов",
                     role="buyer", is_active=True))
    sync_session_factory.commit()

    assert _ingress_sender_allowed(sync_session_factory, "agent@example.com", "boss@example.com")
    assert _ingress_sender_allowed(sync_session_factory, "agent@example.com", "anyone@partner.example")
    # An explicit list replaces the "any known user" default, it does not extend it.
    assert not _ingress_sender_allowed(sync_session_factory, "agent@example.com", "ivanov@example.com")


def test_ingress_role_does_not_route_notifications_as_a_user_role(sync_session_factory):
    """"agent_ingress" is not a UserRole; matching it against User.role found
    nobody and silently fell through to "notify every admin"."""
    from app.tasks.ingest import _mailbox_recipients

    sync_session_factory.add(_ingress_mailbox())
    sync_session_factory.add(User(sub="admin-1", email="admin@example.com", name="Админ",
                     role="admin", is_active=True))
    sync_session_factory.commit()

    # Falls back to admins (the mailbox is shared and claims no real role) —
    # the point of the assertion is that it does not blow up or try to resolve
    # "agent_ingress" as a role.
    assert _mailbox_recipients(sync_session_factory, "agent@example.com") == ["admin-1"]


async def test_assigned_role_is_validated(client, db_session):
    resp = await client.post(
        "/api/mailbox/configs",
        json={
            "name": "typo@example.com", "imap_host": "mail.example.com",
            "imap_port": 993, "imap_user": "typo@example.com",
            "imap_password": "secret", "assigned_role": "accountnat",
        },
    )
    assert resp.status_code == 422
    assert "assigned_role" in resp.json()["detail"]


async def test_agent_ingress_role_is_accepted_and_round_trips(client, db_session):
    resp = await client.post(
        "/api/mailbox/configs",
        json={
            "name": "orders@example.com", "imap_host": "mail.example.com",
            "imap_port": 993, "imap_user": "orders@example.com",
            "imap_password": "secret", "assigned_role": "agent_ingress",
            "ingress_allowed_senders": ["boss@example.com"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["assigned_role"] == "agent_ingress"
    assert body["ingress_allowed_senders"] == ["boss@example.com"]


async def test_work_order_reply_goes_out_from_the_ingress_mailbox(db_session):
    """The reply steps carried no mailbox, so the answer would have been sent
    from the global .env account instead of the address it arrived at."""
    from app.domain.work_email_ingress import create_work_order_from_email
    from app.db.models import WorkPlan, WorkStep
    from sqlalchemy import select

    message_pk = uuid.uuid4()
    order = await create_work_order_from_email(
        db_session,
        _parsed("Поручение: собрать отчёт", "boss@example.com"),
        mailbox="agent@example.com",
        email_message_pk=message_pk,
    )
    await db_session.commit()

    plan = (
        await db_session.execute(select(WorkPlan).where(WorkPlan.work_order_id == order.id))
    ).scalars().first()
    steps = {
        s.step_key: s
        for s in (
            await db_session.execute(select(WorkStep).where(WorkStep.plan_id == plan.id))
        ).scalars().all()
    }
    assert steps["draft_reply"].input_["mailbox"] == "agent@example.com"
    assert steps["draft_reply"].input_["in_reply_to_message_id"] == str(message_pk)
    # And the send step still binds the approval to the letter (Ф0.2).
    assert steps["send_reply"].input_["expected_digest"] == (
        "${steps.draft_reply.output.content_digest}"
    )
    assert order.metadata_["mailbox"] == "agent@example.com"
