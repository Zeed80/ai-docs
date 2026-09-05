"""Что должен уметь почтовый клиент, чтобы им можно было пользоваться.

Каждый тест закрывает найденный при разборе клиента дефект — в названии то,
что происходило раньше.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.db.models import EmailMessage, EmailThread, MailboxConfig


@pytest.fixture
async def box(db_session):
    cfg = MailboxConfig(
        name="ux-box",
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="ux",
        imap_password_encrypted="x",
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="ux@example.com",
        smtp_password_encrypted="y",
        is_active=True,
    )
    db_session.add(cfg)
    await db_session.commit()
    return cfg


async def _draft(client: AsyncClient, **over) -> dict:
    payload = {
        "to_addresses": ["supplier@example.com"],
        "subject": "Черновик",
        "body_html": "<p>Текст</p>",
        "mailbox": "ux-box",
        **over,
    }
    resp = await client.post("/api/email/drafts", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Вложения черновика ─────────────────────────────────────────────────────


async def test_draft_returns_attachment_names_not_only_ids(client: AsyncClient, db_session, box):
    """Композер показывает вложения по имени. Пока ответ нёс одни id, заново
    открытый черновик выглядел так, будто вложений в нём не было — и
    следующее автосохранение стирало их пустым списком."""
    from app.db.models import EmailAttachment

    att = EmailAttachment(
        filename="счёт-2026-04.pdf",
        content_type="application/pdf",
        size=12345,
        sha256="a" * 64,
        storage_path="x/y",
        uploaded_by_sub="dev-user",
    )
    db_session.add(att)
    await db_session.commit()

    draft = await _draft(client, attachment_ids=[str(att.id)])
    assert [a["filename"] for a in draft["attachments"]] == ["счёт-2026-04.pdf"]
    assert draft["attachments"][0]["size"] == 12345

    # И при повторном чтении — тоже.
    again = await client.get(f"/api/email/drafts/{draft['id']}")
    assert [a["id"] for a in again.json()["attachments"]] == [str(att.id)]


async def test_draft_keeps_the_conversation_it_replies_to(client: AsyncClient, db_session, box):
    """Черновик ответа терял in_reply_to и thread_id: письмо уходило как
    новое и выпадало из переписки у получателя."""
    thread = EmailThread(subject="Счёт", mailbox="ux-box", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="ux-box",
        subject="Счёт",
        from_address="supplier@example.com",
        to_addresses=["ux@example.com"],
        body_text="текст",
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
        is_inbound=True,
    )
    db_session.add_all([thread, msg])
    await db_session.commit()

    draft = await _draft(client, thread_id=str(thread.id), in_reply_to_message_id=str(msg.id))
    assert draft["thread_id"] == str(thread.id)
    assert draft["in_reply_to_message_id"] == str(msg.id)


# ── Черновики: удаление и область видимости ────────────────────────────────


async def test_a_draft_can_be_thrown_away(client: AsyncClient, box):
    """Удаления не было ни в API, ни в интерфейсе, а автосохранение создаёт
    черновик после пятнадцати секунд печатания."""
    draft = await _draft(client)
    assert (await client.delete(f"/api/email/drafts/{draft['id']}")).status_code == 204
    assert (await client.get(f"/api/email/drafts/{draft['id']}")).status_code == 404


async def test_a_sent_draft_is_not_deletable(client: AsyncClient, db_session, box):
    """Журнал отправленного — не корзина."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.db.models import DraftAction

    draft = await _draft(client)
    row = await db_session.get(DraftAction, uuid.UUID(draft["id"]))
    row.draft_data = {**row.draft_data, "status": "queued"}
    flag_modified(row, "draft_data")
    await db_session.commit()

    resp = await client.delete(f"/api/email/drafts/{draft['id']}")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "already_sent"


async def test_drafts_can_be_scoped_to_the_open_mailbox(client: AsyncClient, db_session, box):
    """Папка «Черновики» показывала всё скопом, независимо от выбранного ящика."""
    db_session.add(
        MailboxConfig(
            name="other-box",
            imap_host="imap.example.com",
            imap_port=993,
            imap_user="other",
            imap_password_encrypted="x",
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="other@example.com",
            smtp_password_encrypted="y",
            is_active=True,
        )
    )
    await db_session.commit()

    await _draft(client, subject="Из ux-box")
    await _draft(client, subject="Из other-box", mailbox="other-box")

    only = await client.get("/api/email/drafts?mailbox=ux-box")
    assert [d["subject"] for d in only.json()] == ["Из ux-box"]


# ── Список писем ───────────────────────────────────────────────────────────


async def test_sent_folder_shows_the_recipient_not_ourselves(client: AsyncClient, db_session, box):
    """В «Отправленных» sender — это мы, и список выглядел как переписка с
    собой: собеседника там нужно брать из получателей."""
    thread = EmailThread(
        subject="Запрос КП",
        mailbox="ux-box",
        message_count=1,
        folder="sent",
    )
    db_session.add_all(
        [
            thread,
            EmailMessage(
                thread=thread,
                mailbox="ux-box",
                subject="Запрос КП",
                from_address="ux@example.com",
                to_addresses=["sales@romex.example"],
                body_text="текст",
                sent_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                message_id_header=f"<{uuid.uuid4()}@example.com>",
                is_inbound=False,
                folder="sent",
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/email/threads?folder=sent&limit=50")
    row = next(t for t in resp.json()["items"] if t["id"] == str(thread.id))
    assert row["counterparty"] == "sales@romex.example"


# ── Адресная книга ─────────────────────────────────────────────────────────


async def test_adding_a_known_sender_to_contacts_twice_is_not_an_error(
    client: AsyncClient,
):
    """Кнопку «в контакты» нажимают, не помня, есть ли уже такой адрес:
    409 сообщал бы человеку о его же памяти."""
    first = await client.post(
        "/api/email/contacts/book",
        json={"email": "sales@romex.example", "name": "Ромекс", "upsert": True},
    )
    assert first.status_code == 201, first.text

    again = await client.post(
        "/api/email/contacts/book",
        json={"email": "sales@romex.example", "name": "Ромекс", "upsert": True},
    )
    assert again.status_code == 200
    assert again.json()["email"] == "sales@romex.example"


async def test_import_says_which_rows_it_skipped(client: AsyncClient):
    """«Пропущено 13» без строк и причин невозможно ни исправить, ни проверить."""
    csv = "email,name\nsales@romex.example,Ромекс\nне-адрес,Кто-то\n,Пусто\n"
    resp = await client.post("/api/email/contacts/import", json={"csv": csv})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 2
    assert {r["line"] for r in body["skipped_rows"]} == {3, 4}
    assert all(r["reason"] for r in body["skipped_rows"])
