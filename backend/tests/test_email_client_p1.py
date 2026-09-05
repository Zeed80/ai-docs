"""P1: the mail view becomes a real client — threading, folders, labels, search."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient

from app.db.models import (
    EmailMessage,
    EmailThread,
    MailboxConfig,
)


def _shared_mailbox(name: str = "procurement") -> MailboxConfig:
    return MailboxConfig(
        name=name,
        display_name=name,
        imap_host="h",
        imap_port=993,
        imap_user=name,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
        mailbox_type="shared",
    )


def _thread(mailbox: str, subject: str, **kw) -> EmailThread:
    return EmailThread(
        subject=subject, mailbox=mailbox, message_count=1, last_message_at=datetime.now(UTC), **kw
    )


def _msg(thread: EmailThread, mailbox: str, subject: str, body: str, **kw) -> EmailMessage:
    return EmailMessage(
        thread=thread,
        mailbox=mailbox,
        subject=subject,
        body_text=body,
        from_address=kw.pop("from_address", "supplier@partner.ru"),
        to_addresses=kw.pop("to_addresses", ["procurement@company.ru"]),
        received_at=datetime.now(UTC),
        is_inbound=kw.pop("is_inbound", True),
        message_id_header=kw.pop("message_id_header", f"<{uuid.uuid4()}@partner.ru>"),
        **kw,
    )


# ── Threading headers + outbound reflection ────────────────────────────────


def test_resolve_threading_headers_builds_references_chain():
    from app.domain.email_thread import resolve_threading_headers

    parent = EmailMessage(
        mailbox="procurement",
        from_address="x@y.ru",
        subject="s",
        message_id_header="<a@y.ru>",
        references="<root@y.ru>",
    )
    in_reply_to, references = resolve_threading_headers(parent)
    assert in_reply_to == "<a@y.ru>"
    assert references == "<root@y.ru> <a@y.ru>"

    assert resolve_threading_headers(None) == (None, None)


async def test_record_outbound_message_lands_in_thread(db_session):
    db_session.add(_shared_mailbox())
    th = _thread("procurement", "Заказ №5")
    parent = _msg(th, "procurement", "Заказ №5", "исходный текст")
    db_session.add_all([th, parent])
    await db_session.commit()

    from app.domain.email_thread import record_outbound_message

    msg = await record_outbound_message(
        db_session,
        mailbox="procurement",
        draft_data={
            "to_addresses": ["supplier@partner.ru"],
            "subject": "Re: Заказ №5",
            "body_text": "наш ответ",
            "in_reply_to_message_id": str(parent.id),
        },
        smtp_message_id="<reply-1@company.ru>",
        from_address="procurement@company.ru",
    )
    await db_session.commit()

    assert msg.is_inbound is False
    assert msg.folder == "sent"
    assert msg.in_reply_to == parent.message_id_header
    assert msg.thread_id == th.id
    await db_session.refresh(th)
    assert th.message_count == 2


# ── Bulk thread actions ───────────────────────────────────────────────────


async def test_bulk_thread_actions(client: AsyncClient, db_session):
    db_session.add(_shared_mailbox())
    th = _thread("procurement", "Счёт на оплату")
    db_session.add_all([th, _msg(th, "procurement", "Счёт на оплату", "текст")])
    await db_session.commit()

    r = await client.post(
        "/api/email/threads/actions", json={"thread_ids": [str(th.id)], "action": "read"}
    )
    assert r.status_code == 200 and r.json()["updated"] == 1
    await db_session.refresh(th)
    assert th.is_read is True

    r = await client.post(
        "/api/email/threads/actions", json={"thread_ids": [str(th.id)], "action": "archive"}
    )
    assert r.status_code == 200
    await db_session.refresh(th)
    assert th.folder == "archive"

    r = await client.get("/api/email/threads")
    # Ф5.1 — the endpoint now paginates: {items, total, next_cursor}.
    assert all(t["id"] != str(th.id) for t in r.json()["items"])  # archive hidden by default
    r = await client.get("/api/email/threads?folder=archive")
    assert any(t["id"] == str(th.id) for t in r.json()["items"])


# ── Labels ────────────────────────────────────────────────────────────────


async def test_label_crud_and_apply(client: AsyncClient, db_session):
    db_session.add(_shared_mailbox())
    th = _thread("procurement", "Рекламация")
    db_session.add_all([th, _msg(th, "procurement", "Рекламация", "текст")])
    await db_session.commit()

    r = await client.post("/api/email/labels", json={"name": "Срочно", "color": "#f00"})
    assert r.status_code == 201, r.text
    label_id = r.json()["id"]

    r = await client.post(
        "/api/email/threads/actions",
        json={"thread_ids": [str(th.id)], "action": "add_label", "label_id": label_id},
    )
    assert r.status_code == 200 and r.json()["updated"] == 1

    r = await client.get("/api/email/labels")
    assert next(lb for lb in r.json() if lb["id"] == label_id)["thread_count"] == 1

    r = await client.get(f"/api/email/threads?label_id={label_id}")
    assert any(t["id"] == str(th.id) for t in r.json()["items"])

    r = await client.get("/api/email/threads/" + str(th.id))
    assert any(lb["id"] == label_id for lb in r.json()["labels"])


# ── FTS search ────────────────────────────────────────────────────────────


async def test_fulltext_search_cyrillic(client: AsyncClient, db_session):
    db_session.add(_shared_mailbox())
    th = _thread("procurement", "Поставка подшипников")
    db_session.add_all(
        [
            th,
            _msg(
                th,
                "procurement",
                "Поставка подшипников",
                "Уважаемые коллеги, отгрузка запланирована на среду.",
            ),
        ]
    )
    other = _thread("procurement", "Отпуск сотрудника")
    db_session.add_all([other, _msg(other, "procurement", "Отпуск сотрудника", "заявление")])
    await db_session.commit()

    r = await client.post("/api/email/search", json={"query": "отгрузка подшипники"})
    assert r.status_code == 200, r.text
    subjects = [m["subject"] for m in r.json()["results"]]
    assert "Поставка подшипников" in subjects
    assert "Отпуск сотрудника" not in subjects


# ── Direct human send guard ───────────────────────────────────────────────


async def test_send_from_foreign_personal_mailbox_is_forbidden(client: AsyncClient, db_session):
    db_session.add(
        MailboxConfig(
            name="boss@company.ru",
            display_name="boss",
            owner_sub="someone-else",
            mailbox_type="personal",
            imap_host="h",
            imap_port=993,
            imap_user="boss",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    await db_session.commit()

    r = await client.post(
        "/api/email/send",
        json={
            "mailbox": "boss@company.ru",
            "to_addresses": ["x@y.ru"],
            "subject": "hi",
            "body_html": "<p>hi</p>",
        },
    )
    assert r.status_code == 403


async def test_send_from_shared_mailbox_queues_draft(client: AsyncClient, db_session):
    db_session.add(_shared_mailbox())
    await db_session.commit()

    r = await client.post(
        "/api/email/send",
        json={
            "mailbox": "procurement",
            "to_addresses": ["supplier@partner.ru"],
            "subject": "Запрос КП",
            "body_html": "<p>Добрый день</p>",
            "body_text": "Добрый день",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


# ── Contacts ──────────────────────────────────────────────────────────────


async def test_contacts_autocomplete_from_history(client: AsyncClient, db_session):
    db_session.add(_shared_mailbox())
    th = _thread("procurement", "История")
    db_session.add_all(
        [th, _msg(th, "procurement", "История", "t", from_address="ivan@supplier-abc.ru")]
    )
    await db_session.commit()

    r = await client.get("/api/email/contacts?q=supplier-abc")
    assert r.status_code == 200
    assert any(c["email"] == "ivan@supplier-abc.ru" for c in r.json())
