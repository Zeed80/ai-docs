"""Address book, signatures, auto-send policy, the sent-in-inbox fix."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import (
    DraftAction,
    EmailContact,
    EmailMessage,
    EmailRule,
    EmailRuleLog,
    EmailSignature,
    EmailTemplateDB,
    EmailThread,
    MailboxConfig,
    MailServerConfig,
    Party,
    PartyRole,
)


def _shared(name="procurement"):
    return MailboxConfig(
        name=name,
        imap_host="h",
        imap_port=993,
        imap_user=name,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
        mailbox_type="shared",
        smtp_host="smtp.h",
        smtp_user=name,
        smtp_password_encrypted="x",
        smtp_from_address=f"{name}@company.ru",
    )


# ── Address book ──────────────────────────────────────────────────────────


async def test_contact_crud_and_autocomplete(client: AsyncClient, db_session):
    db_session.add(_shared())
    db_session.add(
        Party(name="ООО Ромашка", role=PartyRole.supplier, contact_email="sales@romashka.ru")
    )
    await db_session.commit()

    r = await client.post(
        "/api/email/contacts/book",
        json={
            "email": "ivan@partner.ru",
            "name": "Иван Петров",
            "organization": "Партнёр ООО",
            "is_favorite": True,
            "tags": ["ключевой"],
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = await client.get("/api/email/contacts/book")
    assert any(c["id"] == cid for c in r.json())

    # autocomplete merges book + party
    r = await client.get("/api/email/contacts?q=r")
    emails = {c["email"] for c in r.json()}
    assert "ivan@partner.ru" in emails or "sales@romashka.ru" in emails

    r = await client.patch(f"/api/email/contacts/book/{cid}", json={"phone": "+7 495 000"})
    assert r.status_code == 200 and r.json()["phone"] == "+7 495 000"

    r = await client.delete(f"/api/email/contacts/book/{cid}")
    assert r.status_code == 204


async def test_send_remembers_recipient_as_contact(client: AsyncClient, db_session):
    db_session.add(_shared())
    await db_session.commit()

    r = await client.post(
        "/api/email/send",
        json={
            "mailbox": "procurement",
            "to_addresses": ["new-guy@buyer.ru"],
            "subject": "Привет",
            "body_html": "<p>hi</p>",
            "body_text": "hi",
        },
    )
    assert r.status_code == 200, r.text

    c = (
        await db_session.execute(
            __import__("sqlalchemy")
            .select(EmailContact)
            .where(EmailContact.email == "new-guy@buyer.ru")
        )
    ).scalar_one_or_none()
    assert c is not None and c.source == "auto"


# ── Signatures ────────────────────────────────────────────────────────────


async def test_signature_resolve_prefers_mailbox_then_user(client: AsyncClient, db_session):
    db_session.add(_shared())
    db_session.add(
        EmailSignature(
            name="Личная", body_html="<p>-- Я</p>", owner_sub="dev-user", is_default=True
        )
    )
    db_session.add(
        EmailSignature(
            name="Отдел",
            body_html="<p>-- Отдел закупок</p>",
            owner_sub=None,
            mailbox="procurement",
            is_default=True,
        )
    )
    await db_session.commit()

    r = await client.get("/api/email/signatures/resolve?mailbox=procurement")
    assert r.status_code == 200 and "Отдел закупок" in (r.json() or {}).get("body_html", "")

    r = await client.get("/api/email/signatures/resolve")
    assert "Я" in (r.json() or {}).get("body_html", "")


# ── Sent threads not in inbox ─────────────────────────────────────────────


async def test_composed_thread_goes_to_sent_not_inbox(db_session):
    db_session.add(_shared())
    await db_session.commit()

    from app.domain.email_thread import record_outbound_message

    await record_outbound_message(
        db_session,
        mailbox="procurement",
        draft_data={"to_addresses": ["x@y.ru"], "subject": "Новое письмо", "body_text": "текст"},
        smtp_message_id="<m1@company.ru>",
        from_address="procurement@company.ru",
    )
    await db_session.commit()

    th = (
        await db_session.execute(
            __import__("sqlalchemy")
            .select(EmailThread)
            .where(EmailThread.subject == "Новое письмо")
        )
    ).scalar_one()
    assert th.folder == "sent"


# ── Auto-send guardrails ──────────────────────────────────────────────────


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(url, pool_pre_ping=True)
    import app.db.sync_session as m

    monkeypatch.setattr(m, "sync_session", lambda: Session(engine))
    tables = (
        EmailRuleLog,
        DraftAction,
        EmailRule,
        EmailTemplateDB,
        EmailMessage,
        EmailThread,
        MailboxConfig,
        MailServerConfig,
    )

    def wipe():
        with Session(engine) as db:
            for t in tables:
                db.execute(delete(t))
            db.commit()

    wipe()
    try:
        yield engine
    finally:
        wipe()
        engine.dispose()


def test_auto_send_blocked_unless_policy_enabled(sync_db):
    from app.domain.email_rules import apply_rules

    with Session(sync_db) as db:
        db.add(_shared())
        tpl = EmailTemplateDB(
            name="A",
            slug=f"a-{uuid.uuid4().hex[:6]}",
            subject="Ответ",
            body_html="<p>ок</p>",
            body_text="ок",
            is_builtin=False,
        )
        db.add(tpl)
        th = EmailThread(
            subject="Вопрос",
            mailbox="procurement",
            message_count=1,
            last_message_at=datetime.now(UTC),
        )
        db.add(th)
        db.flush()
        msg = EmailMessage(
            thread_id=th.id,
            mailbox="procurement",
            from_address="c@b.ru",
            subject="Вопрос",
            body_text="?",
            is_inbound=True,
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@b.ru>",
        )
        db.add(msg)
        db.add(
            EmailRule(
                name="auto",
                mailbox=None,
                owner_sub=None,
                is_active=True,
                priority=1,
                auto_send=True,
                conditions={
                    "match": "all",
                    "rules": [{"field": "subject", "op": "contains", "value": "вопрос"}],
                },
                actions=[{"type": "auto_reply_template", "template_id": str(tpl.id)}],
            )
        )
        db.commit()

        apply_rules(db, msg, "procurement")
        db.commit()

        d = db.query(DraftAction).one()
        assert d.draft_data["status"] == "draft"  # policy off → draft, not sent
        assert d.executed is False


async def test_autocomplete_keeps_display_name_from_history(client: AsyncClient, db_session):
    db_session.add(_shared())
    th = EmailThread(
        subject="Переписка",
        mailbox="procurement",
        message_count=1,
        last_message_at=datetime.now(UTC),
    )
    db_session.add(th)
    await db_session.flush()
    db_session.add(
        EmailMessage(
            thread_id=th.id,
            mailbox="procurement",
            from_address='"Пётр Смирнов" <petr@zavod.ru>',
            subject="Переписка",
            body_text="t",
            is_inbound=True,
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@z.ru>",
        )
    )
    await db_session.commit()

    r = await client.get("/api/email/contacts?q=смирнов")
    hit = next((c for c in r.json() if c["email"] == "petr@zavod.ru"), None)
    assert hit is not None and hit["name"] == "Пётр Смирнов"


async def test_autocomplete_empty_query_returns_favorites(client: AsyncClient, db_session):
    db_session.add(
        EmailContact(
            email="fav@x.ru",
            name="Любимый",
            owner_sub="dev-user",
            is_favorite=True,
            source="manual",
            use_count=9,
        )
    )
    db_session.add(
        EmailContact(
            email="rare@x.ru", name="Редкий", owner_sub="dev-user", source="manual", use_count=0
        )
    )
    await db_session.commit()

    r = await client.get("/api/email/contacts")  # no q
    emails = [c["email"] for c in r.json()]
    assert emails and emails[0] == "fav@x.ru"


async def test_contacts_csv_import_export(client: AsyncClient, db_session):
    r = await client.post(
        "/api/email/contacts/import",
        json={
            "csv": "name,email,organization\nАнна,anna@buyer.ru,Байер ООО\n,bad-line,\n",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1 and r.json()["skipped"] == 1

    r = await client.get("/api/email/contacts/export")
    assert r.status_code == 200 and "anna@buyer.ru" in r.text


async def test_all_attachments_download_as_one_archive(client, db_session, monkeypatch):
    """Ф5.3 — письмо с одиннадцатью спецификациями это одиннадцать кликов, и
    одиннадцатый как раз пропускают. Инлайновый логотип подписи в архив не
    попадает: вложением его никто не считает."""
    import io
    import uuid as _uuid
    import zipfile
    from datetime import datetime

    from app.db.models import (
        EmailAttachment,
        EmailMessage,
        EmailThread,
        MailboxConfig,
    )

    db_session.add(
        MailboxConfig(
            name="zipbox",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="zipbox",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    thread = EmailThread(subject="Спецификации", mailbox="zipbox", message_count=1)
    db_session.add(thread)
    await db_session.flush()
    msg = EmailMessage(
        thread_id=thread.id,
        mailbox="zipbox",
        subject="Спецификации",
        from_address="supplier@example.com",
        to_addresses=["zipbox@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{_uuid.uuid4()}@example.com>",
        has_attachments=True,
        attachment_count=3,
    )
    db_session.add(msg)
    await db_session.flush()
    db_session.add_all(
        [
            EmailAttachment(
                message_id=msg.id,
                filename="спец-1.pdf",
                content_type="application/pdf",
                size=3,
                storage_path="s/1.pdf",
                sha256="a" * 64,
            ),
            EmailAttachment(
                message_id=msg.id,
                filename="спец-1.pdf",
                content_type="application/pdf",
                size=3,
                storage_path="s/2.pdf",
                sha256="b" * 64,
            ),
            EmailAttachment(
                message_id=msg.id,
                filename="logo.png",
                content_type="image/png",
                size=3,
                is_inline=True,
                storage_path="s/logo.png",
                sha256="c" * 64,
            ),
        ]
    )
    await db_session.commit()

    import app.storage as storage

    monkeypatch.setattr(storage, "download_file", lambda path: path.encode())

    resp = await client.get(f"/api/email/messages/{msg.id}/attachments/archive")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"

    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "logo.png" not in names
    # Both real attachments survive despite sharing a filename.
    assert len(names) == 2
    assert "спец-1.pdf" in names
