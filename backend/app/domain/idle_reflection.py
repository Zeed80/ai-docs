"""Idle-reflection — the agent's "subconscious": housekeeping that only runs
while no human is actively using the workspace.

Ф6 (AGENT_AUTONOMY_ROADMAP.md). Same self-throttle shape as
app.ai.graph_analytics.GraphAnalyticsSettings / app.domain.proactive_feedback.
ProactiveTaskThrottleSettings (Redis-JSON, tunable from an admin GUI without a
restart) plus the same run-state-as-MemoryFact pattern graph_analytics uses
for its own interval throttle — celery-beat ticks a fixed cadence, the task
self-throttles against both "is anyone using the system right now" and "did
we already run recently enough".

Split module/task shape mirrors app.ai.graph_analytics +
app.tasks.graph_analytics (the plan's own named reference pattern) rather
than the single-file app.tasks.idle_reflection.py the roadmap text
originally sketched — domain logic here, a thin Celery wrapper in
app/tasks/idle_reflection.py, consistent with how every other beat job in
this codebase is split.

Two real tasks run per tick (both idempotent, safe to re-run, never destroy
data — only mark/update, never delete):

1. **Duplicate-proposal consolidation** — collapses exact-duplicate
   MemoryFact(kind="proposed_fact") rows (same scope+title, still awaiting a
   human decision) so a reviewer doesn't see the same candidate fact twice.
   Deliberately narrow: work_order_lesson facts are already deduplicated at
   write time (process_work_learning's subject_key match, see
   app/domain/work_learning.py) and graph_insight facts are wholesale-
   replaced each analytics run (never accumulate) — proposed_fact is the one
   MemoryFact kind that can genuinely pile up duplicates with no existing
   write-time guard. Reuses the same status="superseded"/superseded_by_id
   vocabulary as supersede_learned_memory/process_work_learning, not a new
   consolidation mechanism.
2. **Connector revalidation** — for each SourceConnector overdue for
   re-attempt (due_for_revalidation, Ф5.C), directly re-fetches its
   strategy's sample_url and records the outcome. Deliberately a direct
   fetch, not a full exploratory WorkOrder/planner cycle (the roadmap text's
   "lightweight exploratory step" suggestion): a WorkOrder round-trip
   through the LLM planner is exactly the wrong tool for "is this one URL
   still reachable" — expensive, slow, and the connector already knows
   precisely which URL to check. Applies the outcome via
   connectors._apply_connector_outcome directly on the connector object
   already loaded in this function's own session — not
   record_connector_outcome (opens a second, independent session; found
   live while testing this exact function: it cannot see a row this same
   session only just created/updated inside a not-yet-externally-committed
   transaction — the identical class of bug Ф5 already fixed once for
   record_connector_success/failure) and not record_connector_success/
   failure either (those infer identity from a URL's domain and
   deliberately exclude retired connectors from their dedupe lookup — see
   connectors.py's own docstrings — which would silently create a fresh
   draft instead of reviving the specific retired connector being
   revalidated here).

A third task the roadmap text sketched — cross-checking MemoryFact against
SourceConnector for dangling references (e.g. "connector points at a since-
deleted web_source fact") — was not implemented: nothing in this codebase
ever writes such a reference (SourceConnector has no web_source linkage at
all yet, see AGENT_AUTONOMY_ROADMAP.md's Ф5.A open TODO). Checking for a
relationship the data model doesn't have would be busywork, not a real
integrity check; recorded here rather than silently dropped.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryFact, SourceConnector, User

logger = structlog.get_logger()

_SETTINGS_REDIS_KEY = "idle_reflection_settings"
_STATE_KIND = "idle_reflection_state"
_STATE_TITLE = "idle_reflection_state"

_DEFAULT_IDLE_THRESHOLD_MINUTES = 30
_DEFAULT_MIN_INTERVAL_SECONDS = 3_600  # don't re-run more often than hourly even while idle
_DEFAULT_CONNECTOR_BATCH_SIZE = 5      # cap outbound re-fetches per tick


class IdleReflectionSettings(BaseModel):
    """Runtime-tunable — Redis, not env/import-time, same rationale as
    GraphAnalyticsSettings/ProactiveTaskThrottleSettings."""

    enabled: bool = True
    idle_threshold_minutes: int = Field(default=_DEFAULT_IDLE_THRESHOLD_MINUTES, ge=1)
    min_interval_seconds: int = Field(default=_DEFAULT_MIN_INTERVAL_SECONDS, ge=60)
    connector_revalidation_batch_size: int = Field(default=_DEFAULT_CONNECTOR_BATCH_SIZE, ge=1, le=50)


def _redis_get_settings() -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_SETTINGS_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("idle_reflection_settings_read_failed", error=str(exc))
        return None


def _redis_set_settings(value: dict) -> None:
    from app.utils.redis_client import get_sync_redis

    get_sync_redis().set(_SETTINGS_REDIS_KEY, json.dumps(value))


def get_idle_reflection_settings() -> IdleReflectionSettings:
    raw = _redis_get_settings()
    return IdleReflectionSettings(**raw) if raw else IdleReflectionSettings()


def save_idle_reflection_settings(settings: IdleReflectionSettings) -> IdleReflectionSettings:
    _redis_set_settings(settings.model_dump())
    return settings


# ── Idle detection ──────────────────────────────────────────────────────


async def is_system_idle(db: AsyncSession, *, threshold_minutes: int) -> bool:
    """No user has been seen within the threshold window.

    User.last_seen_at (app/auth/user_service.py's upsert_user) is only
    refreshed on the OIDC callback — a coarse signal (a session can run for
    hours without hitting that route again), not a per-request heartbeat.
    No finer-grained activity tracker exists in this codebase today (no
    websocket-presence table, no last-API-call timestamp) — this is exactly
    the roadmap's own named fallback ("последний updated_at активной
    сессии/авторизованного запроса"), used honestly as the coarse signal it
    is rather than pretending to a precision this system doesn't track.
    No user having ever logged in (most_recent is None) counts as idle —
    there is no one to disturb.
    """
    most_recent = await db.scalar(select(func.max(User.last_seen_at)))
    if most_recent is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    return most_recent < cutoff


# ── Run-state (interval throttle) — mirrors graph_analytics.py exactly ────


async def _load_run_state(db: AsyncSession) -> dict[str, Any] | None:
    result = await db.execute(
        select(MemoryFact).where(MemoryFact.kind == _STATE_KIND, MemoryFact.title == _STATE_TITLE)
    )
    fact = result.scalar_one_or_none()
    return fact.metadata_ if fact else None


async def _save_run_state(db: AsyncSession) -> None:
    result = await db.execute(
        select(MemoryFact).where(MemoryFact.kind == _STATE_KIND, MemoryFact.title == _STATE_TITLE)
    )
    fact = result.scalar_one_or_none()
    payload = {"last_run_at": datetime.now(timezone.utc).isoformat()}
    if fact is None:
        fact = MemoryFact(
            scope="project",
            kind=_STATE_KIND,
            title=_STATE_TITLE,
            summary="Внутреннее состояние idle-reflection задачи (не для чтения агентом).",
            source="idle_reflection",
            confidence=1.0,
            pinned=False,
            metadata_=payload,
        )
        db.add(fact)
    else:
        fact.metadata_ = payload


# ── Task 1: duplicate-proposal consolidation ───────────────────────────────


async def consolidate_duplicate_proposed_facts(db: AsyncSession) -> int:
    """Collapse exact (scope, title) duplicates among still-pending
    MemoryFact(kind="proposed_fact", status="active") rows: the newest per
    group stays active, the rest are marked superseded (pointing at the
    newest) — same vocabulary supersede_learned_memory/process_work_learning
    already use, not a new one. Returns how many rows were superseded."""
    rows = (
        await db.execute(
            select(MemoryFact)
            .where(MemoryFact.kind == "proposed_fact", MemoryFact.status == "active")
            .order_by(MemoryFact.scope, MemoryFact.title, MemoryFact.created_at.desc())
        )
    ).scalars().all()

    groups: dict[tuple[str, str], list[MemoryFact]] = defaultdict(list)
    for fact in rows:
        groups[(fact.scope, fact.title)].append(fact)

    consolidated = 0
    for (scope, title), group in groups.items():
        if len(group) < 2:
            continue
        newest, *older = group  # SQL order (created_at desc) puts the newest first per group
        for dup in older:
            dup.status = "superseded"
            dup.superseded_by_id = newest.id
            consolidated += 1
        logger.info(
            "idle_reflection_proposed_fact_consolidated",
            scope=scope, title=title[:100], kept=str(newest.id), superseded=len(older),
        )
    return consolidated


# ── Task 2: connector revalidation ─────────────────────────────────────────


async def revalidate_due_connectors(db: AsyncSession, *, batch_size: int) -> dict[str, int]:
    """Direct re-fetch of each revalidation-due connector's sample_url — see
    module docstring for why this is a direct fetch, not a WorkOrder cycle.

    Applies the outcome via connectors._apply_connector_outcome directly on
    the SAME connector object already loaded in ``db`` (this function's own
    session), not record_connector_outcome (which opens its own, separate
    session) and not record_connector_success/failure (which infer identity
    from a URL's domain and deliberately exclude retired connectors from
    their dedupe lookup — see connectors.py's docstrings — so they would
    silently create a fresh draft instead of reviving the specific retired
    connector being revalidated here). Same cross-connection-visibility
    rationale as Ф5's own fix to record_connector_success/failure: a second,
    independently-opened session cannot see this session's own not-yet-
    externally-committed work in a test transaction, and in production it is
    simply a wasted extra round trip when the row is already in hand.
    """
    from app.ai.connectors import (
        _MIN_USEFUL_TEXT_LENGTH,
        _apply_connector_outcome,
        due_for_revalidation,
    )

    candidates = (
        await db.execute(
            select(SourceConnector)
            .where(SourceConnector.revalidate_after.is_not(None))
            .order_by(SourceConnector.revalidate_after.asc())
            .limit(max(1, batch_size) * 3)  # due_for_revalidation is a Python-side time check
        )
    ).scalars().all()
    due = [c for c in candidates if due_for_revalidation(c)][: max(1, batch_size)]

    results = {"checked": 0, "revived_or_confirmed": 0, "still_failing": 0, "skipped_no_url": 0}
    for connector in due:
        sample_url = (connector.strategy or {}).get("sample_url")
        if not sample_url:
            results["skipped_no_url"] += 1
            continue
        results["checked"] += 1
        success = False
        try:
            from app.api.web_search import WebFetchRequest, fetch_page

            page = await fetch_page(WebFetchRequest(url=sample_url, screenshot=False, max_chars=20000))
            success = page.status == 200 and len(page.text.strip()) >= _MIN_USEFUL_TEXT_LENGTH
        except Exception as exc:
            logger.info("idle_reflection_revalidation_fetch_failed", connector=str(connector.id), error=str(exc))
        _apply_connector_outcome(connector, success=success)
        results["revived_or_confirmed" if success else "still_failing"] += 1
    await db.flush()
    return results


# ── Entry point ──────────────────────────────────────────────────────────


async def run_idle_reflection(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    """force=True (manual trigger) bypasses enabled/idle/interval checks
    entirely, same "force" semantic as run_graph_analytics_async."""
    if not force:
        settings = get_idle_reflection_settings()
        if not settings.enabled:
            return {"skipped": True, "reason": "disabled"}
        if not await is_system_idle(db, threshold_minutes=settings.idle_threshold_minutes):
            return {"skipped": True, "reason": "user_active"}
        state = await _load_run_state(db)
        last_run_at = state.get("last_run_at") if state else None
        if last_run_at:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run_at)).total_seconds()
            if elapsed < settings.min_interval_seconds:
                return {"skipped": True, "reason": "interval_not_elapsed"}
        batch_size = settings.connector_revalidation_batch_size
    else:
        batch_size = _DEFAULT_CONNECTOR_BATCH_SIZE

    consolidated = await consolidate_duplicate_proposed_facts(db)
    revalidation = await revalidate_due_connectors(db, batch_size=batch_size)
    await _save_run_state(db)

    logger.info(
        "idle_reflection_run",
        consolidated_facts=consolidated,
        connector_revalidation=revalidation,
    )
    return {
        "skipped": False,
        "consolidated_facts": consolidated,
        "connector_revalidation": revalidation,
    }
