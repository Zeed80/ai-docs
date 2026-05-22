"""Tests for Telegram integration API — /api/telegram/*, bot lifecycle, notifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


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
            "configured", "bot_running", "notifications_enabled",
            "has_default_chat", "allowed_users_count", "token_masked",
            "chat_id_masked", "allowed_users_masked", "last_error",
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
