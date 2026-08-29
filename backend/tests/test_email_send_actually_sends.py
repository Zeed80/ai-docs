"""Отправка должна отправлять — или честно говорить, что не отправила.

Живой случай: агент сказал «Готово ✅ Письмо отправлено», человек шесть раз
нажал «Утверждено», а письмо осталось в черновиках. Три отдельных дефекта
сложились в одну картину.
"""

import json
import uuid

import pytest


async def test_a_draft_without_a_mailbox_gets_the_only_sending_one(client, db_session):
    """Черновик без ящика не имеет SMTP-аккаунта, и отправка сваливалась в
    глобальный .env, которого в этой установке нет. Агент ящик не указывает."""
    from app.db.models import DraftAction, MailboxConfig

    db_session.add(MailboxConfig(
        name="sender-box", imap_host="imap.example.com", imap_port=993,
        imap_user="box", imap_password_encrypted="x", imap_ssl=True,
        smtp_host="smtp.example.com", smtp_port=587, smtp_user="box@example.com",
        smtp_password_encrypted="y", is_active=True,
    ))
    await db_session.commit()

    resp = await client.post("/api/email/drafts", json={
        "to_addresses": ["supplier@example.com"],
        "subject": "Запрос каталога",
        "body_html": "<p>Добрый день!</p>",
    })
    assert resp.status_code == 200, resp.text

    draft = await db_session.get(DraftAction, uuid.UUID(resp.json()["id"]))
    await db_session.refresh(draft)
    assert draft.draft_data["mailbox"] == "sender-box"


async def test_no_mailbox_is_chosen_when_several_could_send(client, db_session):
    """Отправить от чужого имени хуже, чем не отправить: при нескольких
    кандидатах ящик не угадывается."""
    from app.db.models import DraftAction, MailboxConfig

    for name in ("box-a", "box-b"):
        db_session.add(MailboxConfig(
            name=name, imap_host="imap.example.com", imap_port=993, imap_user=name,
            imap_password_encrypted="x", imap_ssl=True, smtp_host="smtp.example.com",
            smtp_port=587, smtp_user=f"{name}@example.com",
            smtp_password_encrypted="y", is_active=True,
        ))
    await db_session.commit()

    resp = await client.post("/api/email/drafts", json={
        "to_addresses": ["supplier@example.com"], "subject": "Тема",
        "body_html": "<p>т</p>",
    })
    draft = await db_session.get(DraftAction, uuid.UUID(resp.json()["id"]))
    await db_session.refresh(draft)
    assert draft.draft_data["mailbox"] is None


def test_production_never_reports_a_mock_send_as_success(monkeypatch):
    """Самое дорогое из трёх: при ненастроенном SMTP задача помечала черновик
    executed и возвращала успех. Человеку сообщали, что письмо ушло."""
    import inspect

    from app.tasks import email_sender

    src = inspect.getsource(email_sender.send_email_draft)
    branch = src.split('if not smtp_host:')[-1].split("logger.warning")[0]
    assert 'settings.app_env == "production"' in branch, (
        "в проде мнимая отправка должна быть ошибкой, а не успехом"
    )
    assert '"reason": "smtp_not_configured"' in branch


class _Loop:
    """Минимальный носитель логики ключа одобрения."""

    from app.ai.agent_loop import AgentSession

    _approval_key = AgentSession._approval_key


def test_the_same_send_is_approved_once_per_turn():
    """Модель, не поверив успешному ответу, переспрашивала отправку одного и
    того же черновика шестью наборами аргументов — и человек шесть раз
    подтверждал одно письмо."""
    key = _Loop._approval_key

    base = {"action": "send", "draft_id": "fe1aa152-0000-4000-8000-000000000001"}
    # Те же аргументы плюс мусор, который модель добавляла от неуверенности.
    noisy = {**base, "body": json.dumps({"draft_id": base["draft_id"]})}
    verbose = {**base, "body": json.dumps({
        "draft_id": base["draft_id"], "to_addresses": ["a@b.example"],
        "subject": "Запрос каталога",
    })}
    assert key("email", base) == key("email", noisy) == key("email", verbose)


def test_a_different_letter_still_asks_again():
    """Ключ описывает предмет действия: другое письмо — другой вопрос."""
    key = _Loop._approval_key

    a = {"action": "send", "draft_id": "aaaaaaaa-0000-4000-8000-000000000001"}
    b = {"action": "send", "draft_id": "bbbbbbbb-0000-4000-8000-000000000002"}
    assert key("email", a) != key("email", b)

    # Без draft_id ключ строится по получателю и теме.
    c = {"action": "send", "body": json.dumps(
        {"to_addresses": ["x@example.com"], "subject": "Тема"})}
    d = {"action": "send", "body": json.dumps(
        {"to_addresses": ["другой@example.com"], "subject": "Тема"})}
    assert key("email", c) != key("email", d)


def test_approval_memory_is_cleared_between_turns():
    """Одобрение действует на ход. Прошлое «да» не покрывает новое письмо."""
    import inspect

    from app.ai.agent_loop import AgentSession

    src = inspect.getsource(AgentSession.on_user_message)
    assert "_granted_approvals.clear()" in src
