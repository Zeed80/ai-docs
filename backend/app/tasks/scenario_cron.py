"""Make declared scenario triggers actually fire — Ф6.5.

``gateway.yml`` describes scenarios with ``trigger: {type: schedule, cron: ...}``
and ``{type: event, ...}``. Nothing ever read those: ``scenario_runner.run()``
was reachable only through ``POST /api/scenarios/{name}/run``. So the config
promised recurring work (``email_triage`` every five minutes, ``low_stock_alert``
daily at 09:00, ``memory_maintenance`` nightly) that had never run once — the
most expensive kind of dead code, because it looks configured.

Cron matching reuses ``agent_cron.cron_matches`` rather than parsing cron a
second time; two implementations of the same expression drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

# Beat ticks once a minute; the Redis key stops a second beat (or a restart
# inside the same minute) from running a scenario twice.
_LOCK_TTL_SECONDS = 90


@celery_app.task(name="scenario.cron_dispatch", bind=True, queue="scheduler")
def scenario_cron_dispatch(self) -> dict:
    """Run every scenario whose cron expression matches this minute."""
    from app.ai.gateway_config import gateway_config
    from app.tasks.agent_cron import cron_matches

    now = datetime.now(UTC)
    due: list[str] = []
    for sdef in gateway_config.scenario_definitions:
        trigger = sdef.get("trigger") or {}
        if trigger.get("type") != "schedule":
            continue
        schedule = str(trigger.get("cron") or "").strip()
        name = sdef.get("name")
        if not schedule or not name:
            continue
        if cron_matches(schedule, now):
            due.append(name)

    if not due:
        return {"status": "ok", "dispatched": []}

    try:
        from app.utils.redis_client import get_sync_redis

        redis = get_sync_redis()
    except Exception:  # noqa: BLE001
        redis = None

    dispatched: list[str] = []
    for name in due:
        if redis is not None:
            key = f"scenario:cron:{name}:{now.strftime('%Y%m%d%H%M')}"
            if not redis.set(key, "1", nx=True, ex=_LOCK_TTL_SECONDS):
                continue
        run_scenario.apply_async(args=[name], kwargs={"triggered_by": "cron"}, queue="scheduler")
        dispatched.append(name)

    logger.info("scenario_cron_dispatch", dispatched=dispatched)
    return {"status": "ok", "dispatched": dispatched}


@celery_app.task(name="scenario.run", bind=True, max_retries=1, queue="scheduler")
def run_scenario(
    self, name: str, triggered_by: str = "system", trigger: dict | None = None
) -> dict:
    """Execute one scenario through the existing runner."""
    from app.ai.scenario_runner import scenario_runner
    from app.tasks.async_runner import run_async

    async def _go() -> dict:
        return await scenario_runner.run(name, trigger=trigger, triggered_by=triggered_by)

    try:
        result = run_async(_go())
    except Exception as exc:  # noqa: BLE001
        logger.error("scenario_run_failed", scenario=name, error=str(exc))
        raise self.retry(exc=exc, countdown=300)

    logger.info("scenario_ran", scenario=name, triggered_by=triggered_by)
    return {"status": "ok", "scenario": name, "result_keys": sorted(result or {})[:20]}


def dispatch_event(event: str, payload: dict | None = None) -> list[str]:
    """Run scenarios declared for ``trigger: {type: event}``.

    Called from the places that already know an event happened, rather than by
    inventing an event bus: the declarations name concrete domain moments
    (``invoice.approved``, ``document.review_opened``).
    """
    from app.ai.gateway_config import gateway_config

    started: list[str] = []
    for sdef in gateway_config.scenario_definitions:
        trigger = sdef.get("trigger") or {}
        if trigger.get("type") != "event":
            continue
        declared = trigger.get("events") or ([trigger["event"]] if trigger.get("event") else [])
        if event not in declared:
            continue
        name = sdef.get("name")
        if not name:
            continue
        run_scenario.apply_async(
            args=[name],
            kwargs={"triggered_by": f"event:{event}", "trigger": payload or {}},
            queue="scheduler",
        )
        started.append(name)
    if started:
        # NB: structlog's first positional argument is itself called "event",
        # so the domain event goes under a different key.
        logger.info("scenario_event_dispatch", domain_event=event, scenarios=started)
    return started
