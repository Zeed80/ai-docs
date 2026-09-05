"""Tests for Email Workspace API — drafts, risk-check, templates."""

import pytest
from httpx import AsyncClient

from app.db.models import (
    EmailMessage,
    EmailThread,
    Party,
    PartyRole,
)


@pytest.fixture
async def supplier_with_email(db_session):
    party = Party(
        name="ACME Corp",
        inn="9876543210",
        role=PartyRole.supplier,
        contact_email="sales@acme.com",
    )
    db_session.add(party)
    await db_session.commit()
    return party


@pytest.fixture
async def email_thread(db_session):
    thread = EmailThread(
        subject="Счёт №123",
        mailbox="procurement",
        message_count=2,
    )
    db_session.add(thread)
    await db_session.flush()

    msg1 = EmailMessage(
        thread_id=thread.id,
        mailbox="procurement",
        from_address="sales@acme.com",
        to_addresses=["procurement@company.ru"],
        subject="Счёт №123",
        body_text="Добрый день! Прошу оплатить счёт №123.",
        is_inbound=True,
    )
    msg2 = EmailMessage(
        thread_id=thread.id,
        mailbox="procurement",
        from_address="procurement@company.ru",
        to_addresses=["sales@acme.com"],
        subject="Re: Счёт №123",
        body_text="Спасибо, оплатим в ближайшее время.",
        is_inbound=False,
    )
    db_session.add_all([msg1, msg2])
    await db_session.commit()
    return thread


@pytest.mark.asyncio
async def test_email_search(client: AsyncClient, email_thread):
    resp = await client.post(
        "/api/email/search",
        json={
            "query": "Счёт",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_threads(client: AsyncClient, email_thread):
    resp = await client.get("/api/email/threads")
    assert resp.status_code == 200
    body = resp.json()
    threads = body["items"]
    assert len(threads) >= 1
    assert threads[0]["subject"] == "Счёт №123"
    assert "next_cursor" in body


@pytest.mark.asyncio
async def test_get_thread(client: AsyncClient, email_thread):
    resp = await client.get(f"/api/email/threads/{email_thread.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2


@pytest.mark.asyncio
async def test_draft_lifecycle(client: AsyncClient, db_session):
    # Ящик-отправитель нужен уже на этапе создания: черновик без ящика
    # отправить нельзя (иначе адрес отправителя выбирался бы после
    # подтверждения человека — и мог оказаться личным ящиком сотрудника).
    from app.db.models import MailboxConfig

    db_session.add(
        MailboxConfig(
            name="lifecycle-box",
            imap_host="imap.example.com",
            imap_port=993,
            imap_user="lifecycle",
            imap_password_encrypted="x",
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="lifecycle@example.com",
            smtp_password_encrypted="y",
            is_active=True,
        )
    )
    await db_session.commit()

    # Create draft
    resp = await client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["supplier@example.com"],
            "subject": "Тестовое письмо",
            "body_html": "<p>Добрый день!</p>",
            "mailbox": "lifecycle-box",
        },
    )
    assert resp.status_code == 200
    draft = resp.json()
    draft_id = draft["id"]
    assert draft["status"] == "draft"

    # List drafts
    resp = await client.get("/api/email/drafts")
    assert resp.status_code == 200
    drafts = resp.json()
    assert any(d["id"] == draft_id for d in drafts)

    # Risk check
    resp = await client.post(f"/api/email/drafts/{draft_id}/risk-check")
    assert resp.status_code == 200
    risk = resp.json()
    assert "flags" in risk
    # External domain should be flagged
    assert any(f["code"] == "external_domain" for f in risk["flags"])

    # Send — queues a Celery task (no worker in tests), status becomes "queued".
    # expected_digest обязателен: подтверждают текст письма, а не его id.
    digest = (
        risk["content_digest"]
        if "content_digest" in risk
        else ((await client.get(f"/api/email/drafts/{draft_id}")).json()["content_digest"])
    )
    resp = await client.post(f"/api/email/drafts/{draft_id}/send", json={"expected_digest": digest})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("sent", "queued")

    # Cannot send again
    resp = await client.post(f"/api/email/drafts/{draft_id}/send", json={"expected_digest": digest})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_risk_check_sensitive_content(client: AsyncClient):
    resp = await client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["supplier@example.com"],
            "subject": "Конфиденциально",
            "body_html": "<p>Это конфиденциальная информация</p>",
            "body_text": "Это конфиденциальная информация",
        },
    )
    draft_id = resp.json()["id"]

    resp = await client.post(f"/api/email/drafts/{draft_id}/risk-check")
    assert resp.status_code == 200
    risk = resp.json()
    assert risk["is_safe"] is False
    assert any(f["code"] == "sensitive_content" for f in risk["flags"])


@pytest.mark.asyncio
async def test_suggest_template(client: AsyncClient):
    resp = await client.post(
        "/api/email/suggest-template",
        json={
            "context_type": "payment_reminder",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["templates"]) >= 1
    assert data["recommended"] == "payment_reminder"
    assert "{invoice_number}" in data["templates"][0]["subject"]


@pytest.mark.asyncio
async def test_style_analyze_no_matching_emails(client: AsyncClient, supplier_with_email):
    # supplier_with_email has contact_email=sales@acme.com, but no emails in DB from that address
    resp = await client.post(
        "/api/email/style-analyze",
        json={
            "email_address": "nobody-has-this@nowhere.test",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tone"] in ("neutral", "formal", "friendly")  # AI may still try to analyze
