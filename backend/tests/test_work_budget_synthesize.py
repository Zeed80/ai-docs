"""E21.3c: the durable synthesize step writes the final answer under the ledger."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkEvent, WorkOrder, WorkStep
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_budget_usage import LLM_USAGE_EVENT_TYPE
from app.domain.work_orders import claim_ready_step, create_work_order, create_work_plan, utcnow
from app.domain.work_planning import PlannedStep
from app.tasks.work_orders import execute_claimed_step
from tests.test_work_budget_planner import _install_http, _install_model, _reservations


def _model_reply(payload):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {"content": json.dumps(payload, ensure_ascii=False)},
                "done": True,
                "prompt_eval_count": 120,
                "eval_count": 30,
            }

    return Response()


async def _order_ready_to_synthesize(factory, *, constraints=None):
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="synth-owner",
            objective="Сколько поставщиков в справочнике?",
            constraints=constraints or {},
        )
        await initialize_budget_ledger(db, order.id)
        _plan, (lookup, synth) = await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "lookup",
                    "title": "Поставщики",
                    "kind": "capability",
                    "capability": "suppliers",
                    "action": "list",
                    "input": {},
                },
                {
                    "step_key": "answer",
                    "title": "Итог",
                    "kind": "synthesize",
                    "input": {"instruction": "Назови число поставщиков"},
                    "depends_on": ["lookup"],
                },
            ],
            actor="test",
        )
        # The lookup already ran; the synthesize step is next.
        lookup.state = "succeeded"
        lookup.output = {"result": {"items": [{"name": "А"}, {"name": "Б"}], "total": 39}}
        lookup.finished_at = utcnow()
        synth.state = "ready"
        await db.commit()
        claimed = await claim_ready_step(db, worker_id="w", work_order_id=order.id)
        assert claimed is not None and claimed[1].id == synth.id
        await db.commit()
        return order.id, claimed[1].id, claimed[2].id, order.budget_ledger_id


@pytest.mark.asyncio
async def test_synthesis_writes_the_answer_and_charges_the_shared_ledger(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    order_id, step_id, attempt_id, ledger_id = await _order_ready_to_synthesize(factory)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_model_reply({"text": "В справочнике 39 поставщиков."})], calls)

    assert await execute_claimed_step(
        step_id, attempt_id, schedule_verification=False, session_factory=factory
    )

    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state == "succeeded"
        assert step.output == {"text": "В справочнике 39 поставщиков.", "executor": "synthesize"}
        receipts = list(
            await db.scalars(
                select(WorkEvent).where(
                    WorkEvent.work_order_id == order_id,
                    WorkEvent.event_type == LLM_USAGE_EVENT_TYPE,
                )
            )
        )
    sent = json.loads(calls[0][1]["json"]["messages"][-1]["content"])
    assert "39" in sent["step_results"]  # the model saw the completed result
    charged = [row for row in await _reservations(factory, order_id) if row.reserved_units == 1]
    assert len(calls) == len(charged) == 1
    assert charged[0].ledger_id == ledger_id
    assert [r.payload["tokens"]["total"] for r in receipts] == [{"status": "known", "units": 150}]


@pytest.mark.asyncio
async def test_non_local_reasoning_model_stops_before_any_request(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    order_id, step_id, attempt_id, _ledger = await _order_ready_to_synthesize(factory)
    _install_model(monkeypatch, provider="anthropic")
    calls = []
    _install_http(monkeypatch, [], calls)

    await execute_claimed_step(
        step_id, attempt_id, schedule_verification=False, session_factory=factory
    )

    assert calls == []
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        order = await db.get(WorkOrder, order_id)
        assert step.state != "succeeded"
        assert "direct_text_provider_unsupported" in json.dumps(
            order.blocker or step.last_error or {}
        )


@pytest.mark.asyncio
async def test_exploratory_synthesis_keeps_the_coverage_report(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    _order_id, step_id, attempt_id, _ledger = await _order_ready_to_synthesize(
        factory, constraints={"mode": "exploratory"}
    )
    _install_model(monkeypatch)
    coverage = {"covered": ["А"], "partial": [], "not_found": []}
    _install_http(monkeypatch, [_model_reply({"text": "Готово", "coverage": coverage})], [])

    assert await execute_claimed_step(
        step_id, attempt_id, schedule_verification=False, session_factory=factory
    )
    async with factory() as db:
        assert (await db.get(WorkStep, step_id)).output["coverage"] == coverage


def test_planner_accepts_a_synthesize_step():
    step = PlannedStep(step_key="answer", title="Итог", kind="synthesize", input={})
    assert step.kind == "synthesize"
