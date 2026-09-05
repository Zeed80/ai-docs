"""Ф4 — the human send path gets the same guarantees as the agent's.

Before this, `POST /api/email/send` stamped the draft "approved" and dispatched
it: the path people use had no risk checks at all, while the gated agent path
had every one of them. And `send_email` blocked only flags that were BOTH
severity="error" AND can_override=False — a combination nothing produced, so
the single error-level detector could not stop anything.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.db.models import DraftAction, MailboxConfig, Party, PartyRole


@pytest_asyncio.fixture
async def mailbox(db_session):
    db_session.add(
        MailboxConfig(
            name="procurement",
            display_name="Закупки",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="procurement",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            smtp_from_address="zakupki@ourfirm.example",
        )
    )
    await db_session.commit()


@pytest.fixture(autouse=True)
def _no_real_send(monkeypatch):
    """Capture dispatch instead of talking to Celery/SMTP."""
    import app.tasks.email_sender as sender

    calls: list[dict] = []

    class _Task:
        id = "task-123"

    def _delay(*args, **kwargs):
        calls.append({"args": args, "countdown": None})
        return _Task()

    def _apply_async(args=None, countdown=None, **kwargs):
        calls.append({"args": tuple(args or ()), "countdown": countdown})
        return _Task()

    monkeypatch.setattr(sender.send_email_draft, "delay", _delay)
    monkeypatch.setattr(sender.send_email_draft, "apply_async", _apply_async)
    return calls


def _send_body(**over):
    body = {
        "mailbox": "procurement",
        "to_addresses": ["sales@romex.example"],
        "subject": "Запрос",
        "body_html": "<p>Добрый день</p>",
        "body_text": "Добрый день",
    }
    body.update(over)
    return body


async def test_sensitive_content_blocks_a_human_send(client: AsyncClient, mailbox, _no_real_send):
    resp = await client.post(
        "/api/email/send",
        json=_send_body(
            body_text="Это конфиденциально, никому не пересылайте",
            body_html="<p>Это конфиденциально</p>",
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert [f["code"] for f in body["blocked_by"]] == ["sensitive_content"]
    assert _no_real_send == []  # nothing was dispatched


async def test_the_person_can_override_and_the_override_is_audited(
    client: AsyncClient, db_session, mailbox, _no_real_send
):
    from sqlalchemy import select

    from app.db.models import AuditLog

    resp = await client.post(
        "/api/email/send",
        json=_send_body(
            body_text="Это конфиденциально",
            acknowledged_risks=["sensitive_content"],
        ),
    )
    assert resp.json()["status"] == "queued"
    assert len(_no_real_send) == 1

    actions = (
        (
            await db_session.execute(
                select(AuditLog.action).where(AuditLog.action == "email.risk_override")
            )
        )
        .scalars()
        .all()
    )
    assert actions, "решение отправить вопреки риску должно попадать в аудит"


async def test_lookalike_domain_is_caught(client: AsyncClient, db_session, mailbox, _no_real_send):
    """rornex.example vs romex.example — invisible at a glance, and exactly how
    invoice-redirection fraud lands."""
    db_session.add(
        Party(name="ООО Ромекс", role=PartyRole.supplier, contact_email="sales@romex.example")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/email/send", json=_send_body(to_addresses=["sales@rornex.example"])
    )
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["blocked_by"][0]["code"] == "lookalike_domain"
    assert "romex.example" in body["blocked_by"][0]["message"]


async def test_promised_attachment_is_a_warning_not_a_wall(
    client: AsyncClient, mailbox, _no_real_send
):
    resp = await client.post(
        "/api/email/send",
        json=_send_body(body_text="Во вложении счёт на оплату", attachment_ids=[]),
    )
    body = resp.json()
    assert body["status"] == "queued"  # advisory, not blocking
    codes = {w["code"] for w in body["warnings"]}
    assert "promised_attachment_missing" in codes


async def test_undo_window_delays_the_dispatch(client: AsyncClient, mailbox, _no_real_send):
    resp = await client.post("/api/email/send", json=_send_body(delay_seconds=10))
    body = resp.json()
    assert body["undo_seconds"] == 10
    assert _no_real_send[0]["countdown"] == 10


async def test_undo_window_is_clamped(client: AsyncClient, mailbox, _no_real_send):
    resp = await client.post("/api/email/send", json=_send_body(delay_seconds=99999))
    assert resp.json()["undo_seconds"] == 30


async def test_scheduled_send_accepts_a_future_time(client: AsyncClient, mailbox, _no_real_send):
    when = datetime.now(UTC) + timedelta(hours=2)
    resp = await client.post("/api/email/send", json=_send_body(send_at=when.isoformat()))
    assert resp.json()["status"] == "queued"
    assert 7000 < _no_real_send[0]["countdown"] <= 7200


async def test_cancel_stops_a_queued_send(client: AsyncClient, db_session, mailbox, _no_real_send):
    resp = await client.post("/api/email/send", json=_send_body(delay_seconds=30))
    draft_id = resp.json()["draft_id"]

    cancelled = await client.post(f"/api/email/drafts/{draft_id}/cancel-send")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    draft = await db_session.get(DraftAction, uuid.UUID(draft_id))
    await db_session.refresh(draft)
    assert draft.draft_data["cancelled"] is True
    assert draft.draft_data["status"] == "draft"


def test_the_worker_honours_the_cancel_flag(test_engine):
    """The flag is checked inside the row lock the task already takes, so
    "отменено" and "уже ушло" can never both be true.

    Needs a really-committed row: the task opens its own session, which cannot
    see the suite's per-test transaction.
    """
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from app.tasks.email_sender import send_email_draft

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    draft_id = None
    try:
        with Session(engine) as db:
            draft = DraftAction(
                action_type="email.send",
                entity_type="email",
                draft_data={
                    "to_addresses": ["x@example.com"],
                    "subject": "s",
                    "body_text": "t",
                    "status": "queued",
                    "cancelled": True,
                },
            )
            db.add(draft)
            db.commit()
            draft_id = draft.id

        result = send_email_draft.apply(args=[str(draft_id)]).get()
        assert result["status"] == "cancelled"
    finally:
        if draft_id is not None:
            with Session(engine) as db:
                db.execute(delete(DraftAction).where(DraftAction.id == draft_id))
                db.commit()
        engine.dispose()


async def test_cancelling_an_already_sent_message_is_refused(
    client: AsyncClient, db_session, mailbox
):
    draft = DraftAction(
        action_type="email.send",
        entity_type="email",
        executed=True,
        draft_data={
            "to_addresses": ["x@example.com"],
            "subject": "s",
            "status": "sent",
            "mailbox": "procurement",
        },
    )
    db_session.add(draft)
    await db_session.commit()

    resp = await client.post(f"/api/email/drafts/{draft.id}/cancel-send")
    assert resp.status_code == 409


# ── Ф5.1: pagination ────────────────────────────────────────────────────────


async def test_thread_list_paginates_and_reaches_older_mail(
    client: AsyncClient, db_session, mailbox
):
    """A conversation older than the first page used to be unreachable: the
    endpoint returned a capped list and the client had no way to ask for more."""
    from app.db.models import EmailMessage, EmailThread

    base = datetime.now(UTC)
    for i in range(7):
        thread = EmailThread(
            subject=f"Письмо {i}",
            mailbox="procurement",
            message_count=1,
            last_message_at=base - timedelta(hours=i),
            folder="inbox",
        )
        db_session.add(thread)
        await db_session.flush()
        db_session.add(
            EmailMessage(
                thread_id=thread.id,
                mailbox="procurement",
                subject=f"Письмо {i}",
                from_address="x@y.example",
                to_addresses=["procurement@example.com"],
                received_at=base - timedelta(hours=i),
                message_id_header=f"<page-{i}-{uuid.uuid4()}@y.example>",
            )
        )
    await db_session.commit()

    first = await client.get("/api/email/threads?limit=3")
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"]

    seen = [t["subject"] for t in body["items"]]
    cursor = body["next_cursor"]
    for _ in range(5):
        if not cursor:
            break
        page = (await client.get(f"/api/email/threads?limit=3&cursor={cursor}")).json()
        seen += [t["subject"] for t in page["items"]]
        cursor = page["next_cursor"]

    # Every letter is reachable, and no page repeats a thread.
    assert len(seen) == len(set(seen))
    assert "Письмо 6" in seen


async def test_search_pagination_was_declared_and_now_works(
    client: AsyncClient, db_session, mailbox
):
    """`cursor`/`next_cursor` sat in EmailSearchRequest/Response unimplemented,
    so search could only ever return its first page."""
    from app.db.models import EmailMessage, EmailThread

    thread = EmailThread(subject="Поиск", mailbox="procurement", message_count=5)
    db_session.add(thread)
    await db_session.flush()
    for i in range(5):
        db_session.add(
            EmailMessage(
                thread_id=thread.id,
                mailbox="procurement",
                subject=f"Уникальное слово {i}",
                from_address="x@y.example",
                to_addresses=["procurement@example.com"],
                body_text="ключевоеслово тест",
                received_at=datetime.now(UTC),
                message_id_header=f"<s-{i}-{uuid.uuid4()}@y.example>",
            )
        )
    await db_session.commit()

    first = (
        await client.post("/api/email/search", json={"query": "ключевоеслово", "limit": 2})
    ).json()
    assert len(first["results"]) == 2
    assert first["total"] >= 5
    assert first["next_cursor"] == "2"

    second = (
        await client.post(
            "/api/email/search",
            json={"query": "ключевоеслово", "limit": 2, "cursor": first["next_cursor"]},
        )
    ).json()
    ids = {m["id"] for m in first["results"]} & {m["id"] for m in second["results"]}
    assert not ids, "страницы не должны пересекаться"


# ── Ф2.4 + a hazard it exposed ─────────────────────────────────────────────


def test_a_failure_after_delivery_never_causes_a_second_send(test_engine, monkeypatch):
    """The letter is on the wire; nothing after that may roll back "sent".

    Found for real: an un-awaited coroutine aborted the flush AFTER a
    successful SMTP delivery, so `executed` reverted to False and the retry
    would have delivered the very same message a second time.
    """
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    import app.tasks.email_sender as sender
    from app.db.models import DraftAction as DA
    from app.db.models import MailboxConfig as MC

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)

    sent_calls: list = []

    class _SMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *a):
            pass

        def sendmail(self, *a):
            sent_calls.append(a)

    monkeypatch.setattr(sender.smtplib, "SMTP", _SMTP)

    async def _boom(*a, **k):
        raise RuntimeError("Sent folder unavailable")

    monkeypatch.setattr(sender, "_append_to_sent", _boom)

    draft_id = None
    try:
        with Session(engine) as db:
            db.add(
                MC(
                    name="sendbox",
                    display_name="Send",
                    imap_host="m.example.com",
                    imap_port=993,
                    imap_user="sendbox",
                    imap_password_encrypted="x",
                    imap_ssl=True,
                    is_active=True,
                    smtp_host="smtp.example.com",
                    smtp_port=587,
                    smtp_user="sendbox",
                    smtp_password_encrypted="x",
                    smtp_use_tls=True,
                    smtp_from_address="sendbox@example.com",
                )
            )
            draft = DA(
                action_type="email.send",
                entity_type="email",
                draft_data={
                    "to_addresses": ["x@example.com"],
                    "subject": "s",
                    "body_text": "t",
                    "mailbox": "sendbox",
                    "status": "approved",
                    "attachment_ids": [],
                },
            )
            db.add(draft)
            db.commit()
            draft_id = draft.id

        result = sender.send_email_draft.apply(args=[str(draft_id)]).get()
        assert result["status"] == "sent"
        assert len(sent_calls) == 1

        with Session(engine) as db:
            draft = db.get(DA, draft_id)
            # "Sent" survived the later failure…
            assert draft.executed is True
            assert draft.draft_data["status"] == "sent"
            # …and the failure is recorded rather than hidden.
            assert "Sent folder unavailable" in draft.draft_data["sent_folder_error"]

        # A retry must be a no-op, not a second delivery.
        again = sender.send_email_draft.apply(args=[str(draft_id)]).get()
        assert again["status"] == "already_sent"
        assert len(sent_calls) == 1
    finally:
        # The send mirrors itself into a thread via record_outbound_message;
        # leaving those rows behind leaks into every later test that counts
        # messages (it broke test_spec_tables once).
        from app.db.models import EmailMessage as EM
        from app.db.models import EmailThread as ET

        with Session(engine) as db:
            db.execute(delete(EM).where(EM.mailbox == "sendbox"))
            db.execute(delete(ET).where(ET.mailbox == "sendbox"))
            if draft_id:
                db.execute(delete(DA).where(DA.id == draft_id))
            db.execute(delete(MC).where(MC.name == "sendbox"))
            db.commit()
        engine.dispose()
