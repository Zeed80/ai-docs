"""Ф6.4 + Ф6.8 — the agent understands the LETTER, not just its attachments.

Attachment recognition (Ф6.1) already runs on its own. What was missing is the
layer above it: a counterparty asking for documents, a supplier's quote and a
newsletter are indistinguishable to a pipeline that only knows how to OCR a PDF.

Everything here is draft-first — the agent may label, link and notify; it may
not send. And it must be able to say what it did, or the panel in the thread is
worse than no panel.
"""

import uuid
from datetime import UTC, datetime

import pytest_asyncio

from app.db.models import (
    EmailMessage,
    EmailThread,
    EmailTriageResult,
    MailboxConfig,
    User,
)
from app.domain.email_triage import (
    CATEGORIES,
    TriageOutcome,
    _coerce,
    label_for,
    plan_actions,
)

# ── classification is defensive about what the model returns ───────────────


def test_unknown_category_becomes_other_without_inventing_confidence():
    """A label outside the taxonomy must not be coerced into "whatever looks
    closest" — that is how a newsletter becomes an invoice."""
    out = _coerce({"category": "супер-важное", "confidence": 0.99})
    assert out.category == "other"

    assert _coerce("не json вообще").category == "other"
    assert _coerce({}).confidence == 0.0
    assert _coerce({"category": "invoice", "confidence": "нет"}).confidence == 0.0
    assert _coerce({"category": "invoice", "confidence": 5}).confidence == 1.0


def test_every_category_has_a_human_label():
    for category in CATEGORIES:
        assert label_for(category) != category or category == "other"


# ── the plan is policy, not enthusiasm ─────────────────────────────────────


def test_classify_mode_does_nothing_but_label():
    perform, propose = plan_actions(
        TriageOutcome(category="invoice"),
        has_attachments=True,
        mode="classify",
    )
    assert [a["type"] for a in perform] == ["label"]
    assert propose == []


def test_document_request_only_proposes_a_reply_never_sends():
    perform, propose = plan_actions(
        TriageOutcome(
            category="document_request", entities={"requested_documents": ["акт сверки"]}
        ),
        has_attachments=False,
        mode="full",
    )
    kinds = {a["type"] for a in perform} | {a["type"] for a in propose}
    assert "draft_reply" in {a["type"] for a in propose}
    # Nothing in either list may put a message on the wire.
    assert not any(k in kinds for k in ("send", "send_reply", "forward_to"))


def test_invoice_without_attachment_asks_instead_of_pretending():
    perform, propose = plan_actions(
        TriageOutcome(category="invoice"),
        has_attachments=False,
        mode="full",
    )
    assert [a["type"] for a in perform] == ["label"]
    assert propose[0]["type"] == "ask_for_attachment"


def test_newsletter_is_labelled_and_left_alone():
    perform, propose = plan_actions(
        TriageOutcome(category="newsletter"),
        has_attachments=False,
        mode="full",
    )
    assert [a["type"] for a in perform] == ["label"]
    assert propose == []


# ── end to end through the task ────────────────────────────────────────────


@pytest_asyncio.fixture
async def letter(db_session):
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
            assigned_role="buyer",
            agent_triage_mode="full",
        )
    )
    db_session.add(
        User(sub="buyer-1", email="b@example.com", name="Закупщик", role="buyer", is_active=True)
    )
    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Счёт на оплату",
        from_address="sales@romex.example",
        to_addresses=["procurement@example.com"],
        body_text="Добрый день! Направляем счёт на оплату.",
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@romex.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.commit()
    return msg


async def test_thread_view_explains_what_the_agent_did(client, db_session, letter):
    """The panel renders from a stored row: performed and proposed stay apart,
    so it cannot claim work that never happened."""
    db_session.add(
        EmailTriageResult(
            message_id=letter.id,
            mailbox="procurement",
            category="invoice",
            confidence=0.93,
            summary="Поставщик прислал счёт на оплату",
            entities={"supplier_name": "ООО Ромекс"},
            performed=[{"type": "label", "category": "invoice", "label": "Счёт на оплату"}],
            proposed=[{"type": "ask_for_attachment", "hint": "Запросить вложение?"}],
            model_name="ollama/qwen3.5:9b",
            status="done",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/email/threads/{letter.thread_id}")
    assert resp.status_code == 200, resp.text
    triage = resp.json()["messages"][0]["triage"]
    assert triage["category"] == "invoice"
    assert triage["category_label"] == "Счёт на оплату"
    assert [a["type"] for a in triage["performed"]] == ["label"]
    assert [a["type"] for a in triage["proposed"]] == ["ask_for_attachment"]


async def test_correcting_the_category_is_recorded_as_a_lesson(client, db_session, letter):
    """Ф6.8 — a correction is the only reliable evidence the classifier was
    wrong; captured nowhere, the same mistake repeats forever."""
    from sqlalchemy import select

    from app.db.models import MemoryFact

    db_session.add(
        EmailTriageResult(
            message_id=letter.id,
            mailbox="procurement",
            category="newsletter",
            confidence=0.6,
            summary="Рассылка",
            model_name="ollama/qwen3.5:9b",
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/email/messages/{letter.id}/triage/correct",
        json={"category": "invoice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "invoice"
    assert resp.json()["corrected_category"] == "invoice"

    facts = (
        (
            await db_session.execute(
                select(MemoryFact).where(MemoryFact.source == "email_triage_correction")
            )
        )
        .scalars()
        .all()
    )
    assert len(facts) == 1
    assert facts[0].metadata_["model_category"] == "newsletter"
    assert facts[0].metadata_["human_category"] == "invoice"


async def test_unknown_correction_category_is_refused(client, db_session, letter):
    db_session.add(
        EmailTriageResult(
            message_id=letter.id,
            mailbox="procurement",
            category="other",
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/email/messages/{letter.id}/triage/correct",
        json={"category": "выдумка"},
    )
    assert resp.status_code == 422


def _reset_app_engine() -> None:
    """Сбросить кэш движка приложения (см. пояснение на месте вызова)."""
    from app.db.session import _get_engine, _get_session_factory

    _get_engine.cache_clear()
    _get_session_factory.cache_clear()


def test_triage_is_skipped_for_a_mailbox_with_the_mode_off(test_engine):
    """The mode gate must hold in the task itself, not only at dispatch:
    a queued job outliving a policy change must not read mail it may not read.

    Needs really-committed rows — the task opens its own session.
    """
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from app.tasks.email_triage import triage_message

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    ids: dict = {}
    try:
        with Session(engine) as db:
            db.add(
                MailboxConfig(
                    name="quiet-box",
                    display_name="Тихий",
                    imap_host="m.example.com",
                    imap_port=993,
                    imap_user="quiet-box",
                    imap_password_encrypted="x",
                    imap_ssl=True,
                    is_active=True,
                    agent_triage_mode="off",
                )
            )
            thread = EmailThread(subject="Письмо", mailbox="quiet-box", message_count=1)
            msg = EmailMessage(
                thread=thread,
                mailbox="quiet-box",
                subject="Письмо",
                from_address="x@y.example",
                to_addresses=["quiet-box@example.com"],
                body_text="текст",
                received_at=datetime.now(UTC),
                message_id_header=f"<{uuid.uuid4()}@y.example>",
            )
            db.add_all([thread, msg])
            db.commit()
            ids = {"msg": msg.id, "thread": thread.id}

        # Задача идёт через run_async — у него СВОЙ вечный цикл на процесс, а
        # пул asyncpg привязан к тому циклу, в котором соединение открылось
        # впервые. Если до этого теста БД трогали async-тесты, пул остался в
        # цикле pytest, и задача падала «Future attached to a different loop»
        # — по соседству, а не сама по себе. Сбрасываем кэш движка до и после,
        # чтобы каждый цикл работал со своими соединениями.
        _reset_app_engine()
        try:
            result = triage_message.apply(args=[str(ids["msg"])]).get()
        finally:
            _reset_app_engine()
        assert result["status"] == "skipped"
        assert result["reason"] == "triage_off"

        # And nothing was written about a mailbox the agent may not read.
        with Session(engine) as db:
            assert db.query(EmailTriageResult).filter_by(message_id=ids["msg"]).count() == 0
    finally:
        with Session(engine) as db:
            if ids:
                db.execute(
                    delete(EmailTriageResult).where(EmailTriageResult.message_id == ids["msg"])
                )
                db.execute(delete(EmailMessage).where(EmailMessage.id == ids["msg"]))
                db.execute(delete(EmailThread).where(EmailThread.id == ids["thread"]))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "quiet-box"))
            db.commit()
        engine.dispose()


# ── Ф6.7: one useful notification instead of two noisy ones ────────────────


def test_untriaged_letters_are_still_announced(test_engine, monkeypatch):
    """The plain "новое письмо" ping is suppressed when the agent is going to
    summarise the letter. If triage never happens — queue down, model
    unavailable — the letter must not arrive in total silence, which is a worse
    failure than a duplicate ping."""
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    import app.tasks.ingest as ingest

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    monkeypatch.setattr("app.db.sync_session.sync_session", lambda: Session(engine))

    announced: list = []
    monkeypatch.setattr(
        ingest,
        "_notify_new_email",
        lambda db, mailbox, msg: announced.append(msg.id),
    )

    ids = {}
    try:
        with Session(engine) as db:
            db.add(
                MailboxConfig(
                    name="quietbox",
                    display_name="Q",
                    imap_host="m.example.com",
                    imap_port=993,
                    imap_user="quietbox",
                    imap_password_encrypted="x",
                    imap_ssl=True,
                    is_active=True,
                )
            )
            thread = EmailThread(subject="Тихое", mailbox="quietbox", message_count=2)
            triaged_msg = EmailMessage(
                thread=thread,
                mailbox="quietbox",
                subject="Разобранное",
                from_address="a@b.example",
                to_addresses=["quietbox@example.com"],
                received_at=datetime.now(UTC),
                message_id_header=f"<{uuid.uuid4()}@b.example>",
            )
            silent_msg = EmailMessage(
                thread=thread,
                mailbox="quietbox",
                subject="Пропущенное",
                from_address="a@b.example",
                to_addresses=["quietbox@example.com"],
                received_at=datetime.now(UTC),
                message_id_header=f"<{uuid.uuid4()}@b.example>",
            )
            db.add_all([thread, triaged_msg, silent_msg])
            db.flush()
            db.add(
                EmailTriageResult(
                    message_id=triaged_msg.id,
                    mailbox="quietbox",
                    category="invoice",
                    status="done",
                )
            )
            db.commit()
            ids = {"thread": thread.id, "triaged": triaged_msg.id, "silent": silent_msg.id}

        result = ingest._notify_untriaged_after_delay.apply(
            args=["quietbox", [str(ids["triaged"]), str(ids["silent"])]]
        ).get()

        assert result["notified"] == 1
        # Only the one the agent never reported on.
        assert announced == [ids["silent"]]
    finally:
        with Session(engine) as db:
            if ids:
                db.execute(
                    delete(EmailTriageResult).where(
                        EmailTriageResult.message_id.in_([ids["triaged"], ids["silent"]])
                    )
                )
                db.execute(
                    delete(EmailMessage).where(EmailMessage.id.in_([ids["triaged"], ids["silent"]]))
                )
                db.execute(delete(EmailThread).where(EmailThread.id == ids["thread"]))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "quietbox"))
            db.commit()
        engine.dispose()
