"""Ф9 — политика почты переопределяется на уровне ящика.

Одна глобальная тройка полей означала, что «автоответы для ящика рекламаций»
и «никаких автоответов из личной почты» — одна и та же настройка. NULL на
строке ящика = наследовать общую политику, поэтому у существующих ящиков
поведение не меняется.
"""

import uuid
from datetime import datetime, timedelta, timezone


def _mailbox(name: str, **kw):
    from app.db.models import MailboxConfig

    return MailboxConfig(
        name=name, imap_host="m.example.com", imap_port=993, imap_user=name,
        imap_password_encrypted="x", imap_ssl=True, is_active=True, **kw,
    )


def _message(mailbox: str):
    from app.db.models import EmailMessage

    return EmailMessage(
        mailbox=mailbox, subject="Запрос", from_address="client@example.com",
        to_addresses=[f"{mailbox}@example.com"], body_text="Пришлите счёт",
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )


async def test_mailbox_may_enable_auto_send_while_the_company_default_is_off(
    db_session,
):
    from app.db.models import MailServerConfig
    from app.domain.email_rules import _auto_send_allowed

    db_session.add(MailServerConfig(singleton_key="default", auto_send_enabled=False))
    db_session.add_all([
        _mailbox("claims", auto_send_enabled=True),
        _mailbox("inherits"),
    ])
    await db_session.commit()

    def check(mailbox: str) -> bool:
        return db_session.run_sync(
            lambda sync_db: _auto_send_allowed(sync_db, _message(mailbox))
        )

    assert await check("claims") is True
    # No opinion of its own → follows the company default, which is "off".
    assert await check("inherits") is False


async def test_mailbox_may_disable_auto_send_the_company_allows(db_session):
    from app.db.models import MailServerConfig
    from app.domain.email_rules import _auto_send_allowed

    db_session.add(MailServerConfig(singleton_key="default", auto_send_enabled=True))
    db_session.add(_mailbox("personal", auto_send_enabled=False))
    await db_session.commit()

    allowed = await db_session.run_sync(
        lambda sync_db: _auto_send_allowed(sync_db, _message("personal"))
    )
    assert allowed is False


async def test_per_mailbox_daily_cap_is_counted_per_mailbox(db_session):
    """A busy shared mailbox must not exhaust another mailbox's quota."""
    from app.db.models import EmailAutoReply, MailServerConfig
    from app.domain.email_rules import _auto_send_allowed

    db_session.add(MailServerConfig(
        singleton_key="default", auto_send_enabled=True, auto_send_max_per_day=100,
    ))
    db_session.add_all([
        _mailbox("capped", auto_send_enabled=True, auto_send_max_per_day=2),
        _mailbox("loud", auto_send_enabled=True),
    ])
    now = datetime.now(timezone.utc)
    for i in range(10):
        db_session.add(EmailAutoReply(
            mailbox="loud", recipient=f"someone{i}@example.com",
            sent_at=now - timedelta(minutes=i), thread_root=f"<root{i}@example.com>",
        ))
    await db_session.commit()

    async def check() -> bool:
        return await db_session.run_sync(
            lambda sync_db: _auto_send_allowed(sync_db, _message("capped"))
        )

    # Ten replies from the OTHER mailbox do not count against this one.
    assert await check() is True

    for i in range(2):
        db_session.add(EmailAutoReply(
            mailbox="capped", recipient=f"client{i}@example.com",
            sent_at=now - timedelta(minutes=i), thread_root=f"<c{i}@example.com>",
        ))
    await db_session.commit()
    assert await check() is False
