"""Personal mailboxes are private — API-level scoping.

A personal mailbox (mailbox_configs.mailbox_type="personal") belongs to one
employee. Everyone else — colleagues, admins, and the agent acting on their
behalf — must not see its threads/messages, while shared mailboxes
(procurement/accounting/general) stay company-wide as before.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.db.models import EmailMessage, EmailThread, MailboxConfig

OTHER_SUB = "colleague-sub"


def _mailbox(name: str, *, owner_sub: str | None, mailbox_type: str) -> MailboxConfig:
    return MailboxConfig(
        name=name,
        display_name=name,
        owner_sub=owner_sub,
        mailbox_type=mailbox_type,
        imap_host="mail.example.com",
        imap_port=993,
        imap_user=name,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
    )


def _thread_with_message(mailbox: str, subject: str) -> tuple[EmailThread, EmailMessage]:
    thread = EmailThread(subject=subject, mailbox=mailbox, message_count=1)
    message = EmailMessage(
        thread=thread,
        mailbox=mailbox,
        subject=subject,
        from_address="someone@example.com",
        to_addresses=["x@example.com"],
        body_text="тело письма",
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    return thread, message


@pytest.fixture
async def mailboxes(db_session):
    """A shared mailbox, the caller's own personal one, and a colleague's."""
    db_session.add_all([
        _mailbox("procurement", owner_sub=None, mailbox_type="shared"),
        _mailbox("me@example.com", owner_sub="dev-user", mailbox_type="personal"),
        _mailbox("colleague@example.com", owner_sub=OTHER_SUB, mailbox_type="personal"),
    ])
    objects = []
    for box, subject in (
        ("procurement", "Общий счёт"),
        ("me@example.com", "Моё личное письмо"),
        ("colleague@example.com", "Личное письмо коллеги"),
    ):
        thread, message = _thread_with_message(box, subject)
        objects += [thread, message]
    db_session.add_all(objects)
    await db_session.commit()
    return {obj.mailbox: obj for obj in objects if isinstance(obj, EmailThread)}


async def test_thread_list_hides_other_users_personal_mailbox(
    client: AsyncClient, mailboxes
):
    resp = await client.get("/api/email/threads?limit=100")
    assert resp.status_code == 200
    boxes = {t["mailbox"] for t in resp.json()["items"]}
    assert "procurement" in boxes          # shared — unchanged
    assert "me@example.com" in boxes       # own personal mailbox
    assert "colleague@example.com" not in boxes


async def test_explicit_mailbox_filter_cannot_reach_a_colleague(
    client: AsyncClient, mailboxes
):
    resp = await client.get("/api/email/threads?mailbox=colleague@example.com")
    assert resp.status_code == 403


async def test_thread_and_message_detail_are_scoped(client: AsyncClient, mailboxes, db_session):
    foreign = mailboxes["colleague@example.com"]
    assert (await client.get(f"/api/email/threads/{foreign.id}")).status_code == 404

    msg = (
        await db_session.execute(
            EmailMessage.__table__.select().where(
                EmailMessage.mailbox == "colleague@example.com"
            )
        )
    ).first()
    assert (await client.get(f"/api/email/{msg.id}")).status_code == 404


async def test_search_excludes_other_users_personal_mail(client: AsyncClient, mailboxes):
    resp = await client.post("/api/email/search", json={"query": "письмо", "limit": 50})
    assert resp.status_code == 200
    boxes = {m["mailbox"] for m in resp.json()["results"]}
    assert "colleague@example.com" not in boxes
    assert "me@example.com" in boxes


async def test_headless_agent_sees_no_personal_mailbox(db_session, mailboxes):
    """No acting user (AgentCron/Telegram) → service account → nothing personal."""
    from app.auth.models import UserInfo, UserRole
    from app.domain.email_access import hidden_mailbox_names

    agent = UserInfo(
        sub="agent-service", email="agent@internal", name="agent",
        preferred_username="agent", roles=[UserRole.admin],
    )
    hidden = await hidden_mailbox_names(db_session, agent)
    assert "colleague@example.com" in hidden
    assert "me@example.com" in hidden
    assert "procurement" not in hidden
