"""P2: server-side filter rules on inbound mail."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import (
    DraftAction,
    EmailAttachment,
    EmailLabel,
    EmailMessage,
    EmailRule,
    EmailRuleLog,
    EmailTemplateDB,
    EmailThread,
    EmailThreadLabel,
    MailboxConfig,
)


class FakeMsg:
    def __init__(self, **kw):
        self.from_address = kw.get("from_address", "a@b.ru")
        self.to_addresses = kw.get("to_addresses", [])
        self.cc_addresses = kw.get("cc_addresses", [])
        self.subject = kw.get("subject", "")
        self.body_text = kw.get("body_text", "")
        self.mailbox = kw.get("mailbox", "procurement")


def test_evaluate_conditions_all_any_regex():
    from app.domain.email_rules import evaluate_conditions

    msg = FakeMsg(from_address="sales@acme-supply.ru", subject="Счёт №42 на оплату")
    cond_all = {
        "match": "all",
        "rules": [
            {"field": "from", "op": "ends_with", "value": "acme-supply.ru"},
            {"field": "subject", "op": "contains", "value": "счёт"},
        ],
    }
    assert evaluate_conditions(msg, [], cond_all, known_supplier_domains=set()) is True

    cond_regex = {
        "match": "any",
        "rules": [{"field": "subject", "op": "matches_regex", "value": r"№\s*\d+"}],
    }
    assert evaluate_conditions(msg, [], cond_regex, known_supplier_domains=set()) is True

    cond_supplier = {
        "match": "all",
        "rules": [{"field": "is_from_known_supplier", "op": "is_true"}],
    }
    assert (
        evaluate_conditions(msg, [], cond_supplier, known_supplier_domains={"acme-supply.ru"})
        is True
    )
    assert evaluate_conditions(msg, [], cond_supplier, known_supplier_domains=set()) is False


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
        EmailThreadLabel,
        EmailRule,
        EmailLabel,
        EmailAttachment,
        DraftAction,
        EmailMessage,
        EmailThread,
        EmailTemplateDB,
        MailboxConfig,
    )

    def _wipe():
        with Session(engine) as db:
            for t in tables:
                db.execute(delete(t))
            db.commit()

    _wipe()
    try:
        yield engine
    finally:
        _wipe()
        engine.dispose()


def test_apply_rules_adds_label_and_stops(sync_db):
    from app.domain.email_rules import apply_rules

    with Session(sync_db) as db:
        label = EmailLabel(name="Счета", owner_sub=None, is_system=False)
        db.add(label)
        db.flush()
        th = EmailThread(
            subject="Счёт",
            mailbox="procurement",
            message_count=1,
            last_message_at=datetime.now(UTC),
        )
        db.add(th)
        db.flush()
        msg = EmailMessage(
            thread_id=th.id,
            mailbox="procurement",
            from_address="sales@acme.ru",
            subject="Счёт на оплату №7",
            body_text="текст",
            is_inbound=True,
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@acme.ru>",
        )
        db.add(msg)
        db.add(
            EmailRule(
                name="Счета от acme",
                mailbox=None,
                owner_sub=None,
                is_active=True,
                priority=10,
                stop_processing=True,
                conditions={
                    "match": "all",
                    "rules": [
                        {"field": "from", "op": "contains", "value": "acme.ru"},
                        {"field": "subject", "op": "contains", "value": "счёт"},
                    ],
                },
                actions=[{"type": "add_label", "label_id": str(label.id)}, {"type": "mark_read"}],
            )
        )
        db.add(
            EmailRule(
                name="never runs",
                mailbox=None,
                owner_sub=None,
                is_active=True,
                priority=20,
                conditions={
                    "match": "all",
                    "rules": [{"field": "subject", "op": "contains", "value": "zzz"}],
                },
                actions=[{"type": "star"}],
            )
        )
        db.commit()

        applied = apply_rules(db, msg, "procurement")
        db.commit()

        assert any(a["type"] == "add_label" for a in applied)
        assert msg.is_read is True
        link = (
            db.query(EmailThreadLabel).filter_by(thread_id=th.id, label_id=label.id).one_or_none()
        )
        assert link is not None and link.added_by.startswith("rule:")


async def test_rules_crud_and_dry_run(client: AsyncClient, db_session):
    db_session.add(
        MailboxConfig(
            name="procurement",
            imap_host="h",
            imap_port=993,
            imap_user="p",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            mailbox_type="shared",
        )
    )
    th = EmailThread(
        subject="Рекламация по браку",
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
            from_address="qa@partner.ru",
            subject="Рекламация по браку партии",
            body_text="брак",
            is_inbound=True,
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@p.ru>",
        )
    )
    await db_session.commit()

    r = await client.post(
        "/api/email/rules",
        json={
            "name": "Рекламации",
            "mailbox": "procurement",
            "conditions": {
                "match": "all",
                "rules": [{"field": "subject", "op": "contains", "value": "рекламац"}],
            },
            "actions": [{"type": "star"}],
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    r = await client.get("/api/email/rules")
    assert any(x["id"] == rid for x in r.json())

    r = await client.post(f"/api/email/rules/{rid}/test", json={"last_n": 20})
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 1

    r = await client.delete(f"/api/email/rules/{rid}")
    assert r.status_code == 204


def test_auto_reply_template_creates_draft_never_sends(sync_db):
    from app.domain.email_rules import apply_rules

    with Session(sync_db) as db:
        tpl = EmailTemplateDB(
            name="Автоответ",
            slug=f"auto-{uuid.uuid4().hex[:8]}",
            subject="Ваше обращение получено",
            body_html="<p>Спасибо, мы свяжемся с вами.</p>",
            body_text="Спасибо.",
            is_builtin=False,
        )
        db.add(tpl)
        th = EmailThread(
            subject="Вопрос по поставке",
            mailbox="procurement",
            message_count=1,
            last_message_at=datetime.now(UTC),
        )
        db.add(th)
        db.flush()
        msg = EmailMessage(
            thread_id=th.id,
            mailbox="procurement",
            from_address="client@buyer.ru",
            subject="Вопрос по поставке",
            body_text="когда отгрузка?",
            is_inbound=True,
            received_at=datetime.now(UTC),
            message_id_header=f"<{uuid.uuid4()}@b.ru>",
        )
        db.add(msg)
        db.add(
            EmailRule(
                name="Автоответ на вопросы",
                mailbox=None,
                owner_sub=None,
                is_active=True,
                priority=5,
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

        drafts = db.query(DraftAction).filter_by(action_type="email.send").all()
        assert len(drafts) == 1
        d = drafts[0].draft_data
        assert d["status"] == "draft"  # NEVER auto-sent
        assert drafts[0].executed is False
        assert d["to_addresses"] == ["client@buyer.ru"]
        assert d["created_by"].startswith("rule:")
