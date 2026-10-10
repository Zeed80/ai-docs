"""Planner dates: "tomorrow" needs a "now", and an invented ${...} reference
never reaches a tool (live 2026-10-10: three reminder plans failed with 422 on
remind_at="tomorrow" and remind_at="${runtime.current_date_plus_1_day_iso}")."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import WorkOrder, WorkStep
from app.domain import work_planning


@pytest.mark.asyncio
async def test_the_planner_prompt_carries_now_and_the_date_rule(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "default_timezone", "Europe/Moscow")
    order = WorkOrder(
        objective="Создай напоминание на завтра",
        description=None,
        constraints={},
        budgets={},
        metadata_={},
    )
    captured: dict = {}

    async def fake_generate_json(prompt, *, system, **_kwargs):
        captured.update(prompt=prompt, system=system)
        return {
            "assumptions": [],
            "steps": [
                {
                    "step_key": "s1",
                    "title": "t",
                    "kind": "capability",
                    "capability": "documents",
                    "action": "list",
                    "input": {},
                }
            ],
            "verification_plan": {},
        }

    with patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)):
        await work_planning.generate_capability_plan(order)

    now = json.loads(captured["prompt"])["now"]
    assert "+03:00" in now and "Europe/Moscow" in now
    assert "never as" in captured["system"] and "no other ${...} reference" in captured["system"]


@pytest.mark.parametrize(
    "value",
    ["${runtime.current_date_plus_1_day_iso}", "напомнить ${env.TODAY}", "${steps.a.output}"],
)
def test_any_leftover_placeholder_counts_as_unresolved(value):
    assert work_planning._unresolved_refs({"remind_at": value})


def test_ordinary_text_with_a_dollar_is_not_a_placeholder():
    assert not work_planning._unresolved_refs({"message": "Стоимость $5, шаблон {name}"})


@pytest.mark.asyncio
async def test_an_invented_reference_fails_before_the_tool(db_session):
    from app.domain.work_orders import create_work_order, create_work_plan

    order = await create_work_order(db_session, owner_key="planner-dates", objective="Напомнить")
    _plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "remind",
                "kind": "capability",
                "capability": "analytics",
                "action": "calendar_create_reminder",
                "input": {"remind_at": "${runtime.current_date_plus_1_day_iso}", "message": "x"},
            }
        ],
        actor="test",
    )
    step: WorkStep = steps[0]
    with pytest.raises(ValueError, match="unresolved"):
        await work_planning.resolve_step_input(db_session, step)
    assert uuid.UUID(str(step.id))
