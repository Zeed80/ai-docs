"""Ф0.7 + Ф0.8 — consent for AI reading, and what reaches a lock screen.

* An admin provisioning a mailbox is not the same as an admin deciding that an
  AI may read the correspondence in it. Switching the flag ON is the owner's
  call; an admin may only ask (and may always switch it OFF).
* Notification preferences lived in localStorage only, so the server pushed
  every category to every device regardless — and private mail put sender and
  subject on the phone's lock screen with no way to prevent it.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.db.models import MailboxConfig, Notification, NotificationType, User

ADMIN_SUB = "dev-user"
OWNER_SUB = "employee-sub"


@pytest.fixture(autouse=True)
def _stub_mail_server_config(monkeypatch):
    """The admin endpoint reads the Mailcow config through its own session
    factory (app.db.session._get_session_factory), whose engine is cached across
    event loops — in tests that surfaces as "Event loop is closed" during
    teardown rather than as anything about the endpoint. Stub the lookup; it is
    not what these tests are about."""
    from app.services import integration_config

    class _Cfg:
        webmail_url = None
        mail_domain = "example.com"

    async def _fake():
        return _Cfg()

    monkeypatch.setattr(integration_config, "get_mail_server_config", _fake)
    import app.api.admin as admin_module

    monkeypatch.setattr(admin_module, "get_mail_server_config", _fake, raising=False)


@pytest_asyncio.fixture
async def personal_mailbox(db_session):
    db_session.add(
        User(
            sub=OWNER_SUB,
            email="employee@example.com",
            name="Сотрудник",
            role="buyer",
            is_active=True,
        )
    )
    cfg = MailboxConfig(
        name="employee@example.com",
        display_name="Личный",
        imap_host="mail.example.com",
        imap_port=993,
        imap_user="employee@example.com",
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
        mailbox_type="personal",
        owner_sub=OWNER_SUB,
        sweep_enabled=False,
    )
    db_session.add(cfg)
    await db_session.commit()
    return cfg


async def test_admin_cannot_switch_on_ai_reading_of_private_mail(
    client: AsyncClient, db_session, personal_mailbox
):
    resp = await client.patch(
        f"/api/admin/users/{OWNER_SUB}/mailbox/sweep", json={"sweep_enabled": True}
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(personal_mailbox)
    assert personal_mailbox.sweep_enabled is False  # still the owner's call

    from sqlalchemy import select

    notif = (
        (await db_session.execute(select(Notification).where(Notification.user_sub == OWNER_SUB)))
        .scalars()
        .all()
    )
    assert len(notif) == 1
    assert "разрешить" in notif[0].title.lower()


async def test_admin_may_still_switch_it_off(client: AsyncClient, db_session, personal_mailbox):
    personal_mailbox.sweep_enabled = True
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/users/{OWNER_SUB}/mailbox/sweep", json={"sweep_enabled": False}
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(personal_mailbox)
    assert personal_mailbox.sweep_enabled is False


async def test_preferences_round_trip(client: AsyncClient):
    listed = await client.get("/api/notifications/preferences")
    assert listed.status_code == 200
    types = {p["type"] for p in listed.json()}
    assert "email_received" in types
    assert all(p["push"] and p["in_app"] for p in listed.json())  # defaults

    saved = await client.put(
        "/api/notifications/preferences",
        json={"type": "email_received", "push": False, "private_preview": True},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "type": "email_received",
        "in_app": True,
        "push": False,
        "private_preview": True,
    }

    again = await client.get("/api/notifications/preferences")
    pref = next(p for p in again.json() if p["type"] == "email_received")
    assert pref["push"] is False and pref["private_preview"] is True


async def test_unknown_preference_type_is_rejected(client: AsyncClient):
    resp = await client.put("/api/notifications/preferences", json={"type": "nonsense"})
    assert resp.status_code == 422


async def test_push_is_suppressed_and_redacted_per_preference(db_session, monkeypatch):
    """The in-app row keeps the full text; only the push payload is affected."""
    from app.db.models import UserNotificationPref
    from app.services import notifications as notif_service

    sent: list[tuple[str, str]] = []

    async def fake_push(db, user_sub, title, body, **kwargs):
        sent.append((title, body))

    monkeypatch.setattr(notif_service.push, "push_to_user", fake_push)

    # 1. No preference row, non-sensitive content → full preview.
    await notif_service.create_notification(
        db_session,
        user_sub=OWNER_SUB,
        type=NotificationType.email_received,
        title="Новое письмо · procurement",
        body="supplier@example.com: Счёт",
    )
    assert sent[-1] == ("Новое письмо · procurement", "supplier@example.com: Счёт")

    # 2. Personal mailbox → redacted by default, row still has the full text.
    notif = await notif_service.create_notification(
        db_session,
        user_sub=OWNER_SUB,
        type=NotificationType.email_received,
        title="Новое письмо · employee@example.com",
        body="doctor@clinic.example: Анализы",
        private_preview=True,
    )
    assert sent[-1][0] == "Новое уведомление"
    assert "clinic" not in sent[-1][1]
    assert notif.body == "doctor@clinic.example: Анализы"

    # 3. An explicit preference wins over the caller's default.
    db_session.add(
        UserNotificationPref(
            user_sub=OWNER_SUB,
            type="email_received",
            in_app=True,
            push=True,
            private_preview=False,
        )
    )
    await db_session.commit()
    await notif_service.create_notification(
        db_session,
        user_sub=OWNER_SUB,
        type=NotificationType.email_received,
        title="Новое письмо · employee@example.com",
        body="doctor@clinic.example: Анализы",
        private_preview=True,
    )
    assert sent[-1][1] == "doctor@clinic.example: Анализы"

    # 4. push=False → nothing leaves at all.
    from sqlalchemy import select

    row = (
        await db_session.execute(
            select(UserNotificationPref).where(UserNotificationPref.user_sub == OWNER_SUB)
        )
    ).scalar_one()
    row.push = False
    await db_session.commit()
    before = len(sent)
    await notif_service.create_notification(
        db_session,
        user_sub=OWNER_SUB,
        type=NotificationType.email_received,
        title="Ещё письмо",
        body="кто-то: тема",
    )
    assert len(sent) == before
