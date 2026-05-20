"""Tests for proactive Celery tasks (Эпик 6.2).

All DB access is mocked; we patch the source of _get_session_factory
(app.db.session._get_session_factory) since tasks import it locally.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_invoice(due_days: int, inv_id=None, number="ТТН-001"):
    inv = MagicMock()
    inv.id = inv_id or uuid.uuid4()
    inv.invoice_number = number
    inv.due_date = datetime.now(timezone.utc) + timedelta(days=due_days)
    inv.created_by = "user1"
    return inv


def _make_approval(hours_old: int, action_type="invoice.approve", assigned="user2"):
    appr = MagicMock()
    appr.id = uuid.uuid4()
    appr.action_type = MagicMock()
    appr.action_type.value = action_type
    appr.entity_type = "invoice"
    appr.entity_id = uuid.uuid4()
    appr.assigned_to = assigned
    appr.requested_by = "sveta"
    appr.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    return appr


def _make_anomaly(hours_old: int):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.title = "Дублирующийся счёт"
    a.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    return a


def _make_reminder(user_id="user1", mins_overdue: int = 5):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.user_id = user_id
    r.entity_type = "invoice"
    r.entity_id = uuid.uuid4()
    r.remind_at = datetime.now(timezone.utc) - timedelta(minutes=mins_overdue)
    r.message = "Счёт ТТН-001 — срок оплаты 25.05.2026"
    r.is_sent = False
    r.sent_at = None
    return r


def _mock_db_ctx(rows_per_call: list):
    """Build an async context-manager mock whose execute() side_effect is rows_per_call.

    Each element of rows_per_call controls one execute() call:
      - list[…]  → result.scalars().all() returns the list
      - None     → result.scalar_one_or_none() returns None (no row found)
      - object   → result.scalar_one_or_none() returns that object
    """
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()
    mock_db.add = MagicMock()

    side_effects = []
    for rows in rows_per_call:
        result = MagicMock()
        if isinstance(rows, list):
            scalars = MagicMock()
            scalars.all.return_value = rows
            scalars.scalar_one_or_none.return_value = rows[0] if rows else None
            result.scalars.return_value = scalars
            result.scalar_one_or_none = MagicMock(return_value=rows[0] if rows else None)
        else:
            # scalar result (None or single object)
            scalars = MagicMock()
            scalars.all.return_value = [] if rows is None else [rows]
            scalars.scalar_one_or_none.return_value = rows
            result.scalars.return_value = scalars
            result.scalar_one_or_none = MagicMock(return_value=rows)
        side_effects.append(result)

    mock_db.execute = AsyncMock(side_effect=side_effects)
    return mock_db


# ── check_due_dates ────────────────────────────────────────────────────────────

class TestCheckDueDates:
    @pytest.mark.asyncio
    async def test_creates_reminder_for_approaching_invoice(self):
        from app.tasks.proactive import _check_due_dates

        inv = _make_invoice(due_days=2)
        mock_db = _mock_db_ctx([[inv], None])  # invoices, no existing reminder

        create_notif = AsyncMock()
        mock_notif_obj = MagicMock()
        mock_notif_obj.id = uuid.uuid4()
        mock_notif_obj.created_at = datetime.now(timezone.utc)
        create_notif.return_value = mock_notif_obj

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
            patch("app.services.notifications.create_notification", create_notif),
        ):
            result = await _check_due_dates()

        assert result["created"] == 1
        mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_invoice_with_existing_reminder(self):
        from app.tasks.proactive import _check_due_dates

        inv = _make_invoice(due_days=1)
        existing = MagicMock()
        mock_db = _mock_db_ctx([[inv], existing])  # invoice found, reminder exists

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
        ):
            result = await _check_due_dates()

        assert result["created"] == 0
        mock_db.add.assert_not_called()


# ── check_stale_approvals ─────────────────────────────────────────────────────

class TestCheckStaleApprovals:
    @pytest.mark.asyncio
    async def test_notifies_assignee_for_stale_approval(self):
        from app.tasks.proactive import _check_stale_approvals

        appr = _make_approval(hours_old=30, assigned="manager1")
        mock_db = _mock_db_ctx([[appr]])

        create_notif = AsyncMock()
        mock_notif_obj = MagicMock()
        mock_notif_obj.id = uuid.uuid4()
        mock_notif_obj.created_at = datetime.now(timezone.utc)
        create_notif.return_value = mock_notif_obj

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
            patch("app.tasks.proactive._llm_enrich", new_callable=AsyncMock, return_value="Требует подтверждения"),
            patch("app.services.notifications.create_notification", create_notif),
        ):
            res = await _check_stale_approvals()

        assert res["alerted"] == 1

    @pytest.mark.asyncio
    async def test_skips_sveta_as_assignee(self):
        """Sveta (the AI agent) should not receive DB notifications about its own requests."""
        from app.tasks.proactive import _check_stale_approvals

        appr = _make_approval(hours_old=30, assigned="sveta")
        mock_db = _mock_db_ctx([[appr]])

        create_notif = AsyncMock()

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
            patch("app.tasks.proactive._llm_enrich", new_callable=AsyncMock, return_value="Требует подтверждения"),
            patch("app.services.notifications.create_notification", create_notif),
        ):
            await _check_stale_approvals()

        create_notif.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_queue_returns_zero(self):
        from app.tasks.proactive import _check_stale_approvals

        mock_db = _mock_db_ctx([[]])

        with patch("app.db.session._get_session_factory", return_value=lambda: mock_db):
            res = await _check_stale_approvals()

        assert res["alerted"] == 0


# ── alert_critical_anomalies ──────────────────────────────────────────────────

class TestAlertCriticalAnomalies:
    @pytest.mark.asyncio
    async def test_broadcasts_and_pushes_telegram(self):
        from app.tasks.proactive import _alert_critical_anomalies

        anomaly = _make_anomaly(hours_old=2)
        mock_db = _mock_db_ctx([[anomaly]])

        bus_publish = AsyncMock()
        tg_notify = AsyncMock()
        mock_notifier = MagicMock()
        mock_notifier.notify_critical_anomaly = tg_notify

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._llm_enrich", new_callable=AsyncMock, return_value="Аномалия требует внимания"),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=mock_notifier),
            patch("app.core.chat_bus.chat_bus.publish", bus_publish),
        ):
            res = await _alert_critical_anomalies()

        assert res["alerted"] == 1
        bus_publish.assert_called_once()
        tg_notify.assert_called_once_with(title=anomaly.title, anomaly_id=str(anomaly.id))

    @pytest.mark.asyncio
    async def test_no_anomalies_returns_zero(self):
        from app.tasks.proactive import _alert_critical_anomalies

        mock_db = _mock_db_ctx([[]])

        with patch("app.db.session._get_session_factory", return_value=lambda: mock_db):
            res = await _alert_critical_anomalies()

        assert res["alerted"] == 0


# ── dispatch_due_reminders ────────────────────────────────────────────────────

class TestDispatchDueReminders:
    @pytest.mark.asyncio
    async def test_creates_notification_for_known_user(self):
        from app.tasks.proactive import _dispatch_due_reminders

        reminder = _make_reminder(user_id="user1")
        mock_db = _mock_db_ctx([[reminder]])

        create_notif = AsyncMock()
        mock_notif_obj = MagicMock()
        mock_notif_obj.id = uuid.uuid4()
        mock_notif_obj.created_at = datetime.now(timezone.utc)
        create_notif.return_value = mock_notif_obj

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
            patch("app.services.notifications.create_notification", create_notif),
        ):
            res = await _dispatch_due_reminders()

        assert res["dispatched"] == 1
        assert reminder.is_sent is True

    @pytest.mark.asyncio
    async def test_broadcasts_for_generic_user(self):
        """Reminders with user_id='user' (default placeholder) should broadcast."""
        from app.tasks.proactive import _dispatch_due_reminders

        reminder = _make_reminder(user_id="user")
        mock_db = _mock_db_ctx([[reminder]])

        bus_publish = AsyncMock()
        create_notif = AsyncMock()

        with (
            patch("app.db.session._get_session_factory", return_value=lambda: mock_db),
            patch("app.tasks.proactive._get_notifier", new_callable=AsyncMock, return_value=None),
            patch("app.services.notifications.create_notification", create_notif),
            patch("app.core.chat_bus.chat_bus.publish", bus_publish),
        ):
            res = await _dispatch_due_reminders()

        assert res["dispatched"] == 1
        create_notif.assert_not_called()
        bus_publish.assert_called_once()


# ── _llm_enrich fallback ──────────────────────────────────────────────────────

class TestLlmEnrich:
    @pytest.mark.asyncio
    async def test_returns_fallback_on_error(self):
        from app.tasks.proactive import _llm_enrich

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("timeout"))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _llm_enrich("context", "fallback text")

        assert result == "fallback text"
