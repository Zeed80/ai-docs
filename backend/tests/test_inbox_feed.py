"""Ф7.1 — one feed of what is waiting for a person.

Mail lived on a screen the mobile bottom nav never linked to, so "нужно ли от
меня что-то" meant checking two places. Merged server-side because only the
server can order three sources by time AND apply personal-mailbox visibility
while doing it — a client-side merge would leak a colleague's private mail into
the feed or drop items to keep the pages aligned.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import AsyncClient

from app.db.models import (
    AnomalyCard,
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    Document,
    DocumentStatus,
    EmailThread,
    MailboxConfig,
)

OTHER_SUB = "colleague-sub"


@pytest_asyncio.fixture
async def feed_data(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            MailboxConfig(
                name="procurement",
                display_name="Закупки",
                imap_host="m.example.com",
                imap_port=993,
                imap_user="procurement",
                imap_password_encrypted="x",
                imap_ssl=True,
                is_active=True,
            ),
            MailboxConfig(
                name="colleague@example.com",
                display_name="Личный",
                imap_host="m.example.com",
                imap_port=993,
                imap_user="colleague",
                imap_password_encrypted="x",
                imap_ssl=True,
                is_active=True,
                mailbox_type="personal",
                owner_sub=OTHER_SUB,
            ),
        ]
    )
    db_session.add_all(
        [
            EmailThread(
                subject="Непрочитанное письмо",
                mailbox="procurement",
                message_count=1,
                is_read=False,
                folder="inbox",
                last_message_at=now - timedelta(minutes=5),
            ),
            EmailThread(
                subject="Уже прочитанное",
                mailbox="procurement",
                message_count=1,
                is_read=True,
                folder="inbox",
                last_message_at=now - timedelta(minutes=4),
            ),
            EmailThread(
                subject="Личное письмо коллеги",
                mailbox="colleague@example.com",
                message_count=1,
                is_read=False,
                folder="inbox",
                last_message_at=now - timedelta(minutes=3),
            ),
        ]
    )
    db_session.add(
        Document(
            file_name="Счёт.pdf",
            file_hash=uuid.uuid4().hex,
            mime_type="application/pdf",
            file_size=10,
            storage_path="documents/x",
            source_channel="email",
            status=DocumentStatus.needs_review,
        )
    )
    db_session.add(
        AnomalyCard(
            anomaly_type=AnomalyType.price_spike,
            severity=AnomalySeverity.critical,
            status=AnomalyStatus.open,
            entity_type="invoice",
            entity_id=uuid.uuid4(),
            title="Скачок цены на 40%",
        )
    )
    await db_session.commit()


async def test_feed_merges_all_three_sources(client: AsyncClient, feed_data):
    resp = await client.get("/api/inbox")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    kinds = {i["kind"] for i in body["items"]}
    assert kinds == {"email", "document", "anomaly"}
    assert body["counts"]["email"] >= 1


async def test_only_unread_mail_is_a_pending_item(client: AsyncClient, feed_data):
    """A read thread is not something waiting for you."""
    titles = [i["title"] for i in (await client.get("/api/inbox")).json()["items"]]
    assert "Непрочитанное письмо" in titles
    assert "Уже прочитанное" not in titles


async def test_a_colleagues_private_mail_never_enters_the_feed(client: AsyncClient, feed_data):
    titles = [i["title"] for i in (await client.get("/api/inbox")).json()["items"]]
    assert "Личное письмо коллеги" not in titles


async def test_a_critical_anomaly_outranks_newer_noise(client: AsyncClient, feed_data):
    """Sorted purely by clock, a price spike from this morning is buried under
    an unread newsletter from a minute ago."""
    items = (await client.get("/api/inbox")).json()["items"]
    assert items[0]["kind"] == "anomaly"
    assert items[0]["severity"] == "critical"


async def test_filtering_by_kind(client: AsyncClient, feed_data):
    items = (await client.get("/api/inbox?kinds=email")).json()["items"]
    assert items and {i["kind"] for i in items} == {"email"}


async def test_every_item_carries_a_destination(client: AsyncClient, feed_data):
    """A feed row that does not open anything is decoration."""
    for item in (await client.get("/api/inbox")).json()["items"]:
        assert item["url"].startswith("/")
        if item["kind"] == "email":
            assert item["url"].startswith("/email/")
        elif item["kind"] == "document":
            assert item["url"].endswith("/review")
