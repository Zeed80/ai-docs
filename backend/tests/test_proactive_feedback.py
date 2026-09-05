"""Unit tests for app.domain.proactive_feedback (AGENT_AUTONOMY_ROADMAP.md Ф0.B).

DB access is mocked (same style as test_proactive.py) rather than using the
db_session fixture — that fixture needs a live Postgres, which isn't
reachable in this sandbox; real round-trips are verified against the rebuilt
live stack per the roadmap's Ф0.C checklist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.proactive_feedback import (
    ProactiveTaskThrottleSettings,
    get_proactive_task_acceptance_rate,
    is_snoozed,
    record_proactive_feedback,
    should_throttle_proactive_task,
)


def _mock_db(all_rows=None, scalar_row=None):
    db = AsyncMock()
    result = MagicMock()
    if all_rows is not None:
        result.all = MagicMock(return_value=all_rows)
    if scalar_row is not None or scalar_row is None:
        result.scalar_one_or_none = MagicMock(return_value=scalar_row)
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


# ── get_proactive_task_acceptance_rate ──────────────────────────────────────


class TestAcceptanceRate:
    @pytest.mark.asyncio
    async def test_insufficient_data_below_min_sample(self):
        db = _mock_db(all_rows=[("accepted", 2), ("dismissed", 1)])
        rate = await get_proactive_task_acceptance_rate(
            db, "proactive.check_due_dates", min_sample_size=5
        )
        assert rate.status == "insufficient_data"
        assert rate.rate is None
        assert rate.accepted == 2 and rate.dismissed == 1

    @pytest.mark.asyncio
    async def test_computes_rate_from_accepted_and_dismissed(self):
        db = _mock_db(all_rows=[("accepted", 8), ("dismissed", 2), ("snoozed", 5)])
        rate = await get_proactive_task_acceptance_rate(
            db, "proactive.check_due_dates", min_sample_size=5
        )
        assert rate.status == "ok"
        assert rate.rate == pytest.approx(0.8)
        assert rate.sample_size == 15  # snoozed counts toward sample_size...

    @pytest.mark.asyncio
    async def test_snoozed_does_not_skew_the_rate(self):
        """snoozed observations must not count as either accepted or dismissed."""
        db_lots_snoozed = _mock_db(all_rows=[("accepted", 5), ("dismissed", 5), ("snoozed", 100)])
        rate = await get_proactive_task_acceptance_rate(
            db_lots_snoozed, "proactive.check_due_dates", min_sample_size=5
        )
        assert rate.rate == pytest.approx(0.5)  # unaffected by the 100 snoozes

    @pytest.mark.asyncio
    async def test_no_feedback_at_all(self):
        db = _mock_db(all_rows=[])
        rate = await get_proactive_task_acceptance_rate(db, "proactive.check_due_dates")
        assert rate.status == "insufficient_data"
        assert rate.sample_size == 0


# ── should_throttle_proactive_task ──────────────────────────────────────────


class TestShouldThrottle:
    @pytest.mark.asyncio
    async def test_disabled_never_throttles(self):
        db = _mock_db(all_rows=[("accepted", 0), ("dismissed", 20)])
        with patch(
            "app.domain.proactive_feedback.get_proactive_throttle_settings",
            return_value=ProactiveTaskThrottleSettings(enabled=False),
        ):
            assert await should_throttle_proactive_task(db, "proactive.check_due_dates") is False

    @pytest.mark.asyncio
    async def test_insufficient_data_never_throttles(self):
        db = _mock_db(all_rows=[("dismissed", 1)])
        with patch(
            "app.domain.proactive_feedback.get_proactive_throttle_settings",
            return_value=ProactiveTaskThrottleSettings(enabled=True, low_acceptance_min_sample=10),
        ):
            assert await should_throttle_proactive_task(db, "proactive.check_due_dates") is False

    @pytest.mark.asyncio
    async def test_low_acceptance_with_enough_samples_throttles(self):
        # 1 accepted / 19 dismissed = 0.05 acceptance, well under the 0.2 default.
        db = _mock_db(all_rows=[("accepted", 1), ("dismissed", 19)])
        with patch(
            "app.domain.proactive_feedback.get_proactive_throttle_settings",
            return_value=ProactiveTaskThrottleSettings(
                enabled=True, low_acceptance_threshold=0.2, low_acceptance_min_sample=10
            ),
        ):
            assert await should_throttle_proactive_task(db, "proactive.check_due_dates") is True

    @pytest.mark.asyncio
    async def test_high_acceptance_does_not_throttle(self):
        db = _mock_db(all_rows=[("accepted", 18), ("dismissed", 2)])
        with patch(
            "app.domain.proactive_feedback.get_proactive_throttle_settings",
            return_value=ProactiveTaskThrottleSettings(
                enabled=True, low_acceptance_threshold=0.2, low_acceptance_min_sample=10
            ),
        ):
            assert await should_throttle_proactive_task(db, "proactive.check_due_dates") is False


# ── is_snoozed ────────────────────────────────────────────────────────────


class TestIsSnoozed:
    @pytest.mark.asyncio
    async def test_no_matching_row_means_not_snoozed(self):
        db = _mock_db(scalar_row=None)
        result = await is_snoozed(
            db,
            beat_task_name="proactive.check_due_dates",
            entity_type="invoice",
            entity_id=uuid.uuid4(),
            user_sub="user1",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_matching_row_means_snoozed(self):
        db = _mock_db(scalar_row=uuid.uuid4())
        result = await is_snoozed(
            db,
            beat_task_name="proactive.check_due_dates",
            entity_type="invoice",
            entity_id=uuid.uuid4(),
            user_sub="user1",
        )
        assert result is True


# ── record_proactive_feedback ────────────────────────────────────────────


class TestRecordProactiveFeedback:
    @pytest.mark.asyncio
    async def test_persists_feedback_row_with_given_fields(self):
        db = _mock_db()
        entity_id = uuid.uuid4()
        row = await record_proactive_feedback(
            db,
            beat_task_name="proactive.check_due_dates",
            user_sub="user1",
            action="dismissed",
            entity_type="invoice",
            entity_id=entity_id,
        )
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        assert row.beat_task_name == "proactive.check_due_dates"
        assert row.action == "dismissed"
        assert row.entity_id == entity_id

    @pytest.mark.asyncio
    async def test_snoozed_action_carries_snoozed_until(self):
        db = _mock_db()
        until = datetime.now(UTC) + timedelta(hours=1)
        row = await record_proactive_feedback(
            db,
            beat_task_name="proactive.check_due_dates",
            user_sub="user1",
            action="snoozed",
            snoozed_until=until,
        )
        assert row.snoozed_until == until
