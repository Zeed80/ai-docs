"""Ф0.5 — a filter rule may only act where its author may.

The engine used to select rules by mailbox alone, so a rule any employee
created with ``mailbox=None`` ran against every mailbox in the company,
including colleagues' personal ones. The dry-run had the mirror problem: it
scanned all mailboxes and returned their subject lines.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.models import UserInfo, UserRole
from app.db.models import EmailMessage, EmailRule, EmailThread, MailboxConfig

OWNER_SUB = "dev-user"
COLLEAGUE_SUB = "colleague-sub"


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


def _message(mailbox: str, subject: str) -> tuple[EmailThread, EmailMessage]:
    thread = EmailThread(subject=subject, mailbox=mailbox, message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox=mailbox,
        subject=subject,
        from_address="supplier@example.com",
        to_addresses=["x@example.com"],
        body_text="счёт на оплату",
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    return thread, msg


@pytest_asyncio.fixture
async def people_client(db_session) -> AsyncIterator[AsyncClient]:
    """Client whose identity is picked per request via ``X-Test-Sub``."""
    from fastapi import Request

    from app.auth.acting import get_effective_user
    from app.config import settings
    from app.db.session import get_db
    from app.main import app

    settings.rate_limit_api_per_minute = 0
    people = {
        COLLEAGUE_SUB: UserInfo(
            sub=COLLEAGUE_SUB,
            email="colleague@example.com",
            name="Коллега",
            preferred_username="colleague",
            roles=[UserRole.viewer],
        ),
        OWNER_SUB: UserInfo(
            sub=OWNER_SUB,
            email="admin@example.com",
            name="Админ",
            preferred_username="admin",
            roles=[UserRole.admin],
        ),
    }

    async def override_get_db():
        yield db_session

    def override_effective_user(request: Request) -> UserInfo:
        # Annotated on purpose: an unannotated `request` parameter is read by
        # FastAPI as a query parameter, and every call 422s.
        return people[(request.headers.get("x-test-sub") or OWNER_SUB)]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_effective_user] = override_effective_user
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Test-Sub": COLLEAGUE_SUB}
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mailboxes(db_session):
    db_session.add_all(
        [
            _mailbox("procurement", owner_sub=None, mailbox_type="shared"),
            _mailbox("colleague@example.com", owner_sub=COLLEAGUE_SUB, mailbox_type="personal"),
            _mailbox("boss@example.com", owner_sub="boss-sub", mailbox_type="personal"),
        ]
    )
    await db_session.commit()


async def test_non_admin_cannot_create_an_all_mailboxes_rule(people_client, mailboxes):
    resp = await people_client.post(
        "/api/email/rules",
        json={
            "name": "Всё подряд",
            "mailbox": None,
            "conditions": {
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
            },
            "actions": [{"type": "star"}],
        },
    )
    assert resp.status_code == 403


async def test_non_admin_cannot_create_a_rule_for_someone_elses_mailbox(people_client, mailboxes):
    resp = await people_client.post(
        "/api/email/rules",
        json={
            "name": "Чужой ящик",
            "mailbox": "boss@example.com",
            "conditions": {
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
            },
            "actions": [{"type": "star"}],
        },
    )
    assert resp.status_code == 403


async def test_admin_may_still_create_a_company_wide_rule(people_client, mailboxes):
    resp = await people_client.post(
        "/api/email/rules",
        headers={"X-Test-Sub": OWNER_SUB},
        json={
            "name": "Общая маркировка",
            "mailbox": None,
            "conditions": {
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
            },
            "actions": [{"type": "star"}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["owner_sub"] is None


async def test_engine_ignores_a_personal_rule_in_another_mailbox(db_session, mailboxes):
    """The core of Ф0.5: rule selection is owner-aware, not mailbox-only."""
    from app.db.sync_session import sync_session  # noqa: F401  (import shape check)
    from app.domain.email_rules import apply_rules

    # A rule the colleague owns, scoped to their own mailbox…
    db_session.add(
        EmailRule(
            name="Моё правило",
            mailbox="colleague@example.com",
            owner_sub=COLLEAGUE_SUB,
            is_active=True,
            priority=10,
            conditions={
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
            },
            actions=[{"type": "star"}],
        )
    )
    thread, msg = _message("boss@example.com", "Счёт от поставщика")
    db_session.add_all([thread, msg])
    await db_session.commit()

    # …must not fire on the boss's private mail.
    applied = await db_session.run_sync(
        lambda session: apply_rules(session, msg, "boss@example.com")
    )
    assert applied == []
    assert not msg.is_starred


async def test_engine_applies_a_shared_admin_rule_everywhere(db_session, mailboxes):
    from app.domain.email_rules import apply_rules

    db_session.add(
        EmailRule(
            name="Общая",
            mailbox=None,
            owner_sub=None,
            is_active=True,
            priority=10,
            conditions={
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
            },
            actions=[{"type": "star"}],
        )
    )
    thread, msg = _message("procurement", "Счёт от поставщика")
    db_session.add_all([thread, msg])
    await db_session.commit()

    applied = await db_session.run_sync(
        lambda sync_session: apply_rules(sync_session, msg, "procurement")
    )
    assert [a["type"] for a in applied] == ["star"]
    assert msg.is_starred


async def test_dry_run_does_not_leak_other_peoples_subjects(people_client, db_session, mailboxes):
    """Residual risk after Ф0.5: rows created BEFORE the create-time check —
    a rule owned by an ordinary employee with mailbox=None. The engine now
    refuses to run it outside their own mailbox, and the dry-run must not show
    them other people's subject lines either."""
    thread, msg = _message("boss@example.com", "Секретное письмо начальника про счёт")
    db_session.add_all([thread, msg])
    legacy = EmailRule(
        name="Легаси-правило",
        mailbox=None,
        owner_sub=COLLEAGUE_SUB,
        is_active=True,
        priority=10,
        conditions={
            "match": "all",
            "rules": [{"field": "subject", "op": "contains", "value": "счёт"}],
        },
        actions=[{"type": "star"}],
    )
    db_session.add(legacy)
    await db_session.commit()

    resp = await people_client.post(f"/api/email/rules/{legacy.id}/test", json={"last_n": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    subjects = " ".join(body["sample_subjects"])
    assert "Секретное" not in subjects
    assert body["matched"] == 0


async def test_dry_run_sees_real_attachments(people_client, db_session, mailboxes):
    """An attachment condition used to be false in the preview and true in
    production, because the dry-run passed an empty attachment list."""
    from app.db.models import EmailAttachment

    thread, msg = _message("procurement", "Документы")
    msg.has_attachments = True
    db_session.add_all([thread, msg])
    await db_session.flush()
    db_session.add(
        EmailAttachment(
            message_id=msg.id,
            filename="счёт-2026.pdf",
            content_type="application/pdf",
            size=100,
            storage_path="documents/aa/bb/cc",
            sha256="c" * 64,
        )
    )
    rule = EmailRule(
        name="По имени вложения",
        mailbox="procurement",
        owner_sub=None,
        is_active=True,
        priority=10,
        conditions={
            "match": "all",
            "rules": [{"field": "attachment_name", "op": "contains", "value": "счёт"}],
        },
        actions=[{"type": "star"}],
    )
    db_session.add(rule)
    await db_session.commit()

    resp = await people_client.post(
        f"/api/email/rules/{rule.id}/test",
        headers={"X-Test-Sub": OWNER_SUB},
        json={"last_n": 50},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched"] == 1
