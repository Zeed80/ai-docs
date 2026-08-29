"""Ф6.5 — declared scenario triggers must be true or gone.

``gateway.yml`` described scenarios with ``trigger: {type: schedule, cron: ...}``
and ``{type: event, ...}``, and nothing ever read those declarations: the runner
was reachable only through ``POST /api/scenarios/{name}/run``. So the config
promised recurring work — low_stock_alert every morning, memory_maintenance
every night — that had never run once. That is the most expensive kind of dead
code, because it looks configured.
"""

from datetime import datetime, timezone

import pytest

from app.ai.gateway_config import gateway_config
from app.tasks.scenario_cron import dispatch_event, scenario_cron_dispatch


def _scheduled() -> dict[str, str]:
    return {
        s["name"]: (s.get("trigger") or {}).get("cron")
        for s in gateway_config.scenario_definitions
        if (s.get("trigger") or {}).get("type") == "schedule"
    }


def test_every_declared_cron_is_parseable():
    """A cron expression the dispatcher cannot read is a scenario that silently
    never runs — the exact failure this phase is about."""
    from app.tasks.agent_cron import cron_matches

    scheduled = _scheduled()
    assert scheduled, "в gateway.yml не осталось сценариев по расписанию"
    for name, expression in scheduled.items():
        assert expression and len(expression.split()) == 5, f"{name}: {expression!r}"
        # Must not raise, and must be decidable for a concrete minute.
        assert isinstance(
            cron_matches(expression, datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)),
            bool,
        )


def test_email_triage_no_longer_claims_a_schedule():
    """Superseded by the real pipeline (beat poll + email.triage_message).
    Leaving the cron in place would poll IMAP a second time every 5 minutes."""
    assert "email_triage" not in _scheduled()
    trigger = next(
        (s.get("trigger") for s in gateway_config.scenario_definitions
         if s.get("name") == "email_triage"),
        None,
    )
    assert trigger == {"type": "manual"}


def test_daily_scenarios_fire_at_their_declared_minute(monkeypatch):
    launched: list[tuple] = []
    monkeypatch.setattr(
        "app.tasks.scenario_cron.run_scenario.apply_async",
        lambda args=None, kwargs=None, **k: launched.append((args[0], kwargs)),
    )
    monkeypatch.setattr("app.utils.redis_client.get_sync_redis", lambda: None)

    class _FixedTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("app.tasks.scenario_cron.datetime", _FixedTime)

    result = scenario_cron_dispatch.apply().get()
    assert "low_stock_alert" in result["dispatched"]      # cron: 0 9 * * *
    assert "memory_maintenance" not in result["dispatched"]  # cron: 30 2 * * *
    assert [name for name, _ in launched] == result["dispatched"]


def test_nothing_is_dispatched_on_an_ordinary_minute(monkeypatch):
    monkeypatch.setattr("app.utils.redis_client.get_sync_redis", lambda: None)

    class _FixedTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 29, 13, 47, tzinfo=timezone.utc)

    monkeypatch.setattr("app.tasks.scenario_cron.datetime", _FixedTime)
    assert scenario_cron_dispatch.apply().get()["dispatched"] == []


def test_event_dispatch_starts_only_the_scenarios_that_declared_it(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(
        "app.tasks.scenario_cron.run_scenario.apply_async",
        lambda args=None, kwargs=None, **k: launched.append(args[0]),
    )

    started = dispatch_event("invoice.approved", {"invoice_id": "x"})
    assert "warehouse_receipt" in started
    assert launched == started

    launched.clear()
    assert dispatch_event("nothing.declares.this") == []
    assert launched == []
