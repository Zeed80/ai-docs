"""email.send сам прогоняет проверку рисков.

Раньше send просто отказывал («Risk check required before sending»), и агент
узнавал о нужном порядке действий из ошибки — лишний круг на каждой отправке,
видно в живом прогоне. Теперь проверка выполняется на месте.

Проверяется в первую очередь НЕ удобство, а то, что проверка не пропущена и её
вердикт по-прежнему решает судьбу письма.
"""

import uuid

import pytest


@pytest.fixture
def _no_real_send(monkeypatch):
    """Перехватить постановку в очередь вместо реального Celery/SMTP."""
    import app.tasks.email_sender as sender

    class _Task:
        id = "task-123"

    monkeypatch.setattr(sender.send_email_draft, "delay", lambda *a, **k: _Task())
    monkeypatch.setattr(sender.send_email_draft, "apply_async", lambda *a, **k: _Task())
    return _Task


async def _draft(
    client,
    db_session,
    *,
    to="supplier@example.com",
    subject="Тема",
    body="<p>Обычный текст письма.</p>",
):
    from app.db.models import MailboxConfig

    if not (
        await db_session.execute(
            __import__("sqlalchemy").select(MailboxConfig).where(MailboxConfig.name == "riskbox")
        )
    ).scalar_one_or_none():
        db_session.add(
            MailboxConfig(
                name="riskbox",
                imap_host="imap.example.com",
                imap_port=993,
                imap_user="riskbox",
                imap_password_encrypted="x",
                imap_ssl=True,
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_user="riskbox@example.com",
                smtp_password_encrypted="y",
                is_active=True,
            )
        )
        await db_session.commit()

    resp = await client.post(
        "/api/email/drafts",
        json={
            "to_addresses": [to],
            "subject": subject,
            "body_html": body,
            "mailbox": "riskbox",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _send(client, draft_id, **payload):
    """Отправка так, как её обязан делать вызывающий: с expected_digest.

    Дайджест перестал быть необязательным: без него подтверждение относилось
    бы к идентификатору черновика, а не к тексту письма (см. send_email).
    """
    got = await client.get(f"/api/email/drafts/{draft_id}")
    assert got.status_code == 200, got.text
    body = {"expected_digest": got.json()["content_digest"], **payload}
    return await client.post(f"/api/email/drafts/{draft_id}/send", json=body)


async def test_send_without_a_prior_risk_check_now_works(client, db_session, _no_real_send):
    """Отдельный вызов risk_check больше не обязателен для вызывающего."""
    from app.db.models import DraftAction

    draft_id = await _draft(client, db_session)

    resp = await _send(client, draft_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    # Но проверка действительно выполнена, а не пропущена.
    draft = await db_session.get(DraftAction, uuid.UUID(draft_id))
    await db_session.refresh(draft)
    # Статус после отправки — "queued"; след проверки остаётся в самих флагах
    # и в замороженном на содержимом дайджесте.
    assert "risk_flags" in draft.draft_data
    assert draft.draft_data["risk_checked_digest"] == draft.draft_data["content_digest"]


async def test_a_blocking_flag_still_stops_the_send(client, db_session, _no_real_send):
    """Главное свойство: авто-проверка не превращается в авто-разрешение."""
    draft_id = await _draft(
        client,
        db_session,
        body="<p>Это конфиденциально, никому не пересылайте.</p>",
        subject="Конфиденциально",
    )

    resp = await _send(client, draft_id)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "blocked_by_risk"
    assert "sensitive_content" in detail["blocked_by"]


async def test_a_human_can_override_a_block_and_it_is_audited(client, db_session, _no_real_send):
    from sqlalchemy import select

    from app.db.models import AuditLog

    draft_id = await _draft(
        client,
        db_session,
        body="<p>Коммерческая тайна: не пересылать.</p>",
        subject="Тайна",
    )
    assert (await _send(client, draft_id)).status_code == 400

    resp = await _send(client, draft_id, acknowledged_risks=["sensitive_content"])
    assert resp.status_code == 200, resp.text

    rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "email.risk_override")))
        .scalars()
        .all()
    )
    assert rows, "обход блокирующей проверки обязан попадать в аудит"


async def test_content_changed_after_the_check_is_rechecked(client, db_session, _no_real_send):
    """Изменение после проверки не проходит по старому вердикту: раньше это
    был отказ с просьбой повторить risk_check, теперь проверка повторяется
    сама — но на НОВОМ содержимом."""

    draft_id = await _draft(client, db_session)
    assert (
        await client.post(f"/api/email/drafts/{draft_id}/risk-check", json={})
    ).status_code == 200

    # Подменяем тело на то, что проверка обязана заблокировать.
    resp = await client.patch(
        f"/api/email/drafts/{draft_id}",
        json={
            "body_html": "<p>Это конфиденциально, не для распространения.</p>",
        },
    )
    assert resp.status_code == 200, resp.text

    resp = await _send(client, draft_id)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "blocked_by_risk"


async def test_an_approval_for_other_content_is_still_refused(client, db_session, _no_real_send):
    """Подтверждение привязано к содержимому. Авто-проверка не должна это
    ослаблять: свежий прогон перезаписывает content_digest, и если сверять
    подтверждение после него — подмена стала бы «совпадающей»."""
    draft_id = await _draft(client, db_session)
    r = await client.post(f"/api/email/drafts/{draft_id}/risk-check", json={})
    assert r.status_code == 200

    resp = await client.get(f"/api/email/drafts/{draft_id}")
    stale_digest = resp.json()["content_digest"]

    await client.patch(f"/api/email/drafts/{draft_id}", json={"subject": "Совсем другая тема"})

    resp = await client.post(
        f"/api/email/drafts/{draft_id}/send",
        json={"expected_digest": stale_digest},
    )
    assert resp.status_code == 409, resp.text


async def test_a_queued_draft_is_not_queued_twice(client, db_session, _no_real_send):
    """Защита от дубля больше не держится на чужой ошибке.

    Раньше повторную отправку отбивала проверка «risk_check обязателен»: у
    поставленного в очередь черновика статус переставал быть risk_checked. Как
    только send научился прогонять проверку сам, эта случайная защита исчезла
    бы — а в живом инциденте на один черновик пришлось шесть задач отправки.
    """
    draft_id = await _draft(client, db_session, subject="Однократность")

    first = await _send(client, draft_id)
    assert first.status_code == 200, first.text

    second = await _send(client, draft_id)
    assert second.status_code == 400, second.text
    assert second.json()["detail"]["error_code"] == "already_queued"


async def test_a_cancelled_draft_can_be_sent_again(client, db_session, _no_real_send):
    """Отмена возвращает черновик в работу — иначе «Отменить» означало бы
    «выбросить письмо»."""
    draft_id = await _draft(client, db_session, subject="Отмена и повтор")
    assert (await _send(client, draft_id)).status_code == 200

    cancel = await client.post(f"/api/email/drafts/{draft_id}/cancel-send")
    assert cancel.status_code == 200, cancel.text

    again = await _send(client, draft_id)
    assert again.status_code == 200, again.text
