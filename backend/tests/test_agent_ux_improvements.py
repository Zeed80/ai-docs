"""Взаимодействие с агентом: то, что человек видит и чем управляет.

Тесты названы по дефекту, который закрывают.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.approval_preview import build_preview, describe_call, is_irreversible


async def test_send_approval_shows_the_letter_not_identifiers(db_session):
    """Раньше карточка подтверждения была json.dumps(args): человек утверждал
    draft_id и дайджест, а прочитать письмо было негде."""
    from app.db.models import DraftAction, MailboxConfig

    db_session.add(MailboxConfig(
        name="prev-box", imap_host="imap.example.com", imap_port=993,
        imap_user="prev", imap_password_encrypted="x", imap_ssl=True,
        smtp_host="smtp.example.com", smtp_port=587, smtp_user="prev@example.com",
        smtp_password_encrypted="y", is_active=True,
    ))
    draft = DraftAction(
        action_type="email.send", entity_type="email",
        draft_data={
            "to_addresses": ["sales@romex.example"],
            "subject": "Запрос счёта",
            "body_text": "Добрый день! Пришлите, пожалуйста, счёт.",
            "mailbox": "prev-box",
            "risk_flags": [
                {"code": "first_time_recipient", "severity": "warning",
                 "message": "Впервые пишем на этот адрес"},
            ],
        },
    )
    db_session.add(draft)
    await db_session.commit()

    card = await build_preview(
        "email",
        {"action": "send", "draft_id": str(draft.id), "expected_digest": "abc"},
        db=db_session,
    )
    assert card.title == "Отправить письмо"
    labels = {f.label: f.value for f in card.fields}
    assert labels["Кому"] == "sales@romex.example"
    assert labels["Тема"] == "Запрос счёта"
    assert labels["Из ящика"] == "prev-box"
    assert "Пришлите" in (card.body_text or "")
    # Предупреждения видны ДО решения, а не после отправки.
    assert any("Впервые пишем" in w for w in card.warnings)
    # Письмо можно поправить прямо в карточке.
    assert card.editable == "email_draft"
    assert card.entity_id == str(draft.id)


async def test_preview_never_breaks_the_gate(db_session):
    """Сломанное превью не должно мешать подтверждению: в худшем случае —
    аргументы списком, а не отсутствующая карточка."""
    card = await build_preview("email", {"action": "send", "draft_id": str(uuid.uuid4())})
    assert card.title
    assert card.raw_args["draft_id"]


def test_irreversible_actions_are_marked():
    """«Подтвердить всё» не должно распространяться на то, что не отменить."""
    assert is_irreversible("email", {"action": "send"}) is True
    assert is_irreversible("payments", {"action": "mark_paid"}) is True
    assert is_irreversible("documents", {"action": "list"}) is False


def test_plan_steps_are_written_in_words():
    """План из имён инструментов («email», «invoices») не сообщает намерения."""
    assert describe_call("email", {"action": "send"}) == "отправлю письмо в почте"
    assert describe_call("invoices", {"action": "list"}) == "посмотрю список по счетам"


async def test_reason_reaches_the_card(db_session):
    """Агент может назвать причину вызова — её показывают человеку."""
    card = await build_preview(
        "documents", {"action": "list", "reason": "нужно найти счёт за март"}
    )
    assert card.reason == "нужно найти счёт за март"
    # reason не утекает в «сырые аргументы» дважды.
    assert "reason" not in card.raw_args


async def test_pending_approvals_are_in_the_single_inbox(client, db_session):
    """Самое срочное — «агент ждёт решения» — жило на отдельной странице и не
    попадало в общий список того, что ждёт человека."""
    from app.db.models import Approval, ApprovalActionType, ApprovalStatus

    db_session.add(Approval(
        action_type=ApprovalActionType.email_send,
        entity_type="email",
        entity_id=uuid.uuid4(),
        status=ApprovalStatus.pending,
        requested_by="sveta",
        context={"title": "Отправить письмо", "subtitle": "Ромекс", "irreversible": True},
    ))
    await db_session.commit()

    resp = await client.get("/api/inbox?kinds=approval,email")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    approvals = [i for i in body["items"] if i["kind"] == "approval"]
    assert approvals, body["counts"]
    assert approvals[0]["title"] == "Отправить письмо"
    assert approvals[0]["badge"] == "ждёт решения"
    # И идут выше обычных писем.
    assert body["items"][0]["kind"] == "approval"


async def test_setup_checklist_reports_real_state(client, db_session):
    """Пустой ящик предлагал «настроить почту» и на этом заканчивался."""
    resp = await client.get("/api/email/setup-status")
    assert resp.status_code == 200, resp.text
    steps = {s["key"]: s for s in resp.json()}
    assert {"mailbox", "smtp", "consent", "rules", "templates", "signature"} <= set(steps)
    assert all("url" in s and s["title"] for s in steps.values())


async def test_quiet_hours_and_digest_are_configurable(client):
    """Категории отвечали на «о чём», и нечем было сказать «не ночью»."""
    saved = await client.put(
        "/api/notifications/delivery",
        json={"quiet_from_hour": 22, "quiet_to_hour": 8, "digest_enabled": True,
              "digest_hour": 9},
    )
    assert saved.status_code == 200, saved.text
    again = await client.get("/api/notifications/delivery")
    assert again.json()["digest_enabled"] is True
    assert again.json()["quiet_from_hour"] == 22


@pytest.mark.parametrize(
    "start,end,hour,quiet",
    [
        (22, 8, 23, True),    # окно через полночь
        (22, 8, 3, True),
        (22, 8, 12, False),
        (9, 18, 12, True),    # обычное дневное окно
        (9, 18, 20, False),
        (None, None, 3, False),
    ],
)
def test_quiet_window_math(start, end, hour, quiet):
    from app.services.notifications import in_quiet_window

    assert in_quiet_window(hour, start, end) is quiet


async def test_digest_mode_suppresses_per_event_push(db_session):
    """Включённая сводка означает «одним письмом утром», а не «и то, и это»."""
    from app.db.models import UserNotificationSettings
    from app.services.notifications import _push_allowed_now

    db_session.add(UserNotificationSettings(user_sub="digest-user", digest_enabled=True))
    await db_session.commit()
    assert await _push_allowed_now(db_session, "digest-user") is False
    assert await _push_allowed_now(db_session, "someone-without-settings") is True


async def test_quiet_hours_follow_the_users_timezone(db_session, monkeypatch):
    """Часы считались по серверу: «не беспокоить с 22 до 8» означало чужие
    22:00, если сервер и человек в разных поясах."""
    from app.db.models import UserNotificationSettings
    from app.services import notifications as svc

    db_session.add(UserNotificationSettings(
        user_sub="tz-user", quiet_from_hour=22, quiet_to_hour=8,
        timezone="Asia/Vladivostok",
    ))
    await db_session.commit()

    # 15:00 UTC — это полночь во Владивостоке (UTC+10), то есть тишина, хотя
    # по серверу день.
    monkeypatch.setattr(svc, "local_hour", lambda tz: 0 if tz == "Asia/Vladivostok" else 15)
    assert await svc._push_allowed_now(db_session, "tz-user") is False


def test_unknown_timezone_falls_back_to_the_server():
    """Опечатка в зоне не должна ронять уведомление — просто как раньше."""
    from app.services.notifications import local_hour

    assert 0 <= local_hour("Nowhere/Nothing") <= 23
    assert 0 <= local_hour(None) <= 23


async def test_timezone_round_trips_and_rejects_nonsense(client):
    saved = await client.put(
        "/api/notifications/delivery",
        json={"digest_enabled": True, "digest_hour": 9, "timezone": "Europe/Moscow"},
    )
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/notifications/delivery")).json()["timezone"] == "Europe/Moscow"

    # «UTC+3» — не имя зоны: приняв такую строку, мы бы молча считали по
    # серверу, и человек бы этого не заметил.
    bad = await client.put(
        "/api/notifications/delivery",
        json={"digest_enabled": False, "digest_hour": 9, "timezone": "UTC+3"},
    )
    assert bad.status_code == 422


async def test_agent_quality_report_answers_where_it_errs(client, db_session):
    """Сырьё для «где агент чаще ошибается» собиралось, витрины не было."""
    from app.db.models import Approval, ApprovalActionType, ApprovalStatus

    db_session.add(Approval(
        action_type=ApprovalActionType.email_send, entity_type="email",
        entity_id=uuid.uuid4(), status=ApprovalStatus.rejected, requested_by="sveta",
    ))
    await db_session.commit()

    resp = await client.get("/api/agent/quality?days=30")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approvals_rejected"] >= 1
    assert any(r["action"] == "email.send" for r in body["top_rejected"])
