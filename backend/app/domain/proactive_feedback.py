"""Calibrate proactive beat tasks against how users actually react to them.

AGENT_AUTONOMY_ROADMAP.md Фаза 0.B: a proactive task (due-date reminder,
stale-approval nudge, ...) that a user consistently dismisses is doing more
harm than good — this module turns ProactiveTaskFeedback rows into an
acceptance rate per beat task, and a self-throttle settings store (same
Redis-JSON pattern as app.ai.graph_analytics.GraphAnalyticsSettings) that lets
a low-acceptance task quiet itself down without an admin having to notice and
disable it by hand.

Only the proactive tasks that notify one specific user (Notification.source_task
set — see that column's docstring) can be calibrated this way. Broadcast-style
proactive tasks (critical anomalies, morning briefing, duplicate invoices) have
no single recipient to attribute accept/dismiss to and are out of scope.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProactiveTaskFeedback

logger = structlog.get_logger()

FeedbackAction = Literal["accepted", "dismissed", "snoozed"]

_SETTINGS_REDIS_KEY = "proactive_throttle_settings"
_MIN_SAMPLE_SIZE = 5
_DEFAULT_LOW_ACCEPTANCE_THRESHOLD = 0.2
_DEFAULT_LOW_ACCEPTANCE_MIN_SAMPLE = 10


class ProactiveTaskThrottleSettings(BaseModel):
    """Runtime-tunable calibration thresholds, one shared config for all tasks.

    Stored in Redis (not env/import-time) so it can change from an admin GUI
    without a container restart — same shape as GraphAnalyticsSettings.
    ``enabled=False`` disables self-throttling entirely (every proactive task
    always runs), which is also the fail-open behaviour on a Redis outage.
    """

    enabled: bool = True
    low_acceptance_threshold: float = Field(
        default=_DEFAULT_LOW_ACCEPTANCE_THRESHOLD, ge=0.0, le=1.0
    )
    low_acceptance_min_sample: int = Field(default=_DEFAULT_LOW_ACCEPTANCE_MIN_SAMPLE, ge=1)
    window_days: int = Field(default=30, ge=1)


def _redis_get_settings() -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_SETTINGS_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("proactive_throttle_settings_read_failed", error=str(exc))
        return None


def _redis_set_settings(value: dict) -> None:
    from app.utils.redis_client import get_sync_redis

    get_sync_redis().set(_SETTINGS_REDIS_KEY, json.dumps(value))


def get_proactive_throttle_settings() -> ProactiveTaskThrottleSettings:
    raw = _redis_get_settings()
    return ProactiveTaskThrottleSettings(**raw) if raw else ProactiveTaskThrottleSettings()


def save_proactive_throttle_settings(
    settings: ProactiveTaskThrottleSettings,
) -> ProactiveTaskThrottleSettings:
    _redis_set_settings(settings.model_dump())
    return settings


class AcceptanceRate(BaseModel):
    beat_task_name: str
    window_days: int
    accepted: int
    dismissed: int
    snoozed: int
    # None when there's too little data to draw a conclusion from — a task
    # with 2 observations reads the same as "insufficient_data" whether both
    # were accepted or both dismissed, so callers must check this before
    # treating `rate` as meaningful.
    rate: float | None
    sample_size: int
    status: Literal["ok", "insufficient_data"]


async def get_proactive_task_acceptance_rate(
    db: AsyncSession,
    beat_task_name: str,
    *,
    window_days: int = 30,
    min_sample_size: int = _MIN_SAMPLE_SIZE,
) -> AcceptanceRate:
    """Fraction of accepted-vs-decided (accepted / (accepted+dismissed)) feedback.

    Snoozed observations count toward sample_size (they're a real reaction —
    "not now" rather than "never"), but are excluded from the rate itself:
    they aren't a verdict on whether the nudge was wanted, so folding them
    into either side of the ratio would just add noise to the deciding
    signal, not information.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        await db.execute(
            select(ProactiveTaskFeedback.action, func.count())
            .where(
                ProactiveTaskFeedback.beat_task_name == beat_task_name,
                ProactiveTaskFeedback.created_at >= since,
            )
            .group_by(ProactiveTaskFeedback.action)
        )
    ).all()
    counts = {action: count for action, count in rows}
    accepted = counts.get("accepted", 0)
    dismissed = counts.get("dismissed", 0)
    snoozed = counts.get("snoozed", 0)
    decided = accepted + dismissed
    sample_size = accepted + dismissed + snoozed

    if decided < min_sample_size:
        return AcceptanceRate(
            beat_task_name=beat_task_name,
            window_days=window_days,
            accepted=accepted,
            dismissed=dismissed,
            snoozed=snoozed,
            rate=None,
            sample_size=sample_size,
            status="insufficient_data",
        )
    return AcceptanceRate(
        beat_task_name=beat_task_name,
        window_days=window_days,
        accepted=accepted,
        dismissed=dismissed,
        snoozed=snoozed,
        rate=accepted / decided,
        sample_size=sample_size,
        status="ok",
    )


async def should_throttle_proactive_task(db: AsyncSession, beat_task_name: str) -> bool:
    """True if this task's own users have shown it's not wanted enough to keep
    running at full cadence. Fail-open: any error, or too little data, means
    "run as normal" — silence is never treated as evidence of unwantedness.
    """
    settings = get_proactive_throttle_settings()
    if not settings.enabled:
        return False
    rate = await get_proactive_task_acceptance_rate(
        db,
        beat_task_name,
        window_days=settings.window_days,
        min_sample_size=settings.low_acceptance_min_sample,
    )
    if rate.status != "ok" or rate.rate is None:
        return False
    return rate.rate < settings.low_acceptance_threshold


async def record_proactive_feedback(
    db: AsyncSession,
    *,
    beat_task_name: str,
    user_sub: str,
    action: FeedbackAction,
    notification_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    snoozed_until: datetime | None = None,
) -> ProactiveTaskFeedback:
    row = ProactiveTaskFeedback(
        beat_task_name=beat_task_name,
        notification_id=notification_id,
        entity_type=entity_type,
        entity_id=entity_id,
        user_sub=user_sub,
        action=action,
        snoozed_until=snoozed_until,
    )
    db.add(row)
    await db.flush()
    return row


async def is_snoozed(
    db: AsyncSession,
    *,
    beat_task_name: str,
    entity_type: str,
    entity_id: uuid.UUID,
    user_sub: str | None = None,
) -> bool:
    """True if a still-active snooze exists for this (task, entity) pair
    (optionally scoped to one user). Proactive tasks should skip re-notifying
    a snoozed entity rather than re-annoying the user before their requested
    delay is up.

    Scoped by beat_task_name, not just entity: snoozing the "invoice is due
    soon" nudge for invoice X should not also silence an unrelated "approval
    for invoice X is stale" nudge — they call for different actions (pay vs.
    approve) and a user snoozing one has not said anything about the other.
    """
    now = datetime.now(UTC)
    query = select(ProactiveTaskFeedback.id).where(
        ProactiveTaskFeedback.action == "snoozed",
        ProactiveTaskFeedback.beat_task_name == beat_task_name,
        ProactiveTaskFeedback.entity_type == entity_type,
        ProactiveTaskFeedback.entity_id == entity_id,
        ProactiveTaskFeedback.snoozed_until.is_not(None),
        ProactiveTaskFeedback.snoozed_until > now,
    )
    if user_sub is not None:
        query = query.where(ProactiveTaskFeedback.user_sub == user_sub)
    row = (await db.execute(query.limit(1))).scalar_one_or_none()
    return row is not None
