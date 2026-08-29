"""Ф1.4 — доверие удалённым изображениям: per-user и per-контакт.

Блокировка по умолчанию защищает от трекинг-пикселя. Но если единственный
способ перестать нажимать «Показать» — выключить защиту целиком, её выключают
целиком, и защиты не остаётся ни от кого.
"""

import uuid
from datetime import datetime, timezone


async def _thread_with_sender(db_session, mailbox: str, sender: str):
    from app.db.models import EmailMessage, EmailThread, MailboxConfig

    db_session.add(MailboxConfig(
        name=mailbox, imap_host="m.example.com", imap_port=993, imap_user=mailbox,
        imap_password_encrypted="x", imap_ssl=True, is_active=True,
    ))
    thread = EmailThread(subject="Рассылка", mailbox=mailbox, message_count=1)
    db_session.add(thread)
    await db_session.flush()
    db_session.add(EmailMessage(
        thread_id=thread.id, mailbox=mailbox, subject="Рассылка",
        from_address=sender, to_addresses=[f"{mailbox}@example.com"],
        body_html='<p><img data-blocked-src="https://tracker.example/p.gif"></p>',
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    ))
    await db_session.commit()
    return thread


async def test_images_stay_blocked_for_an_unknown_sender(client, db_session):
    thread = await _thread_with_sender(db_session, "trustbox", "stranger@ads.example")

    resp = await client.get(f"/api/email/threads/{thread.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["messages"][0]["images_trusted"] is False


async def test_trusting_one_sender_does_not_trust_the_others(client, db_session):
    """Доверие точечное — иначе это тот же «выключить защиту», только длиннее."""
    thread = await _thread_with_sender(db_session, "trustbox2", "Поставщик <sales@known.example>")

    resp = await client.post(
        "/api/email/contacts/trust-images",
        json={"email": "sales@known.example", "name": "Поставщик"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["trust_images"] is True

    resp = await client.get(f"/api/email/threads/{thread.id}")
    assert resp.json()["messages"][0]["images_trusted"] is True

    other = await _thread_with_sender(db_session, "trustbox3", "stranger@ads.example")
    resp = await client.get(f"/api/email/threads/{other.id}")
    assert resp.json()["messages"][0]["images_trusted"] is False


async def test_trust_is_idempotent_and_revocable(client, db_session):
    """Кнопка «доверять» не заводит контакт — она снимает раздражитель, и
    нажать её дважды должно быть можно."""
    await _thread_with_sender(db_session, "trustbox4", "repeat@known.example")

    for _ in range(2):
        resp = await client.post(
            "/api/email/contacts/trust-images", json={"email": "repeat@known.example"},
        )
        assert resp.status_code == 200, resp.text

    resp = await client.post(
        "/api/email/contacts/trust-images",
        json={"email": "repeat@known.example", "trust": False},
    )
    assert resp.json()["trust_images"] is False


async def test_always_show_images_covers_every_sender(client, db_session):
    from app.db.models import User

    thread = await _thread_with_sender(db_session, "trustbox5", "stranger@ads.example")

    # The preference lives on the user's row; in production it is created at
    # login (upsert_user), here it has to exist for the setting to have a home.
    me = (await client.get("/api/auth/me")).json()
    if not (await db_session.execute(
        __import__("sqlalchemy").select(User).where(User.sub == me["sub"])
    )).scalar_one_or_none():
        db_session.add(User(
            sub=me["sub"], email=me.get("email") or "t@example.com",
            name=me.get("name") or "Тест", preferred_username="test",
            role="admin", is_active=True,
        ))
        await db_session.commit()

    resp = await client.patch(
        "/api/email/preferences", json={"always_show_images": True},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/email/threads/{thread.id}")
    assert resp.json()["messages"][0]["images_trusted"] is True

    # And it is genuinely persisted, not just echoed back.
    resp = await client.get("/api/email/preferences")
    assert resp.json()["always_show_images"] is True
