"""Ф8 — the mail subsystem must be observable.

Every failure mode here is silent by nature: the poller stops keeping up, the
write-back queue backs up, the agent mislabels letters. None of it raises; mail
just quietly stops arriving or arrives unclassified. Declaring metrics is not
enough — a metric nobody increments is the same dead configuration as a cron
trigger nobody reads.
"""

import uuid
from datetime import UTC, datetime

import pytest

pytest.importorskip("prometheus_client")


def _sample(metric, **labels) -> float:
    """Current value of one labelled counter/gauge."""
    from prometheus_client import REGISTRY

    name = metric._name
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name not in (f"{name}_total", name):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def test_email_metrics_are_registered():
    from app.core import metrics

    for name in (
        "email_messages_ingested_total",
        "email_sent_total",
        "email_sync_ops_pending",
        "email_sync_ops_total",
        "email_imap_errors_total",
        "email_triage_total",
        "email_triage_corrections_total",
        "email_rule_actions_total",
    ):
        assert hasattr(metrics, name), name


def test_rule_actions_are_counted(test_engine, monkeypatch):
    """A rule that fires must show up in the metric — otherwise "правило
    работает?" is still unanswerable from outside."""
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from app.core.metrics import email_rule_actions_total
    from app.db.models import EmailMessage, EmailRule, EmailThread, MailboxConfig
    from app.domain.email_rules import apply_rules

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    monkeypatch.setattr("app.db.sync_session.sync_session", lambda: Session(engine))

    before = _sample(email_rule_actions_total, action="star")
    try:
        with Session(engine) as db:
            db.add(
                MailboxConfig(
                    name="metricbox",
                    display_name="M",
                    imap_host="m.example.com",
                    imap_port=993,
                    imap_user="metricbox",
                    imap_password_encrypted="x",
                    imap_ssl=True,
                    is_active=True,
                )
            )
            db.add(
                EmailRule(
                    name="Пометить",
                    mailbox="metricbox",
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
            thread = EmailThread(subject="Счёт", mailbox="metricbox", message_count=1)
            msg = EmailMessage(
                thread=thread,
                mailbox="metricbox",
                subject="Счёт на оплату",
                from_address="x@y.example",
                to_addresses=["metricbox@example.com"],
                body_text="текст",
                received_at=datetime.now(UTC),
                message_id_header=f"<{uuid.uuid4()}@y.example>",
            )
            db.add_all([thread, msg])
            db.commit()

            applied = apply_rules(db, msg, "metricbox")
            db.commit()

        assert [a["type"] for a in applied] == ["star"]
        assert _sample(email_rule_actions_total, action="star") == before + 1
    finally:
        with Session(engine) as db:
            db.execute(delete(EmailMessage).where(EmailMessage.mailbox == "metricbox"))
            db.execute(delete(EmailThread).where(EmailThread.mailbox == "metricbox"))
            db.execute(delete(EmailRule).where(EmailRule.mailbox == "metricbox"))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "metricbox"))
            db.commit()
        engine.dispose()


async def test_correcting_a_category_moves_the_quality_metric(client, db_session):
    """The honest quality signal: how often a human disagreed with the agent."""
    from app.core.metrics import email_triage_corrections_total
    from app.db.models import (
        EmailMessage,
        EmailThread,
        EmailTriageResult,
        MailboxConfig,
    )

    db_session.add(
        MailboxConfig(
            name="qual",
            display_name="Q",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="qual",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    thread = EmailThread(subject="Письмо", mailbox="qual", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="qual",
        subject="Письмо",
        from_address="a@b.example",
        to_addresses=["qual@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@b.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    db_session.add(
        EmailTriageResult(
            message_id=msg.id,
            mailbox="qual",
            category="newsletter",
            status="done",
        )
    )
    await db_session.commit()

    before = _sample(email_triage_corrections_total, was="newsletter", now="invoice")
    resp = await client.post(
        f"/api/email/messages/{msg.id}/triage/correct",
        json={"category": "invoice"},
    )
    assert resp.status_code == 200, resp.text
    assert _sample(email_triage_corrections_total, was="newsletter", now="invoice") == before + 1


async def test_health_screen_surfaces_what_is_quietly_broken(client, db_session):
    """Sync errors, a backed-up write-back queue and attachments pointing at
    bytes that were never written are all invisible failures — this is where
    they become visible."""
    from app.db.models import (
        EmailAttachment,
        EmailMessage,
        EmailSyncOp,
        EmailThread,
        MailboxConfig,
        MailboxFolder,
    )

    db_session.add(
        MailboxConfig(
            name="healthbox",
            display_name="Здоровье",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="healthbox",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            sync_error="IMAP: connection refused",
        )
    )
    db_session.add(
        MailboxFolder(
            mailbox="healthbox",
            remote_name="INBOX",
            local_folder="inbox",
            sync_enabled=True,
            uid_validity=777,
            sync_error=None,
        )
    )
    thread = EmailThread(subject="Письмо", mailbox="healthbox", message_count=1, is_read=False)
    msg = EmailMessage(
        thread=thread,
        mailbox="healthbox",
        subject="Письмо",
        from_address="a@b.example",
        to_addresses=["healthbox@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@b.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    # An attachment whose bytes never made it to storage.
    db_session.add(
        EmailAttachment(
            message_id=msg.id,
            filename="потерянное.pdf",
            content_type="application/pdf",
            size=10,
            storage_path=None,
            sha256="f" * 64,
        )
    )
    db_session.add_all(
        [
            EmailSyncOp(message_id=msg.id, mailbox="healthbox", op="seen", state="pending"),
            EmailSyncOp(message_id=msg.id, mailbox="healthbox", op="move", state="failed"),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/mailbox/health")
    assert resp.status_code == 200, resp.text
    box = next(b for b in resp.json() if b["name"] == "healthbox")

    assert box["sync_error"] == "IMAP: connection refused"
    assert box["unread"] == 1
    assert box["attachments_without_bytes"] == 1
    assert box["pending_sync_ops"] == 1
    assert box["failed_sync_ops"] == 1
    assert box["folders"][0]["uid_validity"] == 777


async def test_mailbox_counts_are_cached_and_invalidated(client, db_session):
    """A stale sidebar is worse than a slow one: the cache must die the moment
    somebody reads a thread."""
    from app.db.models import EmailMessage, EmailThread, MailboxConfig
    from app.domain.email_counts import invalidate_mailbox_counts, mailbox_counts

    db_session.add(
        MailboxConfig(
            name="countbox",
            display_name="Счётчики",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="countbox",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    thread = EmailThread(
        subject="T", mailbox="countbox", message_count=1, is_read=False, folder="inbox"
    )
    db_session.add(thread)
    await db_session.flush()
    db_session.add(
        EmailMessage(
            thread_id=thread.id,
            mailbox="countbox",
            subject="T",
            from_address="a@b.example",
            to_addresses=["countbox@example.com"],
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@b.example>",
        )
    )
    await db_session.commit()
    await invalidate_mailbox_counts()

    first = (await mailbox_counts(db_session, ["countbox"])).for_mailbox("countbox")
    assert first == {"messages": 1, "threads": 1, "unread": 1}

    # Change the data behind the cache's back.
    thread.is_read = True
    await db_session.commit()

    # Served from cache (or recomputed if Redis is unavailable) — either way it
    # must not crash, and after an explicit invalidate it must be correct.
    await mailbox_counts(db_session, ["countbox"])
    await invalidate_mailbox_counts()
    assert (await mailbox_counts(db_session, ["countbox"])).for_mailbox("countbox")["unread"] == 0


async def test_body_retention_removes_content_but_keeps_the_record(db_session):
    """Retention must delete what is private (the body), not the fact that a
    letter existed — and must not touch a mailbox that never asked for it."""
    from datetime import timedelta

    from app.db.models import EmailMessage, EmailThread, MailboxConfig
    from app.tasks.email_triage import prune_bodies_for

    def _mailbox(name: str, days: int) -> MailboxConfig:
        return MailboxConfig(
            name=name,
            imap_host="m.example.com",
            imap_port=993,
            imap_user=name,
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            body_retention_days=days,
        )

    db_session.add_all([_mailbox("shortbox", 30), _mailbox("foreverbox", 0)])
    old = datetime.now(UTC) - timedelta(days=90)
    ids = {}
    for name in ("shortbox", "foreverbox"):
        thread = EmailThread(subject="Старое", mailbox=name, message_count=1)
        db_session.add(thread)
        await db_session.flush()
        msg = EmailMessage(
            thread_id=thread.id,
            mailbox=name,
            subject="Старое письмо",
            from_address="a@b.example",
            to_addresses=[f"{name}@example.com"],
            body_text="Коммерческая тайна",
            body_html="<p>Коммерческая тайна</p>",
            snippet="Коммерческая тайна",
            received_at=old,
            message_id_header=f"<{uuid.uuid4()}@b.example>",
        )
        db_session.add(msg)
        await db_session.flush()
        ids[name] = msg.id
    await db_session.commit()

    pruned = await db_session.run_sync(lambda sync_db: prune_bodies_for(sync_db))
    await db_session.commit()
    assert pruned == {"shortbox": 1}

    for name, expect_pruned in (("shortbox", True), ("foreverbox", False)):
        msg = await db_session.get(EmailMessage, ids[name])
        await db_session.refresh(msg)
        if expect_pruned:
            assert msg.body_text is None and msg.body_html is None
            assert msg.body_pruned_at is not None
            # The letter is still findable and still explains itself.
            assert msg.subject == "Старое письмо"
            assert msg.from_address == "a@b.example"
            assert msg.snippet == "Коммерческая тайна"
        else:
            assert msg.body_text == "Коммерческая тайна"
            assert msg.body_pruned_at is None
