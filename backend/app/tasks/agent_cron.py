"""Executor for AgentCron — scheduled autonomous agent work.

``AgentCron`` rows used to be stored-only fixtures; this beat task makes them
real: every minute it checks enabled crons against their 5-field schedule and
runs each due prompt as a HEADLESS agent turn (AgentSession without a
WebSocket). The result is recorded as an ``AgentTask`` for auditability.

Safety: a headless turn has nobody to answer approval requests, so any
approval-gated action times out and is auto-denied by the existing
``_request_approval`` flow — scheduled work can only do what needs no human
gate. AgentTeam execution remains intentionally out of scope (stored-only).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 8000
_TURN_TIMEOUT_S = 600.0


# ── Minimal 5-field cron matcher ───────────────────────────────────────────────
# Supports: "*", "*/n", "a", "a-b", "a,b,c" (and combinations via commas).
# Deterministic and dependency-free; the dispatcher runs once a minute, so
# "due" means "the current minute matches and we did not already run in it".


def _field_matches(field: str, value: int, minimum: int = 0) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step > 0 and (value - minimum) % step == 0:
                return True
        elif "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
            except ValueError:
                continue
            if lo <= value <= hi:
                return True
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_matches(schedule: str, moment: datetime) -> bool:
    """True when the 5-field cron expression matches the given minute."""
    fields = (schedule or "").split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        _field_matches(minute, moment.minute)
        and _field_matches(hour, moment.hour)
        and _field_matches(dom, moment.day, minimum=1)
        and _field_matches(month, moment.month, minimum=1)
        # cron: 0=Sunday..6=Saturday; Python: Monday=0..Sunday=6.
        and _field_matches(dow, (moment.weekday() + 1) % 7)
    )


def _is_due(schedule: str, last_run_at: datetime | None, now: datetime) -> bool:
    if not cron_matches(schedule, now):
        return False
    if last_run_at is None:
        return True
    last = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=UTC)
    # Already ran within the current minute → not due again.
    return last.replace(second=0, microsecond=0) < now.replace(second=0, microsecond=0)


# ── Headless agent turn ────────────────────────────────────────────────────────


async def run_headless_agent_turn(prompt: str) -> tuple[bool, str]:
    """Run one agent turn without a WebSocket; returns (ok, final_text)."""
    from app.ai.agent_loop import AgentSession

    chunks: list[str] = []
    errors: list[str] = []

    async def collect(event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype == "text":
            chunks.append(str(event.get("content") or ""))
        elif etype == "error":
            errors.append(str(event.get("content") or ""))
        elif etype == "approval_request":
            # Headless: nobody can approve — the session's own timeout denies it.
            logger.warning("agent_cron_approval_requested_headless")

    session = AgentSession(collect)
    try:
        await asyncio.wait_for(session.on_user_message(prompt), timeout=_TURN_TIMEOUT_S)
    except TimeoutError:
        errors.append("turn timed out")
    text = "".join(chunks).strip()[:_MAX_OUTPUT_CHARS]
    ok = bool(text) and not errors
    if errors:
        text = (text + "\n\n[errors] " + "; ".join(errors))[:_MAX_OUTPUT_CHARS]
    return ok, text


async def _run_headless_turn(prompt: str) -> tuple[bool, str]:
    return await run_headless_agent_turn(prompt)


async def _dispatch() -> int:
    from sqlalchemy import select

    from app.db.models import AgentCron, AgentTask
    from app.db.session import _get_session_factory

    now = datetime.now(UTC)
    factory = _get_session_factory()
    executed = 0

    async with factory() as db:
        crons = list(
            (await db.execute(select(AgentCron).where(AgentCron.enabled.is_(True))))
            .scalars()
            .all()
        )

    for cron in crons:
        if not _is_due(cron.schedule, cron.last_run_at, now):
            continue
        logger.info("agent_cron_due id=%s schedule=%r", cron.id, cron.schedule)

        # Claim the run BEFORE executing so a crashed turn is not retried
        # every minute for the rest of the matching window.
        async with factory() as db:
            row = await db.get(AgentCron, cron.id)
            if row is None or not row.enabled:
                continue
            if not _is_due(row.schedule, row.last_run_at, now):
                continue  # another worker claimed it
            row.last_run_at = now
            row.run_count += 1
            await db.commit()

        ok, output = await _run_headless_turn(cron.prompt)
        executed += 1

        async with factory() as db:
            db.add(AgentTask(
                objective=f"Cron: {(cron.description or cron.prompt)[:200]}",
                description=cron.prompt,
                role="secretary",
                status="completed" if ok else "failed",
                output=output or None,
                metadata_={
                    "agent_cron_id": str(cron.id),
                    "schedule": cron.schedule,
                    "ran_at": now.isoformat(),
                },
            ))
            await db.commit()
        logger.info("agent_cron_executed id=%s ok=%s", cron.id, ok)

    return executed


@celery_app.task(
    name="agent.cron_dispatch",
    bind=True,
    max_retries=0,
    queue="scheduler",
    ignore_result=True,
)
def dispatch_agent_crons(self) -> None:  # type: ignore[override]
    """Run due AgentCron prompts as headless agent turns (beat: every minute)."""
    try:
        run_async(_dispatch())
    except Exception as exc:
        logger.error("agent_cron_dispatch_failed", exc_info=exc)
