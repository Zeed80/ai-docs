"""Tests for Telegram integration API — /api/telegram/*, bot lifecycle, notifier."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox, DurableChatRun
from app.db.models import ChatSession, User, WorkOrder
from app.integrations.telegram_bot import SvetaTelegramBot

# ── Helpers ───────────────────────────────────────────────────────────────────


def _patch_redis(token: str = "", chat_id: str = "", allowed: str = "", enabled: str = ""):
    """Patch Redis and secret_store so tests don't need a live Redis."""
    store = {
        "telegram:config:bot_token": token,
        "telegram:config:notifications_chat_id": chat_id,
        "telegram:config:allowed_users": allowed,
        "telegram:config:notifications_enabled": enabled,
    }

    def mock_get(key: str) -> str:
        return store.get(key, "")

    def mock_set(key: str, value: str) -> None:
        store[key] = value

    def mock_encrypt(v: str) -> str:
        return v

    def mock_decrypt(v: str) -> str:
        return v

    def mock_mask(v: str, n: int = 4) -> str:
        if not v:
            return ""
        return v[:n] + "****"

    patches = [
        patch("app.api.telegram._r_get", side_effect=mock_get),
        patch("app.api.telegram._r_set", side_effect=mock_set),
        patch("app.utils.secret_store.encrypt", side_effect=mock_encrypt),
        patch("app.utils.secret_store.decrypt", side_effect=mock_decrypt),
        patch("app.utils.secret_store.mask", side_effect=mock_mask),
    ]
    return patches


def _enter_patches(patches):
    mocks = [p.__enter__() for p in patches]
    return mocks


def _exit_patches(patches):
    for p in patches:
        p.__exit__(None, None, None)


# ── Status endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_status_unconfigured(client: AsyncClient):
    """GET /api/telegram/status returns 200 with configured=False when no token."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.get("/api/telegram/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["bot_running"] is False
        assert "token_masked" in data
        assert "chat_id_masked" in data
        assert "last_error" in data
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_status_configured(client: AsyncClient):
    """GET /api/telegram/status returns configured=True when token is set."""
    patches = _patch_redis(token="1234567890:AAAAAAAAAAAAAAAAAAAAAA_test")
    for p in patches:
        p.start()
    try:
        resp = await client.get("/api/telegram/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["token_masked"].startswith("1234")
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_status_has_required_fields(client: AsyncClient):
    """Status response must contain all documented fields."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.get("/api/telegram/status")
        assert resp.status_code == 200
        data = resp.json()
        required = {
            "configured",
            "bot_running",
            "notifications_enabled",
            "has_default_chat",
            "allowed_users_count",
            "token_masked",
            "chat_id_masked",
            "allowed_users_masked",
            "last_error",
        }
        assert required.issubset(data.keys()), f"Missing fields: {required - data.keys()}"
    finally:
        for p in patches:
            p.stop()


# ── Config update endpoint ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_config_patch_stores_values(client: AsyncClient):
    """PATCH /api/telegram/config stores token and chat_id."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        with patch("asyncio.create_task"):  # don't actually start bot
            resp = await client.patch(
                "/api/telegram/config",
                json={
                    "bot_token": "987:TestToken",
                    "notifications_chat_id": "-100123456789",
                    "notifications_enabled": True,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["notifications_enabled"] is True
        assert data["has_default_chat"] is True
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_config_patch_partial(client: AsyncClient):
    """PATCH with only one field doesn't reset others."""
    patches = _patch_redis(
        token="initial:token",
        chat_id="-100111",
        enabled="true",
    )
    for p in patches:
        p.start()
    try:
        with patch("asyncio.create_task"):
            resp = await client.patch(
                "/api/telegram/config",
                json={"notifications_enabled": False},
            )
        assert resp.status_code == 200
        data = resp.json()
        # Token and chat_id should still be set
        assert data["configured"] is True
        assert data["notifications_enabled"] is False
    finally:
        for p in patches:
            p.stop()


# ── Bot restart / stop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_restart_no_token(client: AsyncClient):
    """POST /api/telegram/restart without token → bot does not start."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.post("/api/telegram/restart")
        assert resp.status_code == 200
        data = resp.json()
        # Bot should not be running (no token)
        assert data["bot_running"] is False
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_stop(client: AsyncClient):
    """POST /api/telegram/stop returns 200 regardless of bot state."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.post("/api/telegram/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bot_running"] is False
    finally:
        for p in patches:
            p.stop()


# ── Notify endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_notify_no_token(client: AsyncClient):
    """POST /api/telegram/notify without token → 503."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.post(
            "/api/telegram/notify",
            json={"chat_id": "-100123", "text": "Hello"},
        )
        assert resp.status_code == 503
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_notify_no_chat_id(client: AsyncClient):
    """POST /api/telegram/notify without chat_id and no default → 400."""
    patches = _patch_redis(token="123:Token")
    for p in patches:
        p.start()
    try:
        resp = await client.post(
            "/api/telegram/notify",
            json={"text": "Hello"},
        )
        assert resp.status_code == 400
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_notify_with_token_and_chat(client: AsyncClient):
    """POST /api/telegram/notify with token + chat_id → calls notifier."""
    patches = _patch_redis(token="123:Token")
    for p in patches:
        p.start()
    try:
        mock_notifier = MagicMock()
        mock_notifier.notify_text = AsyncMock(return_value=None)

        with patch(
            "app.integrations.telegram_notifier.TelegramNotifier",
            return_value=mock_notifier,
        ):
            resp = await client.post(
                "/api/telegram/notify",
                json={"chat_id": "-100123456", "text": "Тест уведомление"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_notifier.notify_text.assert_called_once_with("Тест уведомление")
    finally:
        for p in patches:
            p.stop()


# ── Test endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_test_no_token(client: AsyncClient):
    """POST /api/telegram/test without token → 503."""
    patches = _patch_redis()
    for p in patches:
        p.start()
    try:
        resp = await client.post("/api/telegram/test")
        assert resp.status_code == 503
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_test_no_chat_id(client: AsyncClient):
    """POST /api/telegram/test with token but no chat_id → 400."""
    patches = _patch_redis(token="123:Token")
    for p in patches:
        p.start()
    try:
        resp = await client.post("/api/telegram/test")
        assert resp.status_code == 400
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_telegram_test_success(client: AsyncClient):
    """POST /api/telegram/test with full config → sends test message."""
    patches = _patch_redis(token="123:Token", chat_id="-100123456")
    for p in patches:
        p.start()
    try:
        mock_notifier = MagicMock()
        mock_notifier.notify_text = AsyncMock(return_value=None)

        with patch(
            "app.integrations.telegram_notifier.TelegramNotifier",
            return_value=mock_notifier,
        ):
            resp = await client.post("/api/telegram/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_notifier.notify_text.assert_called_once()
        call_text = mock_notifier.notify_text.call_args[0][0]
        assert "Света" in call_text or "Тест" in call_text
    finally:
        for p in patches:
            p.stop()


# ── Durable Telegram intake (E15) ───────────────────────────────────────────────


def _telegram_bot(*allowed: int) -> SvetaTelegramBot:
    bot = object.__new__(SvetaTelegramBot)
    bot._allowed = set(allowed)
    return bot


def _telegram_update(*, update_id: int, user_id: int, text: str, chat_type: str = "private"):
    update = MagicMock()
    update.update_id = update_id
    update.effective_user.id = user_id
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.date = datetime.now(UTC) + timedelta(seconds=2)
    update.message.reply_text = AsyncMock()
    return update


async def _bind_telegram(db, *, owner: str, telegram_id: int):
    db.add(
        User(
            sub=owner,
            email=f"{owner}@example.test",
            name=owner,
            preferred_username=owner,
            role="operator",
            is_active=True,
        )
    )
    binding = AgentChannelIdentity(
        owner_key=owner, channel="telegram", external_id=str(telegram_id)
    )
    db.add(binding)
    await db.flush()
    return binding


@pytest.fixture
def telegram_session_factory(db_session, monkeypatch):
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    return factory


@pytest.mark.asyncio
async def test_telegram_repeat_update_and_bot_restart_create_one_durable_work(
    db_session, telegram_session_factory
):
    telegram_id = 701001
    await _bind_telegram(db_session, owner="telegram-alice", telegram_id=telegram_id)
    await db_session.flush()
    update = _telegram_update(update_id=91001, user_id=telegram_id, text="Проверь новые счета")

    await _telegram_bot(telegram_id)._handle_text(update, MagicMock())
    # A new polling object has no process-local state; Telegram redelivery must
    # still resolve to the same persisted run and outbox notification.
    await _telegram_bot(telegram_id)._handle_text(update, MagicMock())

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(DurableChatRun.owner_key == "telegram-alice")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.owner_key == "telegram-alice")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentOutbox)
            .where(AgentOutbox.owner_key == "telegram-alice")
        )
        == 1
    )
    run = await db_session.scalar(
        select(DurableChatRun).where(DurableChatRun.owner_key == "telegram-alice")
    )
    assert run.intake_channel == "telegram"
    assert run.external_message_id == "update:91001"
    # Accepted/progress delivery is persisted. The bot sends no placeholder or
    # model output from a process-local closure.
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_intake_and_outbox_rollback_together_on_producer_failure(
    db_session, telegram_session_factory, monkeypatch
):
    telegram_id = 701002
    await _bind_telegram(db_session, owner="telegram-rollback", telegram_id=telegram_id)
    update = _telegram_update(update_id=91002, user_id=telegram_id, text="Atomic request")

    async def fail_producer(*args, **kwargs):
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr("app.domain.agent_outbox.produce_agent_outbox", fail_producer)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await _telegram_bot(telegram_id)._handle_text(update, MagicMock())

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(DurableChatRun.owner_key == "telegram-rollback")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.owner_key == "telegram-rollback")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentOutbox)
            .where(AgentOutbox.owner_key == "telegram-rollback")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_telegram_conflict_returns_generic_rejection(db_session, telegram_session_factory):
    telegram_id = 701003
    await _bind_telegram(db_session, owner="telegram-conflict", telegram_id=telegram_id)
    first = _telegram_update(update_id=91003, user_id=telegram_id, text="Original")
    changed = _telegram_update(update_id=91003, user_id=telegram_id, text="Changed")

    await _telegram_bot(telegram_id)._handle_text(first, MagicMock())
    await _telegram_bot(telegram_id)._handle_text(changed, MagicMock())

    changed.message.reply_text.assert_awaited_once_with("Сообщение не принято.")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(DurableChatRun.owner_key == "telegram-conflict")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_telegram_rejects_foreign_allowlist_id_without_work(
    db_session, telegram_session_factory
):
    alice_id, bob_id = 701101, 701102
    await _bind_telegram(db_session, owner="telegram-alice-allow", telegram_id=alice_id)
    await _bind_telegram(db_session, owner="telegram-bob-allow", telegram_id=bob_id)
    update = _telegram_update(update_id=91101, user_id=bob_id, text="Чужое сообщение")

    await _telegram_bot(alice_id)._handle_text(update, MagicMock())

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.owner_key == "telegram-bob-allow")
        )
        == 0
    )
    update.message.reply_text.assert_awaited_once_with("Доступ запрещён.")


@pytest.mark.asyncio
async def test_telegram_group_message_never_creates_work(db_session, telegram_session_factory):
    telegram_id = 701201
    await _bind_telegram(db_session, owner="telegram-group-owner", telegram_id=telegram_id)
    update = _telegram_update(
        update_id=91201, user_id=telegram_id, text="Личные данные", chat_type="group"
    )

    await _telegram_bot(telegram_id)._handle_text(update, MagicMock())

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.owner_key == "telegram-group-owner")
        )
        == 0
    )
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "private"])
async def test_telegram_document_is_not_downloaded_before_private_binding(
    db_session, telegram_session_factory, chat_type
):
    telegram_id = 701250
    update = _telegram_update(update_id=91250, user_id=telegram_id, text="", chat_type=chat_type)
    update.message.document.file_id = "private-file"
    context = MagicMock()
    context.bot.get_file = AsyncMock()

    await _telegram_bot(telegram_id)._handle_document(update, context)

    context.bot.get_file.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(DurableChatRun.external_message_id == "update:91250")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_telegram_rebind_starts_new_owner_history(db_session, telegram_session_factory):
    telegram_id = 701301
    await _bind_telegram(db_session, owner="telegram-rebind-alice", telegram_id=telegram_id)
    first = _telegram_update(update_id=91301, user_id=telegram_id, text="Alice secret")
    await _telegram_bot(telegram_id)._handle_text(first, MagicMock())

    db_session.add(
        User(
            sub="telegram-rebind-bob",
            email="telegram-rebind-bob@example.test",
            name="telegram-rebind-bob",
            preferred_username="telegram-rebind-bob",
            role="operator",
            is_active=True,
        )
    )
    binding = await db_session.scalar(
        select(AgentChannelIdentity).where(AgentChannelIdentity.external_id == str(telegram_id))
    )
    binding.is_active = False
    await db_session.flush()
    binding = AgentChannelIdentity(
        owner_key="telegram-rebind-bob", channel="telegram", external_id=str(telegram_id)
    )
    db_session.add(binding)
    await db_session.flush()
    second = _telegram_update(update_id=91302, user_id=telegram_id, text="Bob request")
    await _telegram_bot(telegram_id)._handle_text(second, MagicMock())
    # A delayed redelivery from Alice's binding must not be reinterpreted as a
    # new Bob request merely because E12 namespaces intake by verified owner.
    await _telegram_bot(telegram_id)._handle_text(first, MagicMock())

    owners = ["telegram-rebind-alice", "telegram-rebind-bob"]
    # Only this test's owners: the test DB is shared and other tests commit
    # chat runs of their own (it failed in every full run).
    runs = (
        await db_session.scalars(
            select(DurableChatRun)
            .where(DurableChatRun.owner_key.in_(owners))
            .order_by(DurableChatRun.created_at)
        )
    ).all()
    assert [run.owner_key for run in runs] == owners
    assert runs[0].session_id != runs[1].session_id
    sessions = (
        await db_session.scalars(
            select(ChatSession).where(
                ChatSession.user_key.in_(["telegram-rebind-alice", "telegram-rebind-bob"])
            )
        )
    ).all()
    assert {session.user_key for session in sessions} == {
        "telegram-rebind-alice",
        "telegram-rebind-bob",
    }


@pytest.mark.asyncio
async def test_unseen_pre_rebind_update_cannot_enter_new_owner_history(
    db_session, telegram_session_factory
):
    telegram_id = 701302
    await _bind_telegram(db_session, owner="telegram-old-owner", telegram_id=telegram_id)
    stale = _telegram_update(update_id=91303, user_id=telegram_id, text="Old private request")
    stale.message.date = datetime.now(UTC) - timedelta(minutes=5)

    old_binding = await db_session.scalar(
        select(AgentChannelIdentity).where(AgentChannelIdentity.external_id == str(telegram_id))
    )
    old_binding.is_active = False
    db_session.add(
        User(
            sub="telegram-new-owner",
            email="telegram-new-owner@example.test",
            name="telegram-new-owner",
            preferred_username="telegram-new-owner",
            role="operator",
            is_active=True,
        )
    )
    db_session.add(
        AgentChannelIdentity(
            owner_key="telegram-new-owner", channel="telegram", external_id=str(telegram_id)
        )
    )
    await db_session.flush()

    await _telegram_bot(telegram_id)._handle_text(stale, MagicMock())
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(DurableChatRun.owner_key == "telegram-new-owner")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.owner_key == "telegram-new-owner")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_telegram_binding_soft_revoke_survives_outbox_and_rebinds(db_session):
    telegram_id = "701401"
    await _bind_telegram(db_session, owner="telegram-soft-alice", telegram_id=int(telegram_id))
    binding = await db_session.scalar(
        select(AgentChannelIdentity).where(AgentChannelIdentity.external_id == telegram_id)
    )
    order = WorkOrder(owner_key="telegram-soft-alice", objective="Historical notification")
    db_session.add(order)
    await db_session.flush()
    from app.domain.agent_outbox import AgentOutboxRequest, produce_agent_outbox

    await produce_agent_outbox(
        db_session,
        request=AgentOutboxRequest(
            work_order_id=order.id,
            owner_key="telegram-soft-alice",
            destination_binding_id=binding.id,
            event_type="chat.reply_ready",
            payload={"resource_type": "work_order", "resource_id": str(order.id)},
            dedup_key="soft-revoke-history",
        ),
    )
    await unbind_telegram(telegram_id, db_session, _DEV_USER)
    assert binding.is_active is False

    db_session.add(
        User(
            sub="telegram-soft-bob",
            email="telegram-soft-bob@example.test",
            name="telegram-soft-bob",
            preferred_username="telegram-soft-bob",
            role="operator",
            is_active=True,
        )
    )
    await db_session.flush()
    response = await bind_telegram(
        TelegramBinding(owner_key="telegram-soft-bob", telegram_user_id=telegram_id),
        db_session,
        _DEV_USER,
    )
    assert response["id"] != str(binding.id)
    assert binding.owner_key == "telegram-soft-alice"
    assert binding.is_active is False
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentOutbox)
            .where(AgentOutbox.work_order_id == order.id)
        )
        == 1
    )


# ── Telegram approval handoff (E16) ──────────────────────────────────────────


async def _pending_telegram_approval(
    db_session,
    *,
    expires_at=None,
    assigned: bool = True,
    binding_active: bool = True,
    approver_role: str = "manager",
    finite_expiry: bool = True,
):
    """Build one real durable approval; no Telegram network calls are made."""
    from app.api.work_orders import WorkApprovalIn, request_work_approval
    from app.auth.models import UserInfo
    from app.domain.work_orders import create_work_order, create_work_plan

    owner = "telegram-approval-owner"
    approver = "telegram-approval-manager"
    telegram_id = 709001
    db_session.add_all(
        [
            User(
                sub=owner,
                email="owner@example.test",
                name="owner",
                preferred_username="owner",
                role="operator",
                is_active=True,
            ),
            User(
                sub=approver,
                email="manager@example.test",
                name="manager",
                preferred_username="manager",
                role=approver_role,
                is_active=True,
            ),
        ]
    )
    binding = AgentChannelIdentity(
        owner_key=approver,
        channel="telegram",
        external_id=str(telegram_id),
        is_active=binding_active,
    )
    db_session.add(binding)
    order = await create_work_order(db_session, owner_key=owner, objective="Approval handoff")
    _, steps = await create_work_plan(
        db_session,
        order,
        steps=[{"step_key": "send", "title": "send", "kind": "agent_turn", "input": {}}],
    )
    await db_session.flush()
    owner_info = UserInfo(
        sub=owner, email="owner@example.test", name="owner", preferred_username="owner"
    )
    response = await request_work_approval(
        order.id,
        WorkApprovalIn(
            step_id=steps[0].id,
            capability="email",
            action="send",
            arguments={"recipient": "safe@example.test"},
            reason="Human approval",
            assigned_to=approver if assigned else None,
            expires_at=(expires_at or datetime.now(UTC) + timedelta(hours=1))
            if finite_expiry
            else None,
        ),
        db_session,
        owner_info,
    )
    from app.db.agent_runtime_models import TelegramApprovalCallback
    from app.db.models import Approval

    approval = await db_session.get(Approval, response["approval_id"])
    callback = await db_session.scalar(
        select(TelegramApprovalCallback).where(TelegramApprovalCallback.approval_id == approval.id)
    )
    await db_session.commit()
    return approval, binding, callback, telegram_id


@pytest.mark.asyncio
async def test_work_approval_persists_opaque_callback_and_approver_outbox(db_session):
    approval, binding, callback, _telegram_id = await _pending_telegram_approval(db_session)
    outbox = await db_session.scalar(
        select(AgentOutbox).where(AgentOutbox.approval_id == approval.id)
    )
    assert callback is not None
    assert callback.approval_id == approval.id
    assert callback.binding_id == binding.id
    assert callback.owner_key == binding.owner_key
    assert outbox is not None
    assert outbox.approval_id == approval.id
    assert outbox.owner_key == binding.owner_key
    assert outbox.destination_binding_id == binding.id
    assert outbox.payload == {"resource_type": "work_order", "resource_id": str(approval.entity_id)}


@pytest.mark.asyncio
async def test_telegram_approval_forwarded_button_is_not_a_human_identity(db_session):
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    result = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id + 1
    )
    assert result.status == "unavailable"
    assert approval.status.value == "pending"


@pytest.mark.asyncio
async def test_telegram_approval_expired_callback_never_resumes_work(db_session):
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    callback.expires_at = approval.expires_at
    await db_session.commit()
    result = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    assert result.status == "expired"
    assert approval.status.value == "expired"


@pytest.mark.asyncio
async def test_telegram_approval_double_click_is_single_use(db_session):
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    first = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    second = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    assert first.status == "approved"
    assert second.status == "unavailable"
    assert approval.status.value == "approved"


@pytest.mark.asyncio
async def test_telegram_approval_stale_callback_never_creates_another_step(db_session):
    from app.db.models import ApprovalStatus
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    approval.status = ApprovalStatus.rejected
    await db_session.commit()
    result = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    assert result.status == "unavailable"
    assert approval.status == ApprovalStatus.rejected


@pytest.mark.asyncio
async def test_telegram_approval_rechecks_revoked_binding(db_session):
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    binding.is_active = False
    await db_session.commit()
    result = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    assert result.status == "unavailable"
    assert approval.status.value == "pending"


@pytest.mark.asyncio
async def test_telegram_approval_rejects_changed_exact_arguments(db_session):
    from app.domain.telegram_approvals import settle_telegram_approval_callback

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    approval.context = {**approval.context, "tool_args": {"recipient": "changed@example.test"}}
    await db_session.commit()
    result = await settle_telegram_approval_callback(
        db_session, token=callback.token, approved=True, telegram_user_id=telegram_id
    )
    assert result.status == "unavailable"
    assert approval.status.value == "pending"


@pytest.mark.asyncio
async def test_telegram_approval_callback_handler_uses_fake_query_only(
    db_session, telegram_session_factory
):
    """The bot handler settles an opaque token without sending Telegram traffic."""
    from app.domain.telegram_approvals import callback_data

    _approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    update = MagicMock()
    update.callback_query.data = callback_data(callback, True)
    update.callback_query.from_user.id = telegram_id
    update.callback_query.message.text = "Approve exact action"
    update.callback_query.message.chat.type = "private"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    await _telegram_bot(telegram_id)._handle_callback(update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "Решение сохранено" in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_telegram_approval_callback_rejects_group_forward(
    db_session, telegram_session_factory
):
    from app.domain.telegram_approvals import callback_data

    approval, _binding, callback, telegram_id = await _pending_telegram_approval(db_session)
    update = MagicMock()
    update.callback_query.data = callback_data(callback, True)
    update.callback_query.from_user.id = telegram_id
    update.callback_query.message.chat.type = "group"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    await _telegram_bot(telegram_id)._handle_callback(update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()
    assert approval.status.value == "pending"


@pytest.mark.asyncio
async def test_telegram_approval_migration_round_trip(db_session):
    import importlib.util
    import uuid
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/20260929_0004_telegram_approval_callbacks.py"
    )
    spec = importlib.util.spec_from_file_location("telegram_approval_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "telegram_approval_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        op = Operations(MigrationContext.configure(sync))
        op.create_table("agent_outbox", sa.Column("id", sa.UUID(), primary_key=True))
        module.op = op
        module.upgrade()
        inspector = inspect(sync)
        assert "telegram_approval_callbacks" in inspector.get_table_names(schema=schema)
        assert "approval_id" in {
            column["name"] for column in inspector.get_columns("agent_outbox", schema=schema)
        }
        module.downgrade()
        assert "telegram_approval_callbacks" not in inspect(sync).get_table_names(schema=schema)
        assert "approval_id" not in {
            column["name"] for column in inspect(sync).get_columns("agent_outbox", schema=schema)
        }
        module.upgrade()
        assert "telegram_approval_callbacks" in inspect(sync).get_table_names(schema=schema)

    await connection.run_sync(verify)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assigned", "binding_active", "approver_role", "finite_expiry"),
    [
        (False, True, "manager", True),
        (True, False, "manager", True),
        (True, True, "viewer", True),
        (True, True, "manager", False),
    ],
)
async def test_work_approval_creates_no_telegram_callback_without_verified_approver(
    db_session, assigned, binding_active, approver_role, finite_expiry
):
    from app.db.agent_runtime_models import TelegramApprovalCallback

    approval, _binding, callback, _telegram_id = await _pending_telegram_approval(
        db_session,
        assigned=assigned,
        binding_active=binding_active,
        approver_role=approver_role,
        finite_expiry=finite_expiry,
    )
    assert callback is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentOutbox)
            .where(AgentOutbox.approval_id == approval.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TelegramApprovalCallback)
            .where(TelegramApprovalCallback.approval_id == approval.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_concurrent_admin_binds_have_one_winner(test_engine):
    """The absent-row case is serialized before either request can insert."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    owner = "telegram-concurrent-admin"
    async with factory() as db:
        db.add(
            User(
                sub=owner,
                email=f"{owner}@example.test",
                name=owner,
                preferred_username=owner,
                role="operator",
                is_active=True,
            )
        )
        await db.commit()

    async def bind_once():
        async with factory() as db:
            try:
                return await bind_telegram(
                    TelegramBinding(owner_key=owner, telegram_user_id="701501"), db, _DEV_USER
                )
            except HTTPException as exc:
                return exc

    first, second = await asyncio.gather(bind_once(), bind_once())
    assert sum(isinstance(item, dict) for item in (first, second)) == 1
    loser = next(item for item in (first, second) if isinstance(item, HTTPException))
    assert loser.status_code == 409
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AgentChannelIdentity)
                .where(
                    AgentChannelIdentity.channel == "telegram",
                    AgentChannelIdentity.external_id == "701501",
                    AgentChannelIdentity.is_active.is_(True),
                )
            )
            == 1
        )


from app.api.agent_channels import TelegramBinding, bind_telegram, unbind_telegram
from app.auth.jwt import _DEV_USER
